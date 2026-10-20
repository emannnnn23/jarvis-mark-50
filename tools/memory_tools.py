"""Tools that let the model read and change JARVIS's memory."""

from __future__ import annotations

from typing import Any

from tools.registry import STATUS_SUCCESS, ToolError


class MemoryTools:
    """Memory tool handlers. Mixed into JarvisTools, which provides self.config and self.memory."""

    # -- memory --------------------------------------------------------------
    def remember_fact(self, fact: str, priority: str = "normal") -> dict[str, Any]:
        outcome = self.memory.remember_fact(fact, source="user", priority=priority)
        if outcome["status"] == "duplicate":
            return {"status": STATUS_SUCCESS, "message": "Already remembered; nothing new was saved.",
                    "fact": outcome["fact"]}
        return {"message": "Saved to memory.", "fact": outcome["fact"]}

    def search_memory(self, query: str = "") -> dict[str, Any]:
        facts = self.memory.search_memory(query, limit=15)
        return {
            "profile": self.memory.profile(),
            "facts": [{"id": f["id"], "text": f["text"], "created_at": f.get("created_at")} for f in facts],
            "total_facts_stored": len(self.memory.list_facts()),
        }

    def update_fact(self, fact_id: str, new_text: str) -> dict[str, Any]:
        outcome = self.memory.update_fact(fact_id.strip(), new_text)
        return {"message": "Memory updated.", **outcome}

    def forget_fact(self, fact_id: str) -> dict[str, Any]:
        removed = self.memory.forget_fact(fact_id.strip())
        return {"message": "Memory deleted.", "removed": removed}

    def set_user_profile(self, user_name: str | None = None, location: str | None = None) -> dict[str, Any]:
        if user_name is None and location is None:
            raise ToolError("Provide user_name and/or location.")
        return {"message": "Profile updated.", "profile": self.memory.set_profile(user_name, location)}

    def _describe_fact_change(self, args: dict[str, Any], verb: str) -> tuple[str, list[str]]:
        fact = self.memory.find_fact(str(args.get("fact_id", "")).strip())
        current = repr(fact["text"]) if fact else "(no such memory)"
        operation = f"{verb} memory {args.get('fact_id')}: {current}"
        if "new_text" in args:
            operation += f" -> {args['new_text']!r}"
        return operation, [str(self.memory.path)]
