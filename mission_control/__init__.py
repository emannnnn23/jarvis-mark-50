"""Mission Control: the live 3D browser view of JARVIS (protocol, state tracking, server).

This package's own import is stdlib-only, so `jarvis.py` and `--check` can name the build paths without
pulling in aiohttp; the server module (the only one that imports aiohttp) is imported lazily."""

from __future__ import annotations

from pathlib import Path


WEB_DIR = Path(__file__).resolve().parent / "web"


# Where `npm run build` writes the page; it is git-ignored, so a fresh checkout has none.
DIST_DIR = WEB_DIR / "dist"


# Shown by the "not built" page and by --check: run from the project root.
BUILD_COMMAND = "cd mission_control/web && npm ci && npm run build"
