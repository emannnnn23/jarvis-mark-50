"""Opening the Mission Control page in a browser without leaking the session token.

`webbrowser.open(url)` runs a helper such as xdg-open with the URL on its command line, where every
local user can read it in `ps`. So the tokenised link is written to a private (mode 0600) redirect file
and only that file's `file://` path is passed to the browser. The token stays in the URL fragment,
which browsers never send to a server, and the page removes it from the address bar on load."""

from __future__ import annotations

import atexit
import html
import logging
import os
import tempfile
import webbrowser
from pathlib import Path
from threading import Timer


log = logging.getLogger("jarvis.mission_control.launch")


# Long enough for a cold browser start to read the file, short enough that it does not linger.
DELETE_AFTER = 10.0


def _runtime_dir() -> str:
    """$XDG_RUNTIME_DIR (per-user, mode 0700, in memory) when it exists, else the system temp dir."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime and os.path.isdir(runtime):
        return runtime
    return tempfile.gettempdir()


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.debug("could not remove the Mission Control redirect file: %s", exc.strerror)


def _redirect_page(url: str) -> str:
    target = html.escape(url, quote=True)
    return ("<!doctype html>\n<html><head><meta charset=\"utf-8\">\n"
            "<meta name=\"referrer\" content=\"no-referrer\">\n"
            f"<meta http-equiv=\"refresh\" content=\"0;url={target}\">\n"
            "<title>JARVIS Mission Control</title></head>\n"
            f"<body><p>Opening <a href=\"{target}\" rel=\"noreferrer\">JARVIS Mission Control</a>...</p></body></html>\n")


def open_browser_securely(url: str) -> bool:
    """Open `url` (which carries the token) via a private redirect file. Returns webbrowser's answer.

    Never logs the URL. The file is removed after DELETE_AFTER seconds, at exit, or at once when no
    browser could be started."""
    try:
        # mkstemp creates the file with O_EXCL and mode 0600, so no other user can read or swap it.
        fd, path = tempfile.mkstemp(prefix="jarvis-mission-", suffix=".html", dir=_runtime_dir())
    except OSError as exc:
        log.warning("could not create the Mission Control redirect file: %s", exc.strerror)
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_redirect_page(url))
        os.chmod(path, 0o600)
    except OSError as exc:
        log.warning("could not write the Mission Control redirect file: %s", exc.strerror)
        _remove(path)
        return False
    try:
        opened = bool(webbrowser.open(Path(path).as_uri()))
    except Exception as exc:  # noqa: BLE001 - a missing browser must never stop JARVIS
        log.warning("could not open a browser: %s", type(exc).__name__)
        opened = False
    if not opened:
        _remove(path)
        return False
    atexit.register(_remove, path)
    timer = Timer(DELETE_AFTER, _remove, args=(path,))
    timer.daemon = True
    timer.start()
    return True
