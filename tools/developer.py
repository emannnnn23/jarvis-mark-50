"""Developer tools for repositories inside the workspace."""

from __future__ import annotations

import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from config.settings import ProjectCommand, ProjectRule
from core.audit import redact
from core.policy import Permission
from tools.filesystem import resolve_safe_path
from tools.linux_system import launch_detached, project_env, run_bounded, run_command
from tools.registry import STATUS_ERROR, STATUS_SUCCESS, UNTRUSTED_NOTICE, ToolError

OUTPUT_TAIL_CHARS = 4000  # failures are usually at the end of a test run's output


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
    def inspect_git_status(self, path: str) -> dict[str, Any]:
        target = resolve_safe_path(path, self.config.workspace)
        if not target.is_dir():
            raise ToolError(f"{path!r} is not a directory inside {self.config.workspace}.")
        workspace = self.config.workspace.expanduser().resolve()
        # Inherited GIT_* variables (GIT_DIR, GIT_WORK_TREE...) could point git elsewhere; drop them.
        # The ceiling stops git discovering a repository above the workspace (e.g. a dotfiles repo
        # in $HOME), and optional locks off means status never rewrites the index.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CEILING_DIRECTORIES": str(workspace.parent)})
        # Command-line -c settings override repository config, so a hostile repo cannot make
        # these read-only commands run fsmonitor, hooks, filter drivers or external diff programs.
        overrides = ["core.fsmonitor=false", "core.untrackedCache=false", "core.hooksPath=/dev/null"]
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
        status = run_command([*base, "status", "--short", "--branch"], timeout=15, env=env)
        if status.returncode != 0:
            raise ToolError(f"git status failed: {status.stderr.strip()[:300]}")
        diff = run_command([*base, "diff", "--stat", "--no-ext-diff", "--no-textconv"], timeout=15, env=env)
        return {"path": str(target), "status": STATUS_SUCCESS,
                "git_status": status.stdout[:3000], "diff_stat": diff.stdout[:3000] if diff.returncode == 0 else None}

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
