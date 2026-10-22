"""Developer tools for repositories inside the workspace."""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from config.settings import ProjectCommand, ProjectRule
from core.audit import redact
from core.policy import Permission
from tools.filesystem import is_sensitive_name, resolve_safe_path
from tools.linux_system import launch_detached, project_env, run_bounded, run_command
from tools.registry import STATUS_ERROR, STATUS_SUCCESS, STATUS_UNVERIFIED, UNTRUSTED_NOTICE, ToolError

OUTPUT_TAIL_CHARS = 4000  # failures are usually at the end of a test run's output
MAX_DIFF_CHARS = 8000
MAX_SEARCH_FILES = 5000
MAX_SEARCH_FILE_BYTES = 1_000_000
# Dependency, build and cache folders: large, generated, and rarely what the user means.
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
                       "build", "dist", "target", ".dart_tool", ".bundle", "vendor", "tmp", "log", "coverage",
                       ".next", ".expo", ".gradle", ".idea", ".tox", "storage"})
PROJECT_MARKERS = ("package.json", "Gemfile", "pubspec.yaml", "pyproject.toml", "pytest.ini", "requirements.txt", "setup.py",
                   "go.mod", "Cargo.toml", "composer.json", "pom.xml", "build.gradle", "Makefile", "Dockerfile",
                   "docker-compose.yml", "bin/rails", "app.json", "tsconfig.json")
MAX_EDIT_FILE_BYTES = 1_000_000
MAX_NEW_FILE_CHARS = 200_000
BACKUPS_KEPT = 200
_SINCE = re.compile(r"today|yesterday|\d{1,3} (?:minute|hour|day|week|month|year)s? ago|\d{4}-\d{2}-\d{2}")


def unified_diff(old: str, new: str, label: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{label}", f"b/{label}", lineterm=""))


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _diff_counts(diff: str) -> tuple[int, int]:
    lines = diff.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return added, removed


def _count_overlapping(text: str, needle: str) -> int:
    """Occurrences of needle, overlapping ones included (str.count skips them)."""
    count, start = 0, text.find(needle)
    while start != -1:
        count += 1
        start = text.find(needle, start + 1)
    return count


def replace_once(text: str, old_text: str, new_text: str) -> str:
    """Replace one exact, unique occurrence. Files with Windows line endings keep them."""
    if not old_text:
        raise ToolError("old_text must not be empty; use create_workspace_file for a new file.")
    if old_text == new_text:
        raise ToolError("old_text and new_text are identical; there is nothing to change.")
    count = _count_overlapping(text, old_text)
    if count == 0 and "\r\n" in text and "\n" in old_text:
        crlf = lambda s: s.replace("\r\n", "\n").replace("\n", "\r\n")  # noqa: E731
        old_text, new_text = crlf(old_text), crlf(new_text)
        count = _count_overlapping(text, old_text)
    if count == 0:
        raise ToolError("old_text was not found in the file. Read the file again and copy the text exactly, "
                        "including indentation.")
    if count > 1:
        raise ToolError(f"old_text appears {count} times; include more surrounding lines so it matches once.")
    return text.replace(old_text, new_text, 1)


def write_text_atomically(target: Path, text: str, mode: int | None = None) -> None:
    """Write via a temporary file in the same folder and a rename: never a half-written file."""
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".jarvis-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _walk_source_files(root: Path):
    """Files under root, skipping dependency/build folders, protected names and symlinked folders."""
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not is_sensitive_name(d))
        for name in sorted(files):
            if not is_sensitive_name(name):
                path = Path(folder) / name
                if not path.is_symlink():
                    yield path


