"""Derives JARVIS's one headline state, and the health of its six subsystems, from the event stream.

The browser must not guess what JARVIS is doing from raw events, so the backend decides: this tracker
folds bus events into a single state (error > tool > processing > speaking > listening > idle) plus a
small status block per 3D node (llm, memory, tools, system, voice, network). It is deliberately pure:
no I/O, no threads and injectable clocks, so the server can call `apply()` per event and `tick()` once a
second on one event loop, and tests can drive time by hand.

`offline` for the whole core is not decided here; the browser shows it when its socket drops."""

from __future__ import annotations

import copy
import logging
import time
from typing import Any
from typing import Callable


log = logging.getLogger("jarvis.mission_control.state")


# Tool statuses that mean the tool is still occupying JARVIS, and the ones that end its turn.
ACTIVE_TOOL_STATUSES = frozenset({"running", "awaiting_confirmation"})
FINAL_TOOL_STATUSES = frozenset({"success", "error", "denied", "partial", "unverified"})

# voice.input phases that show the user is being heard, and the one that means speech is being decoded.
LISTENING_PHASES = frozenset({"wake_detected", "collecting", "recording"})
TRANSCRIBING_PHASE = "transcribing"

# session.start memory_status (ok | corrupted | disabled) -> memory node status.
MEMORY_STATUS = {"ok": "online", "corrupted": "degraded", "disabled": "unavailable"}

# llm kinds that mean Groq could not be reached at all (everything else is a degraded service).
LLM_OFFLINE_KINDS = frozenset({"network"})


