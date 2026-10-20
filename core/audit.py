"""Logging, secret redaction and the audit trail."""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from config.settings import Config


log = logging.getLogger("jarvis")


audit_log = logging.getLogger("jarvis.audit")


_SECRET_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]


# Matches DB_PASSWORD=x, "api_key": "x", AWS_SECRET_ACCESS_KEY: x, "my password is x", ...
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_ -]?key|secret|token|password|passwd|pwd|passphrase|access[_-]?key|private[_-]?key|pin(?: code| number)?)"
    r"(?:[_-][A-Za-z0-9]+)*)"
    r"([\"']?\s*[:=]\s*|\s+is\s+)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_text(text: Any) -> str:
    """Replace unencodable characters (lone surrogates) so text can always be saved and printed."""
    return str(text).encode("utf-8", "replace").decode("utf-8")


def safe_display(text: Any) -> str:
    """Text the model wrote, made safe for the terminal: no escape sequences or control codes."""
    return _CONTROL_CHARS.sub("", clean_text(text))


_CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){13,19}\b")


_HIGH_ENTROPY_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b")


def redact(text: Any) -> str:
    """Replace credentials and key material in text with [REDACTED]."""
    text = str(text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def contains_secret(text: str) -> bool:
    """Heuristic check for content that must never be written to memory."""
    if any(p.search(text) for p in _SECRET_PATTERNS) or _SECRET_ASSIGNMENT.search(text):
        return True
    return bool(_CARD_NUMBER.search(text) or _HIGH_ENTROPY_TOKEN.search(text))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(config: Config) -> Path | None:
    """Log to rotating files under the user's data directory. Returns the log dir, or None."""
    log.setLevel(config.log_level)
    log.propagate = False
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False
    for logger in (log, audit_log):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
    # httpx logs every request at INFO; keep it quiet. Headers (and the key) are never logged.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        main_handler = logging.handlers.RotatingFileHandler(
            config.log_dir / "jarvis.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        audit_handler = logging.handlers.RotatingFileHandler(
            config.log_dir / "audit.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
    except OSError as exc:
        log.addHandler(logging.NullHandler())
        audit_log.addHandler(logging.NullHandler())
        print(f"Warning: cannot write logs to {config.log_dir} ({exc}); logging disabled.", file=sys.stderr)
        return None
    main_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    audit_handler.setFormatter(logging.Formatter("%(message)s"))
    for handler in (main_handler, audit_handler):
        handler.addFilter(RedactingFilter())
    log.addHandler(main_handler)
    audit_log.addHandler(audit_handler)
    return config.log_dir


def audit(event: dict[str, Any]) -> None:
    """Write one JSON line to the audit trail. Values are redacted and kept short."""
    entry = {"timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    entry.update({k: redact(v)[:300] if isinstance(v, str) else v for k, v in event.items()})
    audit_log.info(json.dumps(entry, ensure_ascii=False, default=str))