def command_fits(command: ProjectCommand, project: Path) -> bool:
    """Whether an approved command applies to a project: a marker file exists and, for npm
    commands, package.json defines the script."""
    if command.detect and not any((project / marker).exists() for marker in command.detect):
        return False
    if command.npm_script:
        try:
            scripts = json.loads((project / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            return False
        return isinstance(scripts, dict) and command.npm_script in scripts
    return True


def resolve_argv(command: ProjectCommand, project: Path) -> list[str]:
    """The command line to run: "python" means the project's own virtualenv when it has one, and a
    relative path such as "bin/rails" means the project's own script."""
    argv = list(command.argv)
    if argv[0] in ("python", "python3"):
        for venv in (".venv", "venv"):
            candidate = project / venv / "bin" / "python"
            if candidate.exists():
                argv[0] = str(candidate)
                break
    elif "/" in argv[0] and not os.path.isabs(argv[0]):
        argv[0] = str(project / argv[0])
    return argv


class DeveloperTools:
    """Developer tool handlers. Mixed into JarvisTools, which provides self.config and self.memory."""

    def open_project(self, path: str) -> dict[str, Any]:
        target = resolve_safe_path(path, self.config.workspace)
        if not target.is_dir():
            raise ToolError(f"{path!r} is not a directory inside {self.config.workspace}.")
        editor = shlex.split(self.config.editor) or ["code"]
        if shutil.which(editor[0]) is None:
            raise ToolError(f"The configured editor '{editor[0]}' is not installed. Set JARVIS_EDITOR.")
        outcome = launch_detached([*editor, str(target)])
        return {"opened": str(target), "editor": editor[0], **outcome,
                "note": "Editor launch requested; JARVIS cannot see the window."}

    # -- developer (read-only) ----------------------------------------------
    def _git(self, path: str) -> tuple[Path, list[str], dict[str, str]]:
        """A hardened git invocation for a repository inside the workspace: (folder, base argv, env).

        Inherited GIT_* variables (GIT_DIR, GIT_WORK_TREE...) could point git elsewhere, so they are
        dropped. The ceiling stops git discovering a repository above the workspace (e.g. a dotfiles
        repo in $HOME), and optional locks off means read-only commands never rewrite the index.
        Command-line -c settings override repository config, so a hostile repo cannot make these
        commands run fsmonitor, hooks, filter drivers, pagers or external diff programs."""
        target = resolve_safe_path(path, self.config.workspace)
        if not target.is_dir():
            raise ToolError(f"{path!r} is not a directory inside {self.config.workspace}.")
        workspace = self.config.workspace.expanduser().resolve()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CEILING_DIRECTORIES": str(workspace.parent)})
        overrides = ["core.fsmonitor=false", "core.untrackedCache=false", "core.hooksPath=/dev/null",
                     "core.pager=cat", "color.ui=false"]
        listed = run_command(["git", "-C", str(target), "config", "--includes", "--name-only",
                              "--get-regexp", r"^filter\."], timeout=10, env=env)
        for filter_name in {key.rsplit(".", 1)[0] for key in listed.stdout.split()}:
            overrides += [f"{filter_name}.clean=", f"{filter_name}.smudge=", f"{filter_name}.process=",
                          f"{filter_name}.required=false"]
        base = ["git", "--no-pager"]
        for override in overrides:
            base += ["-c", override]
        base += ["-C", str(target)]
        toplevel = run_command([*base, "rev-parse", "--show-toplevel"], timeout=10, env=env)
        if toplevel.returncode != 0:
            raise ToolError(f"{path!r} is not inside a git repository in the workspace.")
        top = Path(toplevel.stdout.strip()).resolve()
        if top != workspace and workspace not in top.parents:
            raise ToolError(f"The repository for {path!r} is outside the workspace.")
        return target, base, env

    def inspect_git_status(self, path: str) -> dict[str, Any]:
        target, base, env = self._git(path)
        status = run_command([*base, "status", "--short", "--branch"], timeout=15, env=env)
        if status.returncode != 0:
            raise ToolError(f"git status failed: {status.stderr.strip()[:300]}")
        diff = run_command([*base, "diff", "--stat", "--no-ext-diff", "--no-textconv"], timeout=15, env=env)
        return {"path": str(target), "status": STATUS_SUCCESS,
                "git_status": status.stdout[:3000], "diff_stat": diff.stdout[:3000] if diff.returncode == 0 else None}

    def inspect_git_diff(self, path: str, file: str | None = None, staged: bool = False) -> dict[str, Any]:
        """The full diff of a repository (or one file in it), without protected files, redacted."""
        target, base, env = self._git(path)
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames"] + (["--cached"] if staged else [])
        if file:  # a file inside the repository; the same protected-name rules as reading files
            pathspecs = [str(resolve_safe_path(str(Path(path) / file), self.config.workspace))]
        else:
            # Exclude every changed path that read_workspace_file would refuse (case-insensitive names,
            # protected folders such as .kube), by the protected part of its path.
            names = run_command([*base, *args, "--name-only", "-z", "--", "."], timeout=20, env=env)
            if names.returncode != 0:
                raise ToolError(f"git diff failed: {names.stderr.strip()[:300]}")
            excluded = set()
            for name in filter(None, names.stdout.split("\0")):
                parts = Path(name).parts
                hit = next((i for i, part in enumerate(parts) if is_sensitive_name(part)), None)
                if hit is not None:
                    excluded.add("/".join(parts[:hit + 1]))
            pathspecs = ["."] + [f":(top,exclude,literal){prefix}" for prefix in sorted(excluded)]
        diff = run_command([*base, *args, "--", *pathspecs], timeout=20, env=env)
        if diff.returncode != 0:
            raise ToolError(f"git diff failed: {diff.stderr.strip()[:300]}")
        untracked = run_command([*base, "ls-files", "--others", "--exclude-standard"], timeout=15, env=env)
        text = redact(diff.stdout)
        return {
            "path": str(target), "staged": staged, "file": file,
            "diff": text[:MAX_DIFF_CHARS], "diff_truncated": len(text) > MAX_DIFF_CHARS,
            "untracked_files": [name for name in untracked.stdout.splitlines()
                                if not any(is_sensitive_name(part) for part in Path(name).parts)][:50],
            "note": "Protected files (.env, keys, credentials) are excluded. Untracked files have no diff.",
            "notice": UNTRUSTED_NOTICE,
        }

    def inspect_git_history(self, path: str, since: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Recent commits with the files each one changed, e.g. "what did I change today"."""
        target, base, env = self._git(path)
        args = ["log", "--no-color", "--date=iso-local", f"-n{max(1, min(limit, 50))}", "--name-status",
                "--pretty=format:commit %h  %ad  %an%n    %s"]
        if since:
            since = since.strip().lower()
            if not _SINCE.fullmatch(since):
                raise ToolError("'since' must be like 'today', 'yesterday', '3 days ago' or a date such as 2026-10-01.")
            # git reads a bare date as that date at the current time of day; a date means from its start.
            if since == "today":
                git_since = "midnight"
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
                git_since = f"{since} 00:00:00"
            else:
                git_since = since
            args.append(f"--since={git_since}")
        log_result = run_command([*base, *args], timeout=20, env=env)
        if log_result.returncode != 0:
            raise ToolError(f"git log failed: {log_result.stderr.strip()[:300]}")
        status = run_command([*base, "status", "--short"], timeout=15, env=env)
        text = redact(log_result.stdout)
        return {"path": str(target), "since": since, "commits": text[:MAX_DIFF_CHARS] or "(no commits in that range)",
                "commits_truncated": len(text) > MAX_DIFF_CHARS,
                "uncommitted_changes": status.stdout[:3000] or "(none)", "notice": UNTRUSTED_NOTICE}

    def describe_project(self, path: str) -> dict[str, Any]:
        """A quick orientation: what kind of project, its layout, git state and README opening."""
        target = resolve_safe_path(path, self.config.workspace)
        if not target.is_dir():
            raise ToolError(f"{path!r} is not a project folder inside {self.config.workspace}.")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if is_sensitive_name(child.name) or child.name in SKIP_DIRS:
                continue
            entries.append(child.name + ("/" if child.is_dir() else ""))
        result: dict[str, Any] = {
            "path": str(target), "top_level": entries[:80], "top_level_truncated": len(entries) > 80,
            "markers": [name for name in PROJECT_MARKERS if (target / name).exists()],
            "approved_commands": {kind: [n for n, s in self.config.commands.items()
                                         if s.kind == kind and command_fits(s, target)] for kind in ("tests", "lint")},
        }
        readme = next((target / n for n in ("README.md", "README.rst", "README.txt", "README") if (target / n).is_file()), None)
        if readme is not None:
            try:
                with readme.open("r", encoding="utf-8", errors="replace") as handle:
                    result["readme_start"] = redact(handle.read(3000))
            except OSError:
                pass
        try:
            _, base, env = self._git(path)
        except ToolError as exc:
            result["git"] = str(exc)
        else:
            branch = run_command([*base, "rev-parse", "--abbrev-ref", "HEAD"], timeout=10, env=env)
            recent = run_command([*base, "log", "--no-color", "-n5", "--pretty=format:%h %ad %s", "--date=short"],
                                 timeout=15, env=env)
            status = run_command([*base, "status", "--short"], timeout=15, env=env)
            result["git"] = {"branch": branch.stdout.strip() or None,
                             "recent_commits": redact(recent.stdout).splitlines(),
                             "uncommitted_files": len(status.stdout.splitlines())}
        result["notice"] = UNTRUSTED_NOTICE
        return result

    def search_workspace_code(self, query: str, path: str = ".", case_sensitive: bool = False,
                              max_results: int = 40) -> dict[str, Any]:
        """Plain-text (not regex) search through source files, skipping dependencies, builds,
        binaries and protected files."""
        root = resolve_safe_path(path or ".", self.config.workspace)
        if not root.exists():
            raise ToolError(f"{path!r} does not exist inside {self.config.workspace}.")
        workspace = self.config.workspace.expanduser().resolve()
        needle = query if case_sensitive else query.lower()
        limit = max(1, min(max_results, 100))
        matches: list[dict[str, Any]] = []
        files_scanned = 0
        candidates = [root] if root.is_file() else _walk_source_files(root)
        for file_path in candidates:
            if files_scanned >= MAX_SEARCH_FILES or len(matches) >= limit:
                break
            try:
                if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                data = file_path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            files_scanned += 1
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
                if needle in (line if case_sensitive else line.lower()):
                    matches.append({"file": str(file_path.relative_to(workspace)), "line": number,
                                    "text": redact(line.strip())[:200]})
                    if len(matches) >= limit:
                        break
        return {"query": query, "searched": str(root), "matches": matches, "files_scanned": files_scanned,
                "truncated": len(matches) >= limit or files_scanned >= MAX_SEARCH_FILES, "notice": UNTRUSTED_NOTICE}

    # -- controlled file changes ------------------------------------------------
    def _relative(self, target: Path) -> str:
        return str(target.relative_to(self.config.workspace.expanduser().resolve()))

    def _file_git_state(self, target: Path) -> str:
        """The file's git situation in plain words, for the confirmation prompt and the result."""
        try:
            _, base, env = self._git(str(target.parent))
        except ToolError:
            return "not in a git repository; JARVIS keeps a backup"
        state = run_command([*base, "status", "--porcelain", "--ignored", "--", str(target)],
                            timeout=10, env=env).stdout.strip()
        if not state:
            return "tracked by git, no other uncommitted changes" if target.exists() else "new file"
        if state.startswith("!!"):
            return "ignored by git; JARVIS keeps a backup"
        if state.startswith("??"):
            return "not yet tracked by git; JARVIS keeps a backup"
        return "has other uncommitted changes, which are kept"

    def _read_editable(self, target: Path) -> tuple[bytes, str]:
        if not target.is_file():
            raise ToolError(f"{self._relative(target)} is not a file; use create_workspace_file for a new one.")
        if target.stat().st_size > MAX_EDIT_FILE_BYTES:
            raise ToolError(f"{self._relative(target)} is larger than 1 MB; JARVIS only edits text files up to 1 MB.")
        data = target.read_bytes()
        if b"\x00" in data[:8192]:
            raise ToolError(f"{self._relative(target)} looks like a binary file; only text files can be edited.")
        try:
            return data, data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{self._relative(target)} is not UTF-8 text, so JARVIS will not edit it.") from None

    def _edit_plan(self, args: dict[str, Any]) -> tuple[Path, bytes, str, str]:
        target = resolve_safe_path(args["path"], self.config.workspace)
        data, text = self._read_editable(target)
        new_text = replace_once(text, args["old_text"], args["new_text"])
        return target, data, new_text, unified_diff(text, new_text, self._relative(target))

    @staticmethod
    def _change_key(*parts: str) -> str:
        return _sha("\x00".join(parts))

    def _describe_edit(self, args: dict[str, Any]) -> tuple[str, list[str], str]:
        target, data, new_text, diff = self._edit_plan(args)
        # Bind the approval to the file exactly as shown: if it changes before the write, nothing is written.
        self.__dict__.setdefault("_approved_changes", {})[
            self._change_key("edit", args["path"], args["old_text"], args["new_text"])] = (str(target), _sha(data))
        added, removed = _diff_counts(diff)
        return (f"Edit {self._relative(target)}: {added} line(s) added, {removed} removed "
                f"({self._file_git_state(target)}).", [str(target)], diff)

    def _backup(self, target: Path, data: bytes) -> Path:
        """Keep the previous version outside the project, so any edit can be undone by hand."""
        root = self.config.backup_dir.expanduser()
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = root / stamp / self._relative(target)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        destination.write_bytes(data)
        destination.chmod(0o600)
        for old in sorted(p for p in root.iterdir() if p.is_dir())[:-BACKUPS_KEPT]:
            shutil.rmtree(old, ignore_errors=True)
        return destination

    def edit_workspace_file(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        target, data, updated, diff = self._edit_plan({"path": path, "old_text": old_text, "new_text": new_text})
        approved = self.__dict__.get("_approved_changes", {}).pop(
            self._change_key("edit", path, old_text, new_text), None)
        if approved is not None and approved != (str(target), _sha(data)):
            raise ToolError(f"{self._relative(target)} changed after the edit was approved. Nothing was written; "
                            "read the file again and propose the edit afresh.")
        git_state = self._file_git_state(target)
        backup = self._backup(target, data)
        try:
            write_text_atomically(target, updated, mode=target.stat().st_mode & 0o7777)
        except OSError as exc:
            raise ToolError(f"Could not write {self._relative(target)}: {exc.strerror or exc}") from None
        verified = target.read_bytes() == updated.encode("utf-8")
        result: dict[str, Any] = {
            "path": self._relative(target), "applied": True, "verified": verified,
            "diff": diff[:MAX_DIFF_CHARS], "git": git_state, "backup": str(backup),
            "next_step": "Run the project's tests with run_project_tests to check the change.",
        }
        if not verified:
            result.update(status=STATUS_UNVERIFIED, message="The file was written but its contents could not be confirmed.")
        return result

    def _create_plan(self, args: dict[str, Any]) -> tuple[Path, str]:
        target = resolve_safe_path(args["path"], self.config.workspace)
        if target.exists() or target.is_symlink():
            raise ToolError(f"{self._relative(target)} already exists; use edit_workspace_file to change it.")
        content = args["content"]
        if len(content) > MAX_NEW_FILE_CHARS or "\x00" in content:
            raise ToolError("New files must be text, at most 200,000 characters.")
        return target, unified_diff("", content, self._relative(target))

    def _describe_create(self, args: dict[str, Any]) -> tuple[str, list[str], str]:
        target, diff = self._create_plan(args)
        self.__dict__.setdefault("_approved_changes", {})[
            self._change_key("create", args["path"], args["content"])] = (str(target), "absent")
        return (f"Create {self._relative(target)} ({len(args['content'].splitlines())} lines).", [str(target)], diff)

    def create_workspace_file(self, path: str, content: str) -> dict[str, Any]:
        target, diff = self._create_plan({"path": path, "content": content})
        approved = self.__dict__.get("_approved_changes", {}).pop(self._change_key("create", path, content), None)
        if approved is not None and approved[0] != str(target):
            raise ToolError("The target changed after the new file was approved. Nothing was written.")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomically(target, content, mode=0o644)
        except OSError as exc:
            raise ToolError(f"Could not create {self._relative(target)}: {exc.strerror or exc}") from None
        verified = target.read_text(encoding="utf-8") == content
        result: dict[str, Any] = {"path": self._relative(target), "created": True, "verified": verified,
                                  "diff": diff[:MAX_DIFF_CHARS], "git": self._file_git_state(target),
                                  "next_step": "Run the project's tests with run_project_tests to check the change."}
        if not verified:
            result.update(status=STATUS_UNVERIFIED, message="The file was written but its contents could not be confirmed.")
        return result

    # -- project commands (tests and linters from config/permissions.toml) -----
    def _select_project_command(self, project: str, check: str = "tests",
                                command: str | None = None) -> tuple[Path, ProjectCommand, ProjectRule | None]:
        target = resolve_safe_path(project, self.config.workspace)
        if not target.is_dir():
            raise ToolError(f"{project!r} is not a project folder inside {self.config.workspace}.")
        approved = {name: spec for name, spec in self.config.commands.items() if spec.kind == check}
        if not approved:
            raise ToolError(f"No {check} commands are approved. Add one to {self.config.permissions_file}.")
        workspace = self.config.workspace.expanduser().resolve()
        rule = self.config.projects.get(str(target.relative_to(workspace)))
        if command:
            if command not in approved:
                raise ToolError(f"{command!r} is not an approved {check} command. Approved: {', '.join(approved)}.")
            return target, approved[command], rule
        if rule is not None and getattr(rule, check):
            return target, approved[getattr(rule, check)], rule
        for spec in approved.values():
            if command_fits(spec, target):
                return target, spec, rule
        raise ToolError(f"No approved {check} command fits {target.name}. Approved {check} commands: "
                        f"{', '.join(approved)}. Add or map one in {self.config.permissions_file}.")

    def _project_command_permission(self, args: dict[str, Any]) -> Permission:
        """Running tests runs the project's code, so it asks first unless the user trusts the project.
        A trusted project still asks when the model names a command other than the project's usual one."""
        check, command = args.get("check", "tests"), args.get("command")
        _, spec, rule = self._select_project_command(args["project"], check, command)
        confirm = rule.confirm if rule is not None and rule.confirm is not None else spec.confirm
        if command and not confirm:
            try:
                usual = self._select_project_command(args["project"], check)[1].name
            except ToolError:
                usual = None
            confirm = command != usual
        return Permission.SENSITIVE_ACTION if confirm else Permission.CONTROLLED_ACTION

    @staticmethod
    def _approval_key(args: dict[str, Any]) -> str:
        """The same key for a confirmation and its run, whether or not optional arguments were given."""
        return json.dumps({"project": args.get("project"), "check": args.get("check") or "tests",
                           "command": args.get("command") or None}, sort_keys=True)

    def _describe_project_command(self, args: dict[str, Any]) -> tuple[str, list[str]]:
        target, spec, _ = self._select_project_command(args["project"], args.get("check", "tests"), args.get("command"))
        argv = resolve_argv(spec, target)
        # Remember exactly what the user is shown, so the run cannot silently differ from it.
        self.__dict__.setdefault("_approved_runs", {})[self._approval_key(args)] = (str(target), tuple(argv))
        purpose = f" ({spec.description})" if spec.description else ""
        return (f"Run {spec.kind} for {target.name}{purpose}: {shlex.join(argv)} (stops after {spec.timeout}s). "
                "This runs the project's code with your full user access.", [str(target)])

    def run_project_tests(self, project: str, check: str = "tests", command: str | None = None) -> dict[str, Any]:
        target, spec, _ = self._select_project_command(project, check, command)
        argv = resolve_argv(spec, target)
        approved = self.__dict__.get("_approved_runs", {}).pop(
            self._approval_key({"project": project, "check": check, "command": command}), None)
        if approved is not None and approved != (str(target), tuple(argv)):
            raise ToolError(f"The project changed after the command was approved (approved: {shlex.join(approved[1])}; "
                            f"now: {shlex.join(argv)}). Nothing was run; ask again to approve the new command.")
        outcome = run_bounded(argv, cwd=target, timeout=spec.timeout, env=project_env())
        output = redact(outcome.output)
        lines = [line[:300] for line in output.splitlines() if line.strip()]
        # Outcome first: if the serialized result is ever cut short, the end of the output goes, not the verdict.
        result: dict[str, Any] = {"project": target.name, "check": spec.kind, "command": shlex.join(argv)}
        if outcome.cancelled or outcome.timed_out:
            result["status"] = STATUS_ERROR
            result["error"] = ("Cancelled by the user; the command was stopped." if outcome.cancelled else
                               f"Stopped after the {spec.timeout}s time limit, so there is no complete result.")
        else:
            result["passed"] = outcome.returncode == 0
            result["message"] = f"Ran {spec.name}: exit code {outcome.returncode}."
        result.update({
            "exit_code": outcome.returncode, "duration_seconds": outcome.seconds, "summary": lines[-3:],
            "output_tail": output[-OUTPUT_TAIL_CHARS:],
            "output_truncated": outcome.output_truncated or len(output) > OUTPUT_TAIL_CHARS,
            "notice": UNTRUSTED_NOTICE,
        })
        return result
