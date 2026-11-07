"""Logging, secret redaction and the audit trail."""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
import re
import sys
import unicodedata
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
#
# The obvious single regex, ((?:\w+[_-])*KEYWORD(?:[_-]\w+)*)SEP VALUE, backtracks quadratically on a
# long run like "a-a-a-..." (the prefix is retried from every segment). Redaction runs on every
# string the assistant logs or sends to the browser, so it is done in one linear pass instead:
# split the text into identifier chains, and look at the separator and value after each chain once.
_SECRET_KEYWORD = re.compile(
    r"(?:api[_ -]?key|secret|token|password|passwd|pwd|passphrase|access[_-]?key|private[_-]?key"
    r"|pin(?: code| number)?)",
    re.IGNORECASE,
)

# A maximal identifier such as x-auth-token or AWS_SECRET_ACCESS_KEY: segments joined by _ or -.
_IDENTIFIER_CHAIN = re.compile(r"[A-Za-z0-9]++(?:[_-][A-Za-z0-9]++)*+", re.IGNORECASE)

# What follows a credential name: the separator (":", "=", or " is ") and then the value.
_ASSIGNMENT_TAIL = re.compile(
    r"(?:[\"']?\s*[:=]\s*|\s+is\s+)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
    re.IGNORECASE,
)


def _names_a_secret(text: str, start: int, end: int, previous: tuple[int, int] | None, floor: int) -> bool:
    """Whether the identifier chain text[start:end] contains a credential keyword.

    A keyword is a whole segment (password) or two adjacent ones (api_key, access-key), and may also
    be split by a single space across two chains ("api key", "pin code", "pin number") when
    `previous` is the chain just before. Keywords inside a word (tokenizer, secretary) do not count."""
    parts = re.split("([_-])", text[start:end])
    segments, delimiters = parts[0::2], parts[1::2]
    if any(_SECRET_KEYWORD.fullmatch(segment) for segment in segments):
        return True
    if any(_SECRET_KEYWORD.fullmatch(f"{segments[i]}{delimiters[i]}{segments[i + 1]}")
           for i in range(len(delimiters))):
        return True
    if previous is not None and previous[1] == start - 1 and text[start - 1] == " ":
        before = re.split("[_-]", text[previous[0]:previous[1]])[-1]
        if previous[1] - len(before) >= floor and _SECRET_KEYWORD.fullmatch(f"{before} {segments[0]}"):
            return True
    return False


def _secret_values(text: str) -> list[tuple[int, int]]:
    """Spans of values assigned to credential-like names (password=x, "token": "x", pin is 1234)."""
    spans: list[tuple[int, int]] = []
    floor = 0  # end of the last value found: text inside it is not scanned again
    previous: tuple[int, int] | None = None
    for chain in _IDENTIFIER_CHAIN.finditer(text):
        start, end = chain.span()
        if start >= floor and _names_a_secret(text, start, end, previous, floor):
            tail = _ASSIGNMENT_TAIL.match(text, end)
            if tail:
                spans.append(tail.span(1))
                floor = tail.end(1)
        previous = (start, end)
    return spans


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_text(text: Any) -> str:
    """Replace unencodable characters (lone surrogates) so text can always be saved and printed."""
    return str(text).encode("utf-8", "replace").decode("utf-8")


def safe_display(text: Any) -> str:
    """Text the model wrote, made safe for the terminal: no escape sequences or control codes."""
    return _CONTROL_CHARS.sub("", clean_text(text))


def _is_harmless(text: str, index: int) -> bool:
    """Invisible characters that legitimately appear in text: a byte-order mark opening a line, and
    the zero-width joiner inside an emoji sequence."""
    char = text[index]
    if char == "\ufeff":
        return index == 0 or text[index - 1] in "\n+ -"
    if char == "\u200d":
        neighbours = text[index - 1:index] + text[index + 1:index + 2]
        return len(neighbours) == 2 and all(unicodedata.category(c) == "So" for c in neighbours)
    return False


def reveal_invisible(text: str) -> tuple[str, int]:
    """Show invisible, text-direction and control characters (zero-width spaces, bidi overrides,
    line and paragraph separators, form feeds, escapes) as \\u{...}, so code under review reads
    exactly as it behaves. Newlines and tabs are kept. Returns the revealed text and how many
    suspicious characters there were (harmless ones, such as a leading byte-order mark, are shown
    but not counted)."""
    out, count = [], 0
    for index, char in enumerate(text):
        category = unicodedata.category(char)
        if category in ("Cf", "Zl", "Zp") or (category == "Cc" and char not in "\n\t"):
            out.append(f"\\u{{{ord(char):04x}}}")
            count += not _is_harmless(text, index)
        else:
            out.append(char)
    return "".join(out), count


_CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){13,19}\b")


_HIGH_ENTROPY_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b")


def redact(text: Any) -> str:
    """Replace credentials and key material in text with [REDACTED]."""
    text = str(text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    pieces, cursor = [], 0
    for start, end in _secret_values(text):
        pieces += [text[cursor:start], "[REDACTED]"]
        cursor = end
    return "".join(pieces) + text[cursor:]


def contains_secret(text: str) -> bool:
    """Heuristic check for content that must never be written to memory."""
    if any(p.search(text) for p in _SECRET_PATTERNS) or _secret_values(text):
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
