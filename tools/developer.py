"""Developer tools for repositories inside the workspace."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Any

from tools.filesystem import resolve_safe_path
from tools.linux_system import launch_detached, run_command
from tools.registry import STATUS_SUCCESS, ToolError


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
    def git_status(self, path: str) -> dict[str, Any]:
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