class StateTracker:
    """Folds events into the current state and per-subsystem status.

    `apply(ev)` returns a `jarvis.state` payload `{"state", "detail", "since"}` only when the headline
    state or its detail changed (a second tool starting keeps state "tool" but changes the detail);
    `since` is when the state itself began, so it does not move on a detail-only change. `tick()` does
    the same for time-driven changes, which is only the end of the error hold."""

    ERROR_HOLD = 4.0
    RECENT_TOOLS = 5

    def __init__(self, clock: Callable[[], float] = time.monotonic, *,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._request_active = False
        self._tools: dict[str, str] = {}  # tool name -> running | awaiting_confirmation, newest last
        self._transcribing = False
        self._listening = False
        self._speaking = False
        self._error_until: float | None = None
        self._error_detail: str | None = None
        self._state = "idle"
        self._detail: str | None = None
        self._since = int(self._wall_clock() * 1000)
        self._subsystems: dict[str, dict[str, Any]] = {
            "llm": {"status": "unknown", "model": None, "last_duration_ms": None,
                    "last_status": None, "last_error_kind": None},
            "memory": {"status": "unknown", "last_op": None, "count": None},
            "tools": {"status": "unknown", "active": [], "recent": []},
            "system": {"status": "unknown", "cpu_percent": None, "memory_percent": None,
                       "disk_percent": None},
            "voice": {"status": "unknown", "mode": None, "input_phase": None, "speaking": False},
            "network": {"status": "unknown", "groq": None, "source": None, "latency_ms": None,
                        "checked_at": None},
        }

    # --- public API -------------------------------------------------------------------------------

    def apply(self, ev: dict[str, Any]) -> dict[str, Any] | None:
        """Fold one bus event in. Never raises: a malformed event is ignored."""
        if not isinstance(ev, dict):
            return None
        event_type = ev.get("type")
        handler = self._HANDLERS.get(event_type) if isinstance(event_type, str) else None
        if handler is not None:
            try:
                handler(self, ev)
            except Exception:  # noqa: BLE001 - one odd event must not stop the state feed
                log.debug("ignored malformed %r event", ev.get("type"), exc_info=True)
        return self._commit()

    def tick(self) -> dict[str, Any] | None:
        """Advance time-driven state (the error hold expiring). Cheap enough to call every second."""
        return self._commit()

    def snapshot(self) -> dict[str, Any]:
        """Headline state plus one status block per node; a deep copy, safe to serialise and mutate.

        Statuses are online | degraded | offline | unknown | unavailable. Blocks stay "unknown" until
        a real event for that subsystem arrives, so the UI never shows invented health."""
        state, detail = self._resolve()
        subsystems = copy.deepcopy(self._subsystems)
        subsystems["tools"]["active"] = list(self._tools)
        subsystems["voice"]["speaking"] = self._speaking
        return {"state": state, "detail": detail,
                "since": self._since if state == self._state else int(self._wall_clock() * 1000),
                "subsystems": subsystems}

    def set_subsystem(self, name: str, **fields: Any) -> None:
        """Merge fields into one node's block, for facts that no bus event carries (for example
        `set_subsystem("system", status="unavailable", reason="psutil not installed")`).

        A later real event for that subsystem overwrites the status again (system.metrics -> online)."""
        if name not in self._subsystems:
            raise ValueError(f"unknown subsystem: {name!r}")
        self._subsystems[name].update(fields)

    # --- headline state ---------------------------------------------------------------------------

    def _resolve(self) -> tuple[str, str | None]:
        """The state the current facts imply, without storing it (so snapshot() has no side effects)."""
        if self._error_until is not None and self._clock() < self._error_until:
            return "error", self._error_detail
        if self._tools:
            if "awaiting_confirmation" in self._tools.values():
                return "tool", "awaiting approval"
            return "tool", next(reversed(self._tools))
        if self._request_active or self._transcribing:
            return "processing", None
        if self._speaking:
            return "speaking", None
        if self._listening:
            return "listening", None
        return "idle", None

    def _commit(self) -> dict[str, Any] | None:
        state, detail = self._resolve()
        if self._error_until is not None and state != "error":
            self._error_until = self._error_detail = None
        if state == self._state and detail == self._detail:
            return None
        if state != self._state:
            self._state = state
            self._since = int(self._wall_clock() * 1000)
        self._detail = detail
        return {"state": state, "detail": detail, "since": self._since}

    def _hold_error(self, detail: str | None) -> None:
        """Start or extend the error hold. An error already showing keeps its detail: the llm.error
        message that precedes request.end is more useful than the bare error kind that follows."""
        if self._error_until is None or self._clock() >= self._error_until:
            self._error_detail = detail
        self._error_until = self._clock() + self.ERROR_HOLD

    # --- event handlers (each updates facts only; _commit decides what changed) --------------------

    def _on_session_start(self, ev: dict[str, Any]) -> None:
        if ev.get("model"):
            self._subsystems["llm"]["model"] = str(ev["model"])
        if ev.get("voice_available") is False:
            self._subsystems["voice"]["status"] = "unavailable"
        elif ev.get("voice_available") is True and self._subsystems["voice"]["status"] == "unknown":
            self._subsystems["voice"]["status"] = "online"
        memory_status = MEMORY_STATUS.get(ev.get("memory_status"))  # type: ignore[arg-type]
        if memory_status is not None:
            self._subsystems["memory"]["status"] = memory_status

    def _on_session_end(self, ev: dict[str, Any]) -> None:
        self._request_active = False
        self._tools.clear()
        self._transcribing = self._listening = self._speaking = False
        self._error_until = self._error_detail = None

    def _on_request_start(self, ev: dict[str, Any]) -> None:
        self._request_active = True
        self._tools.clear()
        self._error_until = self._error_detail = None

    def _on_request_end(self, ev: dict[str, Any]) -> None:
        self._request_active = False
        self._tools.clear()  # a request that ended cannot still be running a tool
        if ev.get("status") == "error":
            self._hold_error(str(ev.get("error_kind") or "request failed"))

    def _on_llm_call(self, ev: dict[str, Any]) -> None:
        llm = self._subsystems["llm"]
        if ev.get("model"):
            llm["model"] = str(ev["model"])
        if ev.get("phase") != "end" or "ok" not in ev:
            return
        if isinstance(ev.get("duration_ms"), (int, float)):
            llm["last_duration_ms"] = ev["duration_ms"]
        if ev["ok"]:
            llm.update(status="online", last_status="success", last_error_kind=None)
        else:
            llm.update(status="degraded", last_status="error", last_error_kind=ev.get("error_kind") or None)

    def _on_llm_retry(self, ev: dict[str, Any]) -> None:
        self._subsystems["llm"].update(status="degraded", last_status="retry")

    def _on_llm_error(self, ev: dict[str, Any]) -> None:
        kind = str(ev.get("kind") or "unknown")
        status = "offline" if kind in LLM_OFFLINE_KINDS else "degraded"
        self._subsystems["llm"].update(status=status, last_status="error", last_error_kind=kind)
        if ev.get("retryable") is False:
            self._hold_error(str(ev.get("message") or kind))

    def _on_tool(self, ev: dict[str, Any]) -> None:
        tool, status = str(ev.get("tool") or ""), ev.get("status")
        tools = self._subsystems["tools"]
        if status in ACTIVE_TOOL_STATUSES and tool:
            self._tools.pop(tool, None)  # re-insert so the newest tool is last
            self._tools[tool] = status
            if tools["status"] == "unknown":
                tools["status"] = "online"
        elif status in FINAL_TOOL_STATUSES:
            self._tools.pop(tool, None)
            tools["recent"] = [*tools["recent"], {"tool": tool, "status": status}][-self.RECENT_TOOLS:]
            tools["status"] = "degraded" if status == "error" else "online"

    def _on_memory(self, ev: dict[str, Any]) -> None:
        count = ev.get("count")
        self._subsystems["memory"].update(
            status="online", last_op=ev.get("op"),
            count=count if isinstance(count, int) and not isinstance(count, bool) else None)

    def _on_voice_output(self, ev: dict[str, Any]) -> None:
        voice = self._subsystems["voice"]
        phase = ev.get("phase")
        if phase == "start":
            self._speaking = True
            voice["status"] = "online"
        elif phase == "end":
            self._speaking = False
            voice["status"] = "online"
        elif phase == "error":
            self._speaking = False
            voice["status"] = "degraded"

    def _on_voice_input(self, ev: dict[str, Any]) -> None:
        voice = self._subsystems["voice"]
        mode, phase = ev.get("mode"), ev.get("phase")
        voice.update(mode=mode, input_phase=phase)
        if voice["status"] in ("unknown", "degraded"):
            voice["status"] = "online"
        # With the mode off nothing is being listened for, whatever phase a late event carries.
        live = mode != "off"
        self._listening = live and phase in LISTENING_PHASES
        self._transcribing = live and phase == TRANSCRIBING_PHASE

    def _on_system_metrics(self, ev: dict[str, Any]) -> None:
        system = self._subsystems["system"]
        system["status"] = "online"
        for field in ("cpu_percent", "memory_percent", "disk_percent"):
            system[field] = ev.get(field)

    def _on_network(self, ev: dict[str, Any]) -> None:
        status = {"up": "online", "down": "offline"}.get(ev.get("groq"), "unknown")
        self._subsystems["network"].update(
            status=status, groq=ev.get("groq"), source=ev.get("source"),
            latency_ms=ev.get("latency_ms"), checked_at=ev.get("checked_at"))

    _HANDLERS: dict[str, Callable[["StateTracker", dict[str, Any]], None]] = {
        "session.start": _on_session_start,
        "session.end": _on_session_end,
        "request.start": _on_request_start,
        "request.end": _on_request_end,
        "llm.call": _on_llm_call,
        "llm.retry": _on_llm_retry,
        "llm.error": _on_llm_error,
        "tool.execution": _on_tool,
        "memory.activity": _on_memory,
        "voice.output": _on_voice_output,
        "voice.input": _on_voice_input,
        "system.metrics": _on_system_metrics,
        "network.status": _on_network,
    }
