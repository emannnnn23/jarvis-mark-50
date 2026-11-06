"""Developer tools for repositories inside the workspace."""

from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import fnmatch
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
from core.audit import redact, reveal_invisible
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
# A change must fit on one review screen: nothing the user approves may be hidden from them.
MAX_REVIEW_LINES = 400
MAX_REVIEW_LINE_CHARS = 1000
APPROVALS_KEPT = 32
_BACKUP_STAMP = re.compile(r"\d{8}-\d{6}-\d{6}")
# Files that build tools, CI or editors run or obey automatically: a change to one deserves a closer look.
RUN_BY_TOOLS = ("Makefile", "makefile", "GNUmakefile", "Justfile", "justfile", "package.json", "conftest.py",
                "setup.py", "pyproject.toml", "tox.ini", "noxfile.py", "Gemfile", "Rakefile", "*.gemspec", "Procfile",
                "Dockerfile", "docker-compose.yml", "compose.yaml", ".yarnrc", ".yarnrc.yml", ".pre-commit-config.yaml",
                ".gitlab-ci.yml", ".gitattributes", ".gitmodules", "build.gradle", "pubspec.yaml", "*.sh",
                ".tool-versions")
RUN_BY_TOOL_DIRS = frozenset({".github", ".vscode", ".husky", ".devcontainer", ".circleci", "bin", "scripts",
                              ".mise", "mise"})
_SINCE = re.compile(r"today|yesterday|\d{1,3} (?:minute|hour|day|week|month|year)s? ago|\d{4}-\d{2}-\d{2}")


def _lines(text: str) -> list[str]:
    """Split only on real newlines. str.splitlines() also splits on form feeds, \\x1c, \\x85 and
    U+2028, which would hide a change that swaps a newline for one of them."""
    lines = text.split("\n")
    if lines and lines[-1] == "":  # the newline ending the last line does not start another one
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def unified_diff(old: str, new: str, label: str) -> str:
    diff = "\n".join(difflib.unified_diff(_lines(old), _lines(new), f"a/{label}", f"b/{label}", lineterm=""))
    if old != new and not diff:
        # Only line endings or a trailing newline changed: nothing the user could see to approve.
        raise ToolError("This change only alters line endings or invisible characters, so it cannot be shown "
                        "for review. JARVIS will not apply it.")
    return diff


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def check_reviewable(diff: str) -> None:
    """Refuse a change the user could not fully see before approving it."""
    lines = diff.split("\n")
    if len(lines) > MAX_REVIEW_LINES:
        raise ToolError(f"This change is too large to review in one step ({len(lines)} diff lines; the limit is "
                        f"{MAX_REVIEW_LINES}). Split it into smaller edits.")
    longest = max((len(line) for line in lines), default=0)
    if longest > MAX_REVIEW_LINE_CHARS:
        raise ToolError(f"A line in this change is {longest} characters long, too long to review on screen "
                        f"(limit {MAX_REVIEW_LINE_CHARS}). Break it into shorter lines.")


def runs_on_tooling(relative: str) -> bool:
    parts = Path(relative).parts
    return (any(part in RUN_BY_TOOL_DIRS for part in parts[:-1])
            or any(fnmatch.fnmatch(parts[-1], pattern) for pattern in RUN_BY_TOOLS))


def change_warnings(relative: str, diff: str) -> str:
    """Extra lines for a confirmation: invisible characters, and files that tools run automatically."""
    notes = []
    added = "\n".join(line for line in diff.split("\n") if line.startswith("+") and not line.startswith("+++"))
    hidden = reveal_invisible(added)[1]  # only what the change adds; context lines are not its doing
    if hidden:
        notes.append(f"WARNING: the change contains {hidden} invisible or text-direction character(s), shown "
                     "as \\u{...}; they can make code read differently from what it does.")
    if runs_on_tooling(relative):
        notes.append("Note: build tools, CI or your editor run or obey this file automatically; read the change carefully.")
    return (" " + " ".join(notes)) if notes else ""


def _diff_counts(diff: str) -> tuple[int, int]:
    lines = diff.split("\n")
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


