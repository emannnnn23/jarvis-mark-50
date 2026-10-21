"""Workspace-scoped file access with path validation, plus JARVIS's notes folder."""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from core.audit import redact
from core.policy import Permission
from tools.registry import STATUS_UNVERIFIED, ToolError, UNTRUSTED_NOTICE


def _human_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024 or unit == "TB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= 1024
    return f"{count:.1f} TB"


SENSITIVE_DIR_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", ".pki",
    ".mozilla", "google-chrome", "chromium", "BraveSoftware", "keyrings", ".git",
})


SENSITIVE_FILE_PATTERNS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", ".pgpass", ".git-credentials",
    ".npmrc", ".pypirc", "credentials", "credentials.*", "secrets.*", "*.secret", "master.key",
    ".envrc", "*_history", ".htpasswd",
)


SAFE_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})


def is_sensitive_name(name: str) -> bool:
    if name in SAFE_ENV_TEMPLATES:
        return False
    if name in SENSITIVE_DIR_NAMES:
        return True
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in SENSITIVE_FILE_PATTERNS)


def resolve_safe_path(raw: Any, root: Path) -> Path:
    """Resolve `raw` (relative to `root` unless absolute), following symlinks, and make sure
    the result stays inside `root` and does not touch a sensitive file or directory."""
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("A path is required.")
    if "\x00" in raw:
        raise ToolError("Invalid path.")
    root_resolved = Path(root).expanduser().resolve()
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ToolError(f"Access denied: {raw!r} is outside the approved directory {root_resolved}.")
    for part in resolved.relative_to(root_resolved).parts:
        if is_sensitive_name(part):
            raise ToolError(f"Access denied: {raw!r} is a protected file or directory ({part}).")
    return resolved


def _slugify(title: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._ -]+", "", title).strip().replace(" ", "-")
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    return slug[:80] or "note"


class FileTools:
    """Workspace file and notes tool handlers. Mixed into JarvisTools, which provides self.config and self.memory."""

    # -- files ---------------------------------------------------------------
    def list_workspace_files(self, path: str = ".") -> dict[str, Any]:
        target = resolve_safe_path(path, self.config.workspace)
        if not target.exists():
            raise ToolError(f"{path!r} does not exist.")
        if not target.is_dir():
            raise ToolError(f"{path!r} is a file, not a directory.")
        entries, hidden = [], 0
        try:
            children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            raise ToolError(f"Permission denied reading {path!r}.") from None
        for child in children:
            if is_sensitive_name(child.name):
                hidden += 1
                continue
            if len(entries) >= 200:
                break
            entry: dict[str, Any] = {"name": child.name, "type": "dir" if child.is_dir() else "file"}
            if entry["type"] == "file":
                try:
                    entry["size"] = _human_bytes(child.stat().st_size)
                except OSError:
                    pass
            entries.append(entry)
        result = {"path": str(target), "entries": entries, "total": len(children)}
        if hidden:
            result["protected_entries_hidden"] = hidden
        if len(children) - hidden > len(entries):
            result["truncated"] = True
        return result

    def read_workspace_file(self, path: str, max_chars: int = 6000) -> dict[str, Any]:
        target = resolve_safe_path(path, self.config.workspace)
        if not target.is_file():
            raise ToolError(f"{path!r} is not a readable file inside {self.config.workspace}.")
        try:
            size = target.stat().st_size
            with target.open("rb") as handle:
                raw = handle.read(200_000)
        except PermissionError:
            raise ToolError(f"Permission denied reading {path!r}.") from None
        if b"\x00" in raw[:8192]:
            raise ToolError(f"{path!r} looks like a binary file; only text files can be read.")
        text = redact(raw.decode("utf-8", errors="replace"))
        truncated = size > len(raw) or len(text) > max_chars
        return {"path": str(target), "size": _human_bytes(size), "truncated": truncated,
                "content": text[:max_chars], "notice": UNTRUSTED_NOTICE}

    def _note_path(self, title: str) -> Path:
        notes_dir = self.config.notes_dir.expanduser().resolve()
        path = (notes_dir / f"{_slugify(title)}.md").resolve()
        if path.parent != notes_dir:
            raise ToolError("Invalid note title.")
        return path

    def _note_permission(self, args: dict[str, Any]) -> Permission:
        try:
            exists = self._note_path(args.get("title", "")).exists()
        except ToolError:
            exists = False
        return Permission.SENSITIVE_ACTION if exists and args.get("overwrite") else Permission.CONTROLLED_ACTION

    def _describe_note(self, args: dict[str, Any]) -> tuple[str, list[str]]:
        path = self._note_path(args.get("title", ""))
        verb = "Overwrite existing note" if path.exists() else "Create note"
        return f"{verb} ({len(args.get('content', ''))} characters)", [str(path)]

    def create_note(self, title: str, content: str, overwrite: bool = False) -> dict[str, Any]:
        path = self._note_path(title)
        existed = path.exists()
        if existed and not overwrite:
            raise ToolError(f"A note already exists at {path}. Ask the user whether to overwrite it or pick another title.")
        body = content if content.lstrip().startswith("#") else f"# {title.strip()}\n\n{content}"
        if not body.endswith("\n"):
            body += "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".note.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(tmp_name, path)
        except OSError as exc:
            raise ToolError(f"Could not write the note: {exc.strerror or exc}") from None
        if path.read_text(encoding="utf-8") != body:
            return {"status": STATUS_UNVERIFIED, "path": str(path), "message": "The note was written but could not be verified."}
        return {"path": str(path), "overwritten": existed, "bytes": len(body.encode("utf-8"))}
