"""Durable multi-step task records with checkpoints, so an interrupted job can be explained and
resumed after a restart. Same persistence rules as the memory store: atomic writes, and a corrupted
file is never overwritten until the user resets it."""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

from core.audit import clean_text, log, redact
from memory.json_store import _now_iso, save_memory


TASK_STATUSES = ("active", "paused", "done", "abandoned")
STEP_STATUSES = ("pending", "in_progress", "done", "skipped", "failed")

MAX_TASKS = 50        # oldest done/abandoned tasks are dropped first
MAX_ACTIONS = 200     # per task, oldest dropped

# Tools whose success means the workspace changed, so the tests must run again before finishing.
CHANGING_TOOLS = frozenset({"edit_workspace_file", "create_workspace_file"})

MAX_GOAL = 300
MAX_STEP = 200
MAX_EVIDENCE = 300
MAX_REQUEST = 500
MAX_ERROR = 300
MAX_TARGET = 120
MAX_TOOL = 60
MAX_PROMPT_SUMMARY = 1500

_PROMPT_FILES = 10
_PROMPT_ACTIONS = 5

PAUSED_BY_NEW_TASK = "another task started"


class TaskStoreError(Exception):
    """A task operation failed; the message is safe to show the user."""


class TaskCorruptedError(TaskStoreError):
    """The task file exists but cannot be parsed. It is never overwritten automatically."""


def _clean(value: Any, limit: int) -> str:
    """Model- or user-provided text made safe to store: no secrets, one line, bounded.

    Redaction runs before truncation so a key cut in half can never slip past the patterns."""
    return re.sub(r"\s+", " ", redact(clean_text(value))).strip()[:limit]


def _normalize_task(item: Any) -> dict[str, Any] | None:
    """Fill in missing keys of a stored task; entries that are not usable tasks are dropped."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        return None
    task = dict(item)
    now = _now_iso()
    defaults: dict[str, Any] = {
        "goal": "", "status": "paused", "created_at": now, "updated_at": now,
        "paused_reason": None, "request": "", "steps": [], "actions": [], "files_changed": [],
        "last_error": None, "summary": None,
    }
    for key, default in defaults.items():
        task.setdefault(key, default)
    for key in ("steps", "actions", "files_changed"):
        if not isinstance(task[key], list):
            task[key] = []
    return task


def count_done_steps(task: dict[str, Any]) -> int:
    """How many of the task's plan steps are finished (done or skipped)."""
    return sum(1 for step in task["steps"] if step.get("status") in ("done", "skipped"))


