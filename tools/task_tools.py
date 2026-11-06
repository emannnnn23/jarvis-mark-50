"""The tool that lets the model keep a durable plan for a multi-step job."""

from __future__ import annotations

import re
from typing import Any

from memory.task_store import STEP_STATUSES, TaskStoreError, count_done_steps
from tools.registry import ToolError


UPDATE_TASK_ACTIONS = ("start", "plan", "step", "finish", "abandon")

# "1. ", "2) ", "- ", "* " in front of a line: models number steps even when told not to.
_STEP_MARKER = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)])\s+")


def split_steps(text: str | None) -> list[str]:
    """One step per line; blank lines and list markers are dropped."""
    lines = (_STEP_MARKER.sub("", line).strip() for line in (text or "").splitlines())
    return [line for line in lines if line]


def format_plan(task: dict[str, Any]) -> str:
    """The task's plan as plain text, for the model."""
    steps = task["steps"]
    done = count_done_steps(task)
    lines = [f"Task {task['id']} ({task['status']}): {task['goal']}"]
    if steps:
        lines.append(f"Plan ({done}/{len(steps)} steps done):")
        for number, step in enumerate(steps, 1):
            lines.append(f"  {number}. [{step['status']}] {step['text']}")
    else:
        lines.append("No plan yet; add one with action plan.")
    return "\n".join(lines)


class TaskTools:
    """Task tool handlers. Mixed into JarvisTools, which provides self.tasks (a TaskStore or None)."""

    def update_task(self, action: str, goal: str | None = None, steps: str | None = None,
                    step: int | None = None, status: str | None = None, evidence: str | None = None,
                    summary: str | None = None) -> dict[str, Any]:
        if self.tasks is None:
            raise ToolError("Task tracking is not available in this session.")
        try:
            return self._update_task(action, goal, steps, step, status, evidence, summary)
        except TaskStoreError as exc:
            raise ToolError(str(exc)) from exc

    def _update_task(self, action: str, goal: str | None, steps: str | None, step: int | None,
                     status: str | None, evidence: str | None, summary: str | None) -> dict[str, Any]:
        store = self.tasks
        if action == "start":
            if not goal:
                raise ToolError("action start needs a goal.")
            task = store.start(goal, split_steps(steps))
            return {"message": "Task started.", "task_id": task["id"], "plan": format_plan(task)}
        active = store.active()
        if active is None:
            raise ToolError("There is no active task. Use action start to begin one.")
        task_id = active["id"]
        if action == "plan":
            if not split_steps(steps):
                raise ToolError("action plan needs steps, one per line.")
            task = store.set_plan(task_id, split_steps(steps))
            return {"message": "Plan updated.", "task_id": task_id, "plan": format_plan(task)}
        if action == "step":
            if step is None or status is None:
                raise ToolError(f"action step needs step (a number from 1) and status ({', '.join(STEP_STATUSES)}).")
            task = store.set_step(task_id, step, status, evidence)
            return {"message": f"Step {step} marked {status}.", "task_id": task_id, "plan": format_plan(task)}
        if action == "finish":
            # Refused without a summary, while a step is still open, or while changed files lack a passing test run.
            task = store.finish(task_id, summary or "")
            return {"message": "Task finished.", "task_id": task_id, "plan": format_plan(task)}
        if action == "abandon":
            task = store.abandon(task_id)
            return {"message": "Task abandoned.", "task_id": task_id, "plan": format_plan(task)}
        raise ToolError(f"Unknown action {action!r}; use one of: {', '.join(UPDATE_TASK_ACTIONS)}.")
