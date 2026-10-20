"""The conversation session: recent turns kept as plain messages."""

from __future__ import annotations



class Conversation:
    """Recent turns as plain user/assistant messages. Tool traffic stays inside each request,
    so the history never accumulates stale tool results or half-finished call structures."""

    def __init__(self, max_turns: int = 10, max_chars: int = 16000):
        self.max_turns = max_turns
        self.max_chars = max_chars
        self.turns: list[tuple[str, str]] = []
        self.summary_lines: list[str] = []

    def add(self, user_text: str, assistant_text: str) -> None:
        self.turns.append((user_text, assistant_text))
        self._trim()

    def _trim(self) -> None:
        while self.turns and (len(self.turns) > self.max_turns
                              or sum(len(u) + len(a) for u, a in self.turns) > self.max_chars):
            user_text, _ = self.turns.pop(0)
            self.summary_lines.append(f"- The user asked: {user_text[:120]}")
        self.summary_lines = self.summary_lines[-10:]

    @property
    def summary(self) -> str:
        return "\n".join(self.summary_lines)

    def messages(self) -> list[dict[str, str]]:
        result = []
        for user_text, assistant_text in self.turns:
            result.append({"role": "user", "content": user_text})
            result.append({"role": "assistant", "content": assistant_text})
        return result

    def clear(self) -> None:
        self.turns.clear()
        self.summary_lines.clear()
