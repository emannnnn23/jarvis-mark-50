"""JSON-backed memory. Its public methods are the interface a SQLite store would replace."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from core.audit import clean_text, contains_secret, log, redact, safe_display


MAX_FACTS = 500


MAX_FACT_LENGTH = 500


MAX_CONVERSATIONS = 50


MAX_CONVERSATION_TEXT = 500


_STOPWORDS = frozenset(
    "a an and are as at be but by do does for from had has have he her his how i if in is it its "
    "me my of on or our she so that the their them they this to was we were what when where which "
    "who why will with you your about remember know tell please jarvis sir".split()
)


class MemoryStoreError(Exception):
    """A memory operation failed; the message is safe to show the user."""


class MemoryCorruptedError(MemoryStoreError):
    """The memory file exists but cannot be parsed. It is never overwritten automatically."""


def empty_memory() -> dict[str, Any]:
    return {"user_name": None, "location": None, "facts": [], "conversations": []}


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_fact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".!").strip().lower()


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    result = set()
    for word in words:
        if word in _STOPWORDS or len(word) < 2:
            continue
        if len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        result.add(word)
    return result


def normalize_memory(data: dict[str, Any]) -> dict[str, Any]:
    """Fill in missing keys and upgrade legacy entries without dropping unknown data."""
    memory = dict(data)
    for key, default in empty_memory().items():
        memory.setdefault(key, default)
    facts = []
    for item in memory.get("facts") or []:
        if isinstance(item, str):  # legacy: plain-string facts
            item = {"text": item}
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        item = dict(item)
        item.setdefault("id", "f" + uuid.uuid4().hex[:6])
        item.setdefault("created_at", None)
        item.setdefault("updated_at", None)
        item.setdefault("source", "user")
        item.setdefault("priority", "normal")
        facts.append(item)
    memory["facts"] = facts
    if not isinstance(memory.get("conversations"), list):
        memory["conversations"] = []
    return memory


def load_memory(path: Path) -> dict[str, Any]:
    """Read memory from disk. A missing or empty file yields a fresh memory."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty_memory()
    except OSError as exc:
        raise MemoryStoreError(f"Cannot read memory file {path}: {exc}") from exc
    if not raw.strip():
        return empty_memory()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MemoryCorruptedError(
            f"Memory file {path} is not valid JSON (line {exc.lineno}, column {exc.colno})."
        ) from exc
    if not isinstance(data, dict):
        raise MemoryCorruptedError(f"Memory file {path} does not contain a JSON object.")
    return normalize_memory(data)