def write_text_atomically(target: Path, text: str, mode: int | None = None, exclusive: bool = False) -> None:
    """Write via a temporary file in the same folder and a rename: never a half-written file.
    With exclusive=True the file is linked into place only if nothing exists there yet."""
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".jarvis-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        if exclusive:
            try:
                os.link(tmp_name, target)  # raises FileExistsError if something appeared meanwhile
            except FileExistsError:
                raise
            except OSError:  # a file system without hard links (vfat, exFAT, some FUSE mounts)
                _write_exclusive(target, text, mode)
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
        else:
            os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


_GIT_VERSION_OK: bool | None = None


def _require_git_config_env() -> None:
    """The hardening is passed through GIT_CONFIG_COUNT, which git before 2.31 silently ignores."""
    global _GIT_VERSION_OK
    if _GIT_VERSION_OK is None:
        found = re.search(r"(\d+)\.(\d+)", run_command(["git", "version"], timeout=10).stdout)
        _GIT_VERSION_OK = bool(found) and (int(found[1]), int(found[2])) >= (2, 31)
    if not _GIT_VERSION_OK:
        raise ToolError("JARVIS needs git 2.31 or newer to inspect repositories safely; please update git.")


def _write_exclusive(target: Path, text: str, mode: int | None) -> None:
    """Create target only if it does not exist yet, writing it directly."""
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode if mode is not None else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


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
        package = project / "package.json"
        try:
            if project.resolve() not in package.resolve().parents:  # a symlink pointing elsewhere
                return False
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
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
        Settings passed through GIT_CONFIG_COUNT override repository config with the highest
        priority, and take each key literally, so a hostile repo cannot make these commands run
        fsmonitor, hooks, filter drivers (whatever their name), pagers, signature programs or
        network transports such as partial-clone fetches."""
        target = resolve_safe_path(path, self.config.workspace)
        if not target.is_dir():
            raise ToolError(f"{path!r} is not a directory inside {self.config.workspace}.")
        _require_git_config_env()
        workspace = self.config.workspace.expanduser().resolve()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CEILING_DIRECTORIES": str(workspace.parent), "GIT_NO_LAZY_FETCH": "1"})
        overrides = {
            "core.fsmonitor": "false", "core.untrackedCache": "false", "core.hooksPath": "/dev/null",
            "core.pager": "cat", "color.ui": "false",
            "log.showSignature": "false",  # verifying signatures runs the repo's gpg.program
            "protocol.allow": "never",  # no transports: blocks partial-clone fetches and sshCommand
        }
        for scheme in ("file", "ssh", "git", "http", "https", "ext"):  # a repo's protocol.<x>.allow would win otherwise
            overrides[f"protocol.{scheme}.allow"] = "never"
        listed = run_command(["git", "-C", str(target), "config", "-z", "--includes", "--name-only",
                              "--get-regexp", r"^filter\."], timeout=10, env=env)
        for key in filter(None, listed.stdout.split("\0")):  # NUL-separated: names may contain any space
            name = key.rsplit(".", 1)[0]
            overrides.update({f"{name}.clean": "", f"{name}.smudge": "", f"{name}.process": "",
                              f"{name}.required": "false"})
        protocols = run_command(["git", "-C", str(target), "config", "-z", "--includes", "--name-only",
                                 "--get-regexp", r"^protocol\..*\.allow$"], timeout=10, env=env)
        for key in filter(None, protocols.stdout.split("\0")):
            overrides[key] = "never"
        env["GIT_CONFIG_COUNT"] = str(len(overrides))
        for index, (key, value) in enumerate(overrides.items()):
            env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
        base = ["git", "--no-pager", "-C", str(target)]
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
        # A file (or folder) inside the repository, taken literally (no wildcards), under the same
        # protected-name rules as reading files; or the whole repository.
        scope = ([":(literal)" + str(resolve_safe_path(str(Path(path) / file), self.config.workspace))]
                 if file else ["."])
        # Exclude every changed path that read_workspace_file would refuse (case-insensitive names,
        # protected folders such as .kube), by the protected part of its path.
        names = run_command([*base, *args, "--name-only", "-z", "--", *scope], timeout=20, env=env)
        if names.returncode != 0:
            raise ToolError(f"git diff failed: {names.stderr.strip()[:300]}")
        excluded = set()
        for name in filter(None, names.stdout.split("\0")):
            parts = Path(name).parts
            hit = next((i for i, part in enumerate(parts) if is_sensitive_name(part)), None)
            if hit is not None:
                excluded.add("/".join(parts[:hit + 1]))
        pathspecs = scope + [f":(top,exclude,literal){prefix}" for prefix in sorted(excluded)]
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
        args = ["log", "--no-color", "--no-show-signature", "--date=iso-local", f"-n{max(1, min(limit, 50))}", "--name-status",
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
        readme = None
        for name in ("README.md", "README.rst", "README.txt", "README"):
            try:  # a README symlinked to a file outside the workspace is never read
                candidate = resolve_safe_path(str(Path(path) / name), self.config.workspace)
            except ToolError:
                continue
            if candidate.is_file():
                readme = candidate
                break
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
            recent = run_command([*base, "log", "--no-color", "--no-show-signature", "-n5", "--pretty=format:%h %ad %s", "--date=short"],
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

    def _file_git_state(self, target: Path, backed_up: bool = True) -> str:
        """The file's git situation in plain words, for the confirmation prompt and the result."""
        backup = "; JARVIS keeps a backup" if backed_up else ""
        try:
            _, base, env = self._git(str(target.parent))
        except ToolError:
            return "not in a git repository" + backup
        state = run_command([*base, "status", "--porcelain", "--ignored", "--", str(target)],
                            timeout=10, env=env).stdout.strip()
        if not state:
            return "tracked by git, no other uncommitted changes" if target.exists() else "new file"
        if state.startswith("!!"):
            return "ignored by git" + backup
        if state.startswith("??"):
            return "not yet tracked by git" + backup
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

    def _writable(self, raw_path: str) -> Path:
        """Where a change may be written: inside the workspace, not protected, not inside any git
        directory (a bare "*.git" repository's hooks would run on its next git command)."""
        target = resolve_safe_path(raw_path, self.config.workspace)
        if any(part.lower().endswith(".git") for part in Path(self._relative(target)).parts[:-1]):
            raise ToolError(f"{self._relative(target)} is inside a git directory; JARVIS does not write there.")
        return target

    def _remember_approval(self, key: str, value: tuple[str, str]) -> None:
        approvals = self.__dict__.setdefault("_approved_changes", {})
        approvals[key] = value
        while len(approvals) > APPROVALS_KEPT:  # declined prompts must not pile up
            approvals.pop(next(iter(approvals)))

    def _take_approval(self, key: str) -> tuple[str, str] | None:
        return self.__dict__.get("_approved_changes", {}).pop(key, None)

    def _decline_edit(self, args: dict[str, Any]) -> None:
        """The user said no: forget the approval prepared for this exact edit."""
        self._take_approval(self._change_key("edit", args["path"], args["old_text"], args["new_text"]))

    def _decline_create(self, args: dict[str, Any]) -> None:
        self._take_approval(self._change_key("create", args["path"], args["content"]))

    def _edit_plan(self, args: dict[str, Any]) -> tuple[Path, bytes, str, str]:
        target = self._writable(args["path"])
        data, text = self._read_editable(target)
        new_text = replace_once(text, args["old_text"], args["new_text"])
        return target, data, new_text, unified_diff(text, new_text, self._relative(target))

    @staticmethod
    def _change_key(*parts: str) -> str:
        return _sha("\x00".join(parts))

    def _describe_edit(self, args: dict[str, Any]) -> tuple[str, list[str], str]:
        target, data, new_text, diff = self._edit_plan(args)
        check_reviewable(diff)
        # Bind the approval to the file exactly as shown: if it changes before the write, nothing is written.
        self._remember_approval(self._change_key("edit", args["path"], args["old_text"], args["new_text"]),
                                (str(target), _sha(data)))
        added, removed = _diff_counts(diff)
        relative = self._relative(target)
        return (f"Edit {relative}: {added} line(s) added, {removed} removed ({self._file_git_state(target)})."
                + change_warnings(relative, diff), [str(target)], diff)

    def _backup(self, target: Path, data: bytes) -> Path:
        """Keep the previous version outside the project, so any edit can be undone by hand."""
        root = self.config.backup_dir.expanduser()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        stamp = root / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        stamp.mkdir(mode=0o700, exist_ok=True)
        destination = stamp / self._relative(target)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_bytes(data)
        destination.chmod(0o600)
        # Prune only JARVIS's own timestamped folders, never anything else in that directory.
        backups = sorted(p for p in root.iterdir() if p.is_dir() and _BACKUP_STAMP.fullmatch(p.name))
        for old in backups[:-BACKUPS_KEPT]:
            shutil.rmtree(old, ignore_errors=True)
        return destination

    def edit_workspace_file(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        approved = self._take_approval(self._change_key("edit", path, old_text, new_text))
        if approved is None:  # writes happen only for a change the user was shown and approved
            raise ToolError("This edit was not shown to the user for approval, so nothing was written.")
        changed = ToolError(f"{path} changed after the edit was approved. Nothing was written; "
                            "read the file again and propose the edit afresh.")
        try:
            target, data, updated, diff = self._edit_plan({"path": path, "old_text": old_text, "new_text": new_text})
        except ToolError:
            target = self._writable(path)
            try:
                current = target.read_bytes()
            except OSError:
                current = b""
            if approved != (str(target), _sha(current)):
                raise changed from None  # the edit no longer fits because the file changed
            raise
        # Check the very bytes the new content is built from, so what is written follows what was shown.
        if approved != (str(target), _sha(data)):
            raise changed
        check_reviewable(diff)
        git_state = self._file_git_state(target)
        backup = self._backup(target, data)
        try:
            write_text_atomically(target, updated, mode=target.stat().st_mode & 0o7777)
        except OSError as exc:
            raise ToolError(f"Could not write {self._relative(target)}: {exc.strerror or exc}") from None
        verified = target.read_bytes() == updated.encode("utf-8")
        result: dict[str, Any] = {
            "path": self._relative(target), "applied": True, "verified": verified,
            "diff": redact(diff)[:MAX_DIFF_CHARS], "git": git_state, "backup": str(backup),
            "next_step": "Run the project's tests with run_project_tests to check the change.",
        }
        if not verified:
            result.update(status=STATUS_UNVERIFIED, message="The file was written but its contents could not be confirmed.")
        return result

    def _create_plan(self, args: dict[str, Any]) -> tuple[Path, str]:
        target = self._writable(args["path"])
        if target.exists() or target.is_symlink():
            raise ToolError(f"{self._relative(target)} already exists; use edit_workspace_file to change it.")
        content = args["content"]
        if len(content) > MAX_NEW_FILE_CHARS or "\x00" in content:
            raise ToolError("New files must be text, at most 200,000 characters.")
        return target, unified_diff("", content, self._relative(target))

    def _describe_create(self, args: dict[str, Any]) -> tuple[str, list[str], str]:
        target, diff = self._create_plan(args)
        check_reviewable(diff)
        self._remember_approval(self._change_key("create", args["path"], args["content"]), (str(target), "absent"))
        relative = self._relative(target)
        return (f"Create {relative} ({len(args['content'].splitlines())} lines)." + change_warnings(relative, diff),
                [str(target)], diff)

    def create_workspace_file(self, path: str, content: str) -> dict[str, Any]:
        approved = self._take_approval(self._change_key("create", path, content))
        if approved is None:
            raise ToolError("This new file was not shown to the user for approval, so nothing was written.")
        existing = self._writable(path)
        if existing.exists() or existing.is_symlink():  # someone created it while the prompt was open
            raise ToolError(f"{self._relative(existing)} appeared while waiting for approval; nothing was overwritten.")
        target, diff = self._create_plan({"path": path, "content": content})
        check_reviewable(diff)
        if approved[0] != str(target):
            raise ToolError("The target changed after the new file was approved. Nothing was written.")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomically(target, content, mode=0o644, exclusive=True)
        except FileExistsError:
            raise ToolError(f"{self._relative(target)} appeared while waiting for approval; nothing was overwritten.") from None
        except OSError as exc:
            raise ToolError(f"Could not create {self._relative(target)}: {exc.strerror or exc}") from None
        verified = target.read_text(encoding="utf-8") == content
        result: dict[str, Any] = {"path": self._relative(target), "created": True, "verified": verified,
                                  "diff": redact(diff)[:MAX_DIFF_CHARS],
                                  "git": self._file_git_state(target, backed_up=False),
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