class TaskStore:
    """JSON-backed task records. Every method returns copies, never the live stored dicts."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"tasks": []}
        self.corruption_error: str | None = None
        self._lock = threading.RLock()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                self.data = self._read()
                self.corruption_error = None
            except TaskCorruptedError as exc:
                # Keep the broken file untouched; run in-memory only until the user decides.
                self.data = {"tasks": []}
                self.corruption_error = str(exc)
                log.error("Task file corrupted: %s", exc)

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"tasks": []}
        except OSError as exc:
            raise TaskStoreError(f"Cannot read task file {self.path}: {exc}") from exc
        if not raw.strip():
            return {"tasks": []}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TaskCorruptedError(
                f"Task file {self.path} is not valid JSON (line {exc.lineno}, column {exc.colno})."
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("tasks", []), list):
            raise TaskCorruptedError(f"Task file {self.path} does not contain a task list.")
        tasks = [t for t in map(_normalize_task, data.get("tasks", [])) if t is not None]
        return {**data, "tasks": tasks}

    @property
    def writable(self) -> bool:
        return self.corruption_error is None

    def save(self) -> None:
        with self._lock:
            if not self.writable:
                raise TaskStoreError(
                    f"{self.corruption_error} I won't overwrite it. Repair the file, or type "
                    "'/tasks reset' to back it up and start fresh."
                )
            try:
                save_memory(self.data, self.path)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                raise TaskStoreError(f"Could not save tasks to {self.path}: {exc}") from exc

    def reset_corrupted(self) -> Path:
        """Move a corrupted task file aside and start fresh. Returns the backup path."""
        with self._lock:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
            counter = 1
            while backup.exists():  # two resets within a second must not overwrite a backup
                backup = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}-{counter}{self.path.suffix}")
                counter += 1
            if self.path.exists():
                os.replace(self.path, backup)
            self.corruption_error = None
            self.data = {"tasks": []}
            self.save()
            return backup

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        """Hold the lock, save on success, and restore the in-memory state if anything fails."""
        with self._lock:
            snapshot = copy.deepcopy(self.data)
            try:
                yield
                self._trim()
                self.save()
            except BaseException:
                self.data = snapshot
                raise

    def _trim(self) -> None:
        """Keep at most MAX_TASKS tasks, dropping the oldest finished ones. Unfinished are never dropped."""
        tasks = self.data["tasks"]
        excess = len(tasks) - MAX_TASKS
        if excess <= 0:
            return
        finished = sorted((t for t in tasks if t["status"] in ("done", "abandoned")),
                          key=lambda t: t["updated_at"])
        drop = {id(t) for t in finished[:excess]}
        self.data["tasks"] = [t for t in tasks if id(t) not in drop]

    # -- lookups -----------------------------------------------------------
    def _find(self, task_id: str) -> dict[str, Any]:
        for task in self.data["tasks"]:
            if task["id"] == task_id:
                return task
        raise TaskStoreError(f"No task with id {task_id!r}.")

    def _newest_first(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # The list position breaks ties between tasks touched in the same second.
        order = {id(t): i for i, t in enumerate(self.data["tasks"])}
        return sorted(tasks, key=lambda t: (t["updated_at"], order.get(id(t), 0)), reverse=True)

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            active = self._newest_first([t for t in self.data["tasks"] if t["status"] == "active"])
            return copy.deepcopy(active[0]) if active else None

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            for task in self.data["tasks"]:
                if task["id"] == task_id:
                    return copy.deepcopy(task)
        return None

    def unfinished(self) -> list[dict[str, Any]]:
        """Active and paused tasks, most recently updated first."""
        with self._lock:
            found = [t for t in self.data["tasks"] if t["status"] in ("active", "paused")]
            return copy.deepcopy(self._newest_first(found))

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._newest_first(list(self.data["tasks"]))[:max(limit, 0)])

    # -- lifecycle ---------------------------------------------------------
    def _new_id(self) -> str:
        existing = {t["id"] for t in self.data["tasks"]}
        while True:
            task_id = "t" + uuid.uuid4().hex[:6]
            if task_id not in existing:
                return task_id

    @staticmethod
    def _new_step(text: str, now: str) -> dict[str, Any]:
        return {"text": text, "status": "pending", "evidence": None, "updated_at": now}

    @staticmethod
    def _clean_steps(steps: list[str] | None) -> list[str]:
        return [text for text in (_clean(s, MAX_STEP) for s in steps or []) if text]

    def _pause_active(self, reason: str, except_id: str | None = None) -> None:
        now = _now_iso()
        for task in self.data["tasks"]:
            if task["status"] == "active" and task["id"] != except_id:
                task.update(status="paused", paused_reason=reason, updated_at=now)

    def start(self, goal: str, steps: list[str] | None = None, request: str = "") -> dict[str, Any]:
        """Begin a new active task. Any other active task is paused, never lost."""
        goal = _clean(goal, MAX_GOAL)
        if not goal:
            raise TaskStoreError("A task needs a goal.")
        with self._transaction():
            self._pause_active(PAUSED_BY_NEW_TASK)
            now = _now_iso()
            task = {
                "id": self._new_id(), "goal": goal, "status": "active", "created_at": now,
                "updated_at": now, "paused_reason": None, "request": _clean(request, MAX_REQUEST),
                "steps": [self._new_step(t, now) for t in self._clean_steps(steps)],
                "actions": [], "files_changed": [], "last_error": None, "summary": None,
            }
            self.data["tasks"].append(task)
            result = copy.deepcopy(task)
        return result

    def set_plan(self, task_id: str, steps: list[str]) -> dict[str, Any]:
        """Replace the plan. A step already done keeps its status and evidence if its text is unchanged."""
        with self._transaction():
            task = self._find(task_id)
            now = _now_iso()
            done = {s["text"]: s for s in task["steps"] if s.get("status") == "done"}
            task["steps"] = [dict(done[t]) if t in done else self._new_step(t, now)
                             for t in self._clean_steps(steps)]
            task["updated_at"] = now
            result = copy.deepcopy(task)
        return result

    def set_step(self, task_id: str, number: int, status: str, evidence: str | None = None) -> dict[str, Any]:
        """Checkpoint one step (number is 1-based). Marking it done needs evidence of what was done."""
        if status not in STEP_STATUSES:
            raise TaskStoreError(f"Unknown step status {status!r}; use one of: {', '.join(STEP_STATUSES)}.")
        cleaned = _clean(evidence, MAX_EVIDENCE) if evidence is not None else None
        if status == "done" and not cleaned:
            raise TaskStoreError("A step can only be marked done with evidence of what was done.")
        with self._transaction():
            task = self._find(task_id)
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= len(task["steps"]):
                raise TaskStoreError(f"Task {task_id} has no step {number}; it has {len(task['steps'])} step(s).")
            step = task["steps"][number - 1]
            now = _now_iso()
            step["status"] = status
            if cleaned is not None:
                step["evidence"] = cleaned
            step["updated_at"] = task["updated_at"] = now
            result = copy.deepcopy(task)
        return result

    def record_action(self, task_id: str, event: dict[str, Any]) -> None:
        """Log what a tool call did (an event from core.orchestrator._event) and track changed files."""
        with self._transaction():
            task = self._find(task_id)
            now = _now_iso()
            action: dict[str, Any] = {
                "tool": _clean(event.get("tool", ""), MAX_TOOL), "status": _clean(event.get("status", ""), MAX_TOOL),
            }
            target = _clean(event["target"], MAX_TARGET) if event.get("target") else ""
            if target:
                action["target"] = target
            if "passed" in event:
                action["passed"] = bool(event["passed"])
            action["at"] = now
            task["actions"].append(action)
            del task["actions"][:-MAX_ACTIONS]
            if action["tool"] in CHANGING_TOOLS and action["status"] == "success" and target:
                if target not in task["files_changed"]:
                    task["files_changed"].append(target)
            task["updated_at"] = now

    def record_error(self, task_id: str, message: str) -> None:
        with self._transaction():
            task = self._find(task_id)
            task["last_error"] = _clean(message, MAX_ERROR) or None
            task["updated_at"] = _now_iso()

    def _require_unfinished(self, task: dict[str, Any], action: str) -> None:
        if task["status"] in ("done", "abandoned"):
            raise TaskStoreError(f"Task {task['id']} is already {task['status']}; I can't {action} it.")

    def pause(self, task_id: str, reason: str) -> dict[str, Any]:
        with self._transaction():
            task = self._find(task_id)
            self._require_unfinished(task, "pause")
            task.update(status="paused", paused_reason=_clean(reason, MAX_ERROR) or "paused",
                        updated_at=_now_iso())
            result = copy.deepcopy(task)
        return result

    def resume(self, task_id: str) -> dict[str, Any]:
        """Make the task active again. Any other active task is paused."""
        with self._transaction():
            task = self._find(task_id)
            self._require_unfinished(task, "resume")
            self._pause_active(PAUSED_BY_NEW_TASK, except_id=task_id)
            task.update(status="active", paused_reason=None, updated_at=_now_iso())
            result = copy.deepcopy(task)
        return result

    def finish(self, task_id: str, summary: str) -> dict[str, Any]:
        """Mark the task done, unless files changed and the tests have not passed since."""
        with self._transaction():
            task = self._find(task_id)
            self._require_unfinished(task, "finish")
            gap = self.verification_gap(task_id)
            if gap:
                raise TaskStoreError(gap)
            task.update(status="done", paused_reason=None, summary=_clean(summary, MAX_GOAL) or None,
                        updated_at=_now_iso())
            result = copy.deepcopy(task)
        return result

    def abandon(self, task_id: str) -> dict[str, Any]:
        with self._transaction():
            task = self._find(task_id)
            self._require_unfinished(task, "abandon")
            task.update(status="abandoned", paused_reason=None, updated_at=_now_iso())
            result = copy.deepcopy(task)
        return result

    def forget(self, task_id: str) -> dict[str, Any]:
        """Delete a task record entirely. Returns what was removed."""
        with self._transaction():
            task = self._find(task_id)
            self.data["tasks"].remove(task)
            return copy.deepcopy(task)

    # -- verification and prompt -------------------------------------------
    def verification_gap(self, task_id: str) -> str | None:
        """Why the task is not safe to finish, or None. Changed files need a passing test run after the last change."""
        with self._lock:
            task = self._find(task_id)
            files = task["files_changed"]
            if not files:
                return None
            actions = task["actions"]
            last_change = max((i for i, a in enumerate(actions)
                               if a.get("tool") in CHANGING_TOOLS and a.get("status") == "success"), default=-1)
            if any(a.get("tool") == "run_project_tests" and a.get("passed") is True
                   for a in actions[last_change + 1:]):
                return None
            return ("Run the project's tests (run_project_tests) after the last change before finishing: "
                    f"{len(files)} file(s) changed since the last passing run.")

    def summary_for_prompt(self, task_id: str) -> str:
        """A compact description of the task for the model's context, at most MAX_PROMPT_SUMMARY chars.

        What a resume depends on (files changed, last error, last actions) is always kept; the plan
        is what gets cut when space runs out, unfinished steps first to stay."""
        with self._lock:
            task = self._find(task_id)
            state = task["status"] + (f": {task['paused_reason']}" if task.get("paused_reason") else "")
            head = [f"Task {task['id']} ({state}): {task['goal']}"]
            tail = []
            if task["files_changed"]:
                shown = task["files_changed"][:_PROMPT_FILES]
                more = len(task["files_changed"]) - len(shown)
                tail.append("Files changed: " + ", ".join(shown) + (f" (+{more} more)" if more else ""))
            if task["last_error"]:
                tail.append(f"Last error: {task['last_error']}")
            if task["actions"]:
                parts = []
                for a in task["actions"][-_PROMPT_ACTIONS:]:
                    part = a["tool"] + (f" on {a['target']}" if a.get("target") else "") + f": {a['status']}"
                    if "passed" in a:
                        part += " (tests passed)" if a["passed"] else " (tests failed)"
                    parts.append(part)
                tail.append("Last actions: " + "; ".join(parts))
            plan = self._plan_lines(task["steps"], MAX_PROMPT_SUMMARY - len("\n".join(head + tail)) - 1)
            text = "\n".join(head + plan + tail)
            if len(text) > MAX_PROMPT_SUMMARY:  # only if the tail alone is too long
                text = text[:MAX_PROMPT_SUMMARY - 3] + "..."
            return text

    @staticmethod
    def _plan_lines(steps: list[dict[str, Any]], budget: int) -> list[str]:
        """The plan as prompt lines within `budget` characters (newlines included); unfinished steps
        are kept before finished ones, and the steps left out are counted."""
        if not steps:
            return []
        lines = [f"  {n}. [{s['status']}] {s['text']}" for n, s in enumerate(steps, 1)]
        header = "Plan:"
        if budget >= len(header) + 1 + sum(len(line) + 1 for line in lines):
            return [header, *lines]
        budget -= len(header) + 1 + len("  ... (999 more steps not shown)") + 1  # room for the note
        order = sorted(range(len(steps)), key=lambda i: steps[i]["status"] in ("done", "skipped"))  # stable
        keep = set()
        for i in order:
            if len(lines[i]) + 1 > budget:
                break
            budget -= len(lines[i]) + 1
            keep.add(i)
        hidden = len(steps) - len(keep)
        shown = [lines[i] for i in range(len(steps)) if i in keep]
        return [header, *shown, f"  ... ({hidden} more step{'s' if hidden != 1 else ''} not shown)"]