def save_memory(memory: dict[str, Any], path: Path) -> None:
    """Write memory atomically: temp file in the same directory, fsync, then rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(memory, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


class MemoryStore:
    """JSON-backed memory. The public methods are the interface a SQLite store would replace."""

    def __init__(self, path: Path, max_conversations: int = MAX_CONVERSATIONS):
        self.path = Path(path)
        self.max_conversations = max_conversations
        self.data = empty_memory()
        self.corruption_error: str | None = None
        self._lock = threading.RLock()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                self.data = load_memory(self.path)
                self.corruption_error = None
            except MemoryCorruptedError as exc:
                # Keep the broken file untouched; run in-memory only until the user decides.
                self.data = empty_memory()
                self.corruption_error = str(exc)
                log.error("Memory corrupted: %s", exc)

    @property
    def writable(self) -> bool:
        return self.corruption_error is None

    def save(self) -> None:
        with self._lock:
            if not self.writable:
                raise MemoryStoreError(
                    f"{self.corruption_error} I won't overwrite it. Repair the file, or type "
                    "'/memory reset' to back it up and start a fresh memory."
                )
            try:
                save_memory(self.data, self.path)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise MemoryStoreError(f"Could not save memory to {self.path}: {exc}") from exc

    def reset_corrupted(self) -> Path:
        """Move a corrupted memory file aside and start fresh. Returns the backup path."""
        with self._lock:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
            if self.path.exists():
                os.replace(self.path, backup)
            self.corruption_error = None
            self.data = empty_memory()
            self.save()
            return backup

    # -- facts -------------------------------------------------------------
    def list_facts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(f) for f in self.data["facts"]]

    def find_fact(self, fact_id: str) -> dict[str, Any] | None:
        with self._lock:
            for fact in self.data["facts"]:
                if fact["id"] == fact_id:
                    return fact
        return None

    def _check_fact_text(self, text: Any) -> str:
        if not isinstance(text, str) or not text.strip():
            raise MemoryStoreError("A fact must be a non-empty piece of text.")
        text = re.sub(r"\s+", " ", safe_display(text)).strip()
        if len(text) > MAX_FACT_LENGTH:
            raise MemoryStoreError(f"That fact is too long to store (limit {MAX_FACT_LENGTH} characters).")
        if contains_secret(text):
            raise MemoryStoreError(
                "That looks like a password, key, token or card number. I don't store secrets in memory."
            )
        return text

    def _new_id(self) -> str:
        existing = {f["id"] for f in self.data["facts"]}
        while True:
            fact_id = "f" + uuid.uuid4().hex[:6]
            if fact_id not in existing:
                return fact_id

    def remember_fact(self, text: str, source: str = "user", priority: str = "normal") -> dict[str, Any]:
        """Store a fact. Returns {"status": "saved"|"duplicate", "fact": {...}}."""
        with self._lock:
            text = self._check_fact_text(text)
            normalized = _normalize_fact(text)
            for fact in self.data["facts"]:
                if _normalize_fact(fact["text"]) == normalized:
                    return {"status": "duplicate", "fact": dict(fact)}
            if len(self.data["facts"]) >= MAX_FACTS:
                raise MemoryStoreError(f"Memory is full ({MAX_FACTS} facts). Please forget some first.")
            fact = {
                "id": self._new_id(),
                "text": text,
                "created_at": _now_iso(),
                "updated_at": None,
                "source": source,
                "priority": priority if priority in ("normal", "high") else "normal",
            }
            self.data["facts"].append(fact)
            try:
                self.save()
            except MemoryStoreError:
                self.data["facts"].remove(fact)
                raise
            return {"status": "saved", "fact": dict(fact)}

    def update_fact(self, fact_id: str, new_text: str) -> dict[str, Any]:
        with self._lock:
            fact = self.find_fact(fact_id)
            if fact is None:
                raise MemoryStoreError(f"No memory with id {fact_id!r}.")
            new_text = self._check_fact_text(new_text)
            normalized = _normalize_fact(new_text)
            for other in self.data["facts"]:
                if other is not fact and _normalize_fact(other["text"]) == normalized:
                    raise MemoryStoreError(f"That text is already stored as memory {other['id']}.")
            previous = dict(fact)
            fact["text"], fact["updated_at"] = new_text, _now_iso()
            try:
                self.save()
            except MemoryStoreError:
                fact.clear()
                fact.update(previous)
                raise
            return {"previous_text": previous["text"], "fact": dict(fact)}

    def forget_fact(self, fact_id: str) -> dict[str, Any]:
        with self._lock:
            fact = self.find_fact(fact_id)
            if fact is None:
                raise MemoryStoreError(f"No memory with id {fact_id!r}.")
            index = self.data["facts"].index(fact)
            self.data["facts"].pop(index)
            try:
                self.save()
            except MemoryStoreError:
                self.data["facts"].insert(index, fact)
                raise
            return dict(fact)

    def search_memory(self, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """Facts matching any keyword in the query; an empty query lists the newest facts."""
        with self._lock:
            facts = list(self.data["facts"])
        if not query.strip():
            return [dict(f) for f in reversed(facts)][:limit]
        wanted = _tokens(query)
        lowered = query.lower().strip()
        scored = []
        for position, fact in enumerate(facts):
            overlap = len(wanted & _tokens(fact["text"]))
            if overlap or lowered in fact["text"].lower():
                scored.append((overlap, position, fact))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [dict(item[2]) for item in scored[:limit]]

    def get_relevant_memories(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Up to `limit` facts for the prompt: relevant first, then high-priority, then recent."""
        with self._lock:
            facts = list(self.data["facts"])
        wanted = _tokens(query)
        scored = []
        for position, fact in enumerate(facts):
            relevance = len(wanted & _tokens(fact["text"]))
            priority = 1 if fact.get("priority") == "high" else 0
            scored.append((relevance, priority, position, fact))
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return [dict(item[3]) for item in scored[:limit]]

    # -- profile and conversations -----------------------------------------
    def profile(self) -> dict[str, Any]:
        with self._lock:
            return {"user_name": self.data.get("user_name"), "location": self.data.get("location")}

    def set_profile(self, user_name: str | None = None, location: str | None = None) -> dict[str, Any]:
        with self._lock:
            previous = self.profile()
            for key, value in (("user_name", user_name), ("location", location)):
                if value is None:
                    continue
                value = safe_display(value).strip() or None
                if value and contains_secret(value):
                    raise MemoryStoreError("That value looks like a secret; not stored.")
                self.data[key] = value
            try:
                self.save()
            except MemoryStoreError:
                self.data.update(previous)
                raise
            return self.profile()

    def add_conversation(self, user_text: str, assistant_text: str) -> None:
        """Keep a short, redacted, bounded log of exchanges. Silently skipped if read-only."""
        with self._lock:
            if not self.writable:
                return
            self.data["conversations"].append({
                "timestamp": _now_iso(),
                "user": redact(clean_text(user_text))[:MAX_CONVERSATION_TEXT],
                "assistant": redact(clean_text(assistant_text))[:MAX_CONVERSATION_TEXT],
            })
            overflow = len(self.data["conversations"]) - self.max_conversations
            if overflow > 0:
                del self.data["conversations"][:overflow]
            try:
                self.save()
            except MemoryStoreError as exc:
                log.warning("Could not save conversation: %s", exc)
