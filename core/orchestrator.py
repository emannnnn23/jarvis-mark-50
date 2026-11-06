"""The request lifecycle: the bounded model/tool loop."""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING

from core.audit import log
from core.context import build_messages, build_system_prompt
from core.policy import Permission
from intelligence.groq_client import JarvisAPIError, call_model
from memory.task_store import TaskStoreError
from tools.registry import (STATUS_DENIED, STATUS_ERROR, STATUS_SUCCESS, Tool, ToolValidationError, execute_tool,
                            parse_arguments, serialize_tool_result)

if TYPE_CHECKING:
    from config.settings import Config
    from core.policy import PermissionPolicy
    from memory.json_store import MemoryStore
    from memory.task_store import TaskStore
    from tools.registry import ToolRegistry


_LEAKED_MARKUP = (
    re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<function=[^>]*>.*?(</function>|\Z)", re.DOTALL),
    re.compile(r"<\|[a-z_]+\|>"),
)


def format_final_response(content: str | None) -> str:
    text = content or ""
    for pattern in _LEAKED_MARKUP:
        text = pattern.sub("", text)
    text = text.strip()
    return text or "I'm afraid the model returned an empty reply, Sir. Could you rephrase that?"


def _assistant_tool_message(message) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": [
            {"id": call.id, "type": "function",
             "function": {"name": call.function.name, "arguments": call.function.arguments or "{}"}}
            for call in message.tool_calls
        ],
    }


@dataclasses.dataclass
class AgentContext:
    client: Any
    config: Config
    registry: ToolRegistry
    policy: PermissionPolicy
    memory: MemoryStore | None = None
    tasks: TaskStore | None = None


@dataclasses.dataclass
class AgentResult:
    text: str
    status: str  # "complete" or "incomplete"
    tool_events: list[dict[str, Any]]
    rounds: int


def _canonical_call(name: str, raw_arguments: Any) -> str:
    try:
        return name + json.dumps(parse_arguments(raw_arguments), sort_keys=True)
    except ToolValidationError:
        return name + str(raw_arguments)


# Tools whose success changes files on disk.
CHANGING_TOOLS = frozenset({"edit_workspace_file", "create_workspace_file", "create_note"})


def _changes_state(tool: Tool | None, raw_arguments: Any) -> bool:
    """Whether a successful call may have changed anything (files, memory, the desktop)."""
    if tool is None:
        return False
    try:
        return tool.permission_for(parse_arguments(raw_arguments)) is not Permission.READ_ONLY
    except Exception:  # noqa: BLE001 - when unsure, treat it as a change
        return True


def _event(name: str, raw_arguments: Any, result: dict[str, Any]) -> dict[str, Any]:
    """What a tool call did, kept for progress reports: tool, status and its target if any."""
    event: dict[str, Any] = {"tool": name, "status": result["status"]}
    try:
        args = parse_arguments(raw_arguments)
    except ToolValidationError:
        args = {}
    target = args.get("path") or args.get("project") or args.get("url") or args.get("name")
    if isinstance(target, str):
        event["target"] = target[:120]
    data = result.get("result") if isinstance(result.get("result"), dict) else result
    if "passed" in data:
        event["passed"] = data["passed"]
    return event


def progress_report(events: list[dict[str, Any]]) -> str:
    """Plain-language list of the steps that already ran, so an unfinished task is never misreported."""
    if not events:
        return "Nothing had been done yet."
    steps = []
    for e in events:
        step = e["tool"] + (f" on {e['target']}" if e.get("target") else "") + f": {e['status']}"
        if "passed" in e:
            step += " (tests passed)" if e["passed"] else " (tests failed)"
        steps.append(step)
    changed = [e for e in events if e["tool"] in CHANGING_TOOLS and e["status"] == "success"]
    note = (" Files were changed; check them, and run the tests if the last step was not a test run."
            if changed else "")
    return "Steps already done: " + "; ".join(steps) + "." + note


UPDATE_TASK = "update_task"

PAUSED_TASK_NOTE = ("This task is paused. Do not act on it or continue it until the user asks to "
                    "(for example \"continue\").")


def task_prompt_summary(tasks: TaskStore | None) -> str:
    """The task the model should know about: the active one, else the most recently updated paused one
    (with a note that it is paused, so it is not acted on until the user says continue)."""
    if tasks is None:
        return ""
    try:
        task = tasks.active()
        if task is None:
            unfinished = tasks.unfinished()
            task = unfinished[0] if unfinished else None
        if task is None:
            return ""
        summary = tasks.summary_for_prompt(task["id"])
    except TaskStoreError:
        return ""
    return summary if task["status"] == "active" else f"{summary}\n{PAUSED_TASK_NOTE}"


def _step_progress(ctx: AgentContext, raw_arguments: Any, result: dict[str, Any]) -> str | None:
    """"[2/3] <step text>: done" after update_task changed a step, else None."""
    if ctx.tasks is None or result["status"] != STATUS_SUCCESS:
        return None
    try:
        args = parse_arguments(raw_arguments)
        data = result.get("result") if isinstance(result.get("result"), dict) else {}
        task = ctx.tasks.get(data.get("task_id", ""))
        if args.get("action") != "step" or task is None:
            return None
        number = int(args["step"])
        text = task["steps"][number - 1]["text"]
        return f"[{number}/{len(task['steps'])}] {text}: {str(args['status']).replace('_', ' ')}"
    except (ToolValidationError, KeyError, IndexError, TypeError, ValueError):
        return None


def _record_action(ctx: AgentContext, event: dict[str, Any]) -> None:
    """Log a tool call on the active task. Task notes must never break the request they describe."""
    if ctx.tasks is None or event["tool"] == UPDATE_TASK:
        return
    try:
        task = ctx.tasks.active()
        if task is not None:
            ctx.tasks.record_action(task["id"], event)
    except TaskStoreError as exc:
        log.warning("Could not record a task action: %s", exc)


def _after_update_task(ctx: AgentContext, raw_arguments: Any, result: dict[str, Any], earlier: list[dict[str, Any]],
                       tainted: bool) -> None:
    """Bookkeeping once update_task start or plan has run.

    A plan written after untrusted content was read is marked as such. A new task also takes over what
    this request already did, so the finish check holds whatever order the model used."""
    if ctx.tasks is None or result["status"] != STATUS_SUCCESS:
        return
    try:
        action = parse_arguments(raw_arguments).get("action")
        data = result.get("result") if isinstance(result.get("result"), dict) else {}
        task_id = data.get("task_id")
        if action not in ("start", "plan") or not task_id:
            return
        if tainted:
            ctx.tasks.mark_untrusted(task_id)
        if action == "start":
            for event in earlier:
                ctx.tasks.record_action(task_id, event)
    except (ToolValidationError, TaskStoreError) as exc:
        log.warning("Could not update the task after update_task: %s", exc)


def _pause_task(ctx: AgentContext, reason: str) -> None:
    """The request ended unfinished: say why on the active task and pause it, so it can be resumed."""
    if ctx.tasks is None:
        return
    try:
        task = ctx.tasks.active()
        if task is not None:
            ctx.tasks.record_error(task["id"], reason)
            ctx.tasks.pause(task["id"], reason)
    except TaskStoreError as exc:
        log.warning("Could not pause the task: %s", exc)


def run_agent(ctx: AgentContext, user_input: str, history: list[dict[str, Any]] | None = None,
              history_summary: str = "", on_tool: Callable[[str], None] | None = None,
              on_retry: Callable[[str, float], None] | None = None,
              tool_groups: set[str] | None = None, voice_state: str = "",
              last_seen: str = "", on_progress: Callable[[str], None] | None = None) -> AgentResult:
    """Run the model/tool loop until a final answer or the round limit.

    `tool_groups` limits which tools the model is shown (and may call); None means all of them.
    With `ctx.tasks`, every tool call is logged on the active task, an unfinished request pauses it, and
    `on_progress` hears about each step update_task changes.
    Raises JarvisAPIError if the model cannot be reached; KeyboardInterrupt cancels.
    """
    memories, profile, warning = [], None, None
    if ctx.memory is not None:
        memories = ctx.memory.get_relevant_memories(user_input, limit=5)
        profile = ctx.memory.profile()
        warning = ctx.memory.corruption_error
    system_prompt = build_system_prompt(ctx.config, memories, profile, history_summary, warning, voice_state,
                                        last_seen, task_prompt_summary(ctx.tasks))
    messages = build_messages(system_prompt, history or [], user_input)
    tools = ctx.registry.schemas(tool_groups)
    offered = {schema["function"]["name"] for schema in tools}
    events: list[dict[str, Any]] = []
    ran: list[dict[str, Any]] = []  # events of tools that ran in this request, update_task excluded
    previous_results: dict[str, dict[str, Any]] = {}
    tainted = False  # becomes True once third-party content (web, files) is in the context

    # Developer tasks (look, edit, test, re-test) need more steps than everyday requests.
    max_rounds = (ctx.config.max_dev_tool_rounds if tool_groups is not None and "developer" in tool_groups
                  else ctx.config.max_tool_rounds)
    for round_number in range(1, max_rounds + 1):
        try:
            response = call_model(ctx.client, ctx.config.model, messages, tools, on_retry=on_retry,
                                  reasoning_effort=ctx.config.reasoning_effort)
        except JarvisAPIError as exc:
            if not events:
                raise
            # Steps already ran: say exactly which, rather than a bare error that hides them.
            _pause_task(ctx, f"the model could not be reached: {exc}")
            return AgentResult(f"I couldn't finish: {exc} {progress_report(events)}", "incomplete", events,
                               round_number - 1)  # rounds actually completed
        message = response.choices[0].message
        if not message.tool_calls:
            return AgentResult(format_final_response(message.content), "complete", events, round_number)

        messages.append(_assistant_tool_message(message))
        for call in message.tool_calls:
            name = call.function.name
            key = _canonical_call(name, call.function.arguments)
            executed = False
            if key in previous_results:
                # Identical repeat within one request: never re-run an action; reuse the result.
                result = {**previous_results[key], "note": "Identical call already made in this request; not repeated."}
            elif name not in offered and ctx.registry.get(name) is not None:
                # A real tool that was not offered this turn (e.g. developer mode is off): never run it.
                result = {"status": STATUS_ERROR, "error": f"{name} is not available right now. Developer tools "
                          "are offered when the user's request is about code or projects (/dev on)."}
            else:
                if on_tool:
                    on_tool(name)
                result = execute_tool(ctx.registry, ctx.policy, name, call.function.arguments, tainted=tainted)
                executed = True
                tool = ctx.registry.get(name)
                if result["status"] == STATUS_SUCCESS and _changes_state(tool, call.function.arguments):
                    # Earlier results may now be stale (e.g. tests run before an edit): forget them.
                    previous_results.clear()
                if name != UPDATE_TASK:  # a step may go back to an earlier status, so it is never "a repeat"
                    previous_results[key] = result
                # Any result that ran (even a failed or timed-out one) may carry third-party text.
                if tool is not None and tool.untrusted_output and result["status"] != STATUS_DENIED:
                    tainted = True
            event = _event(name, call.function.arguments, result)
            events.append(event)
            if executed and name == UPDATE_TASK:
                _after_update_task(ctx, call.function.arguments, result, ran, tainted)
                progress = _step_progress(ctx, call.function.arguments, result) if on_progress else None
                if progress:
                    on_progress(progress)
            elif executed:  # a reused result or a refused call did nothing new to record
                _record_action(ctx, event)
                ran.append(event)
            messages.append({"role": "tool", "tool_call_id": call.id, "name": name,
                             "content": serialize_tool_result(result)})

    # Round limit reached: ask for a summary of confirmed progress with tools disabled.
    messages.append({"role": "system", "content": (
        f"The tool-round limit ({max_rounds}) has been reached, so no more tools can run. "
        "Tell the user the task is incomplete, summarise only what the tool results confirm, and say what remains.")})
    try:
        response = call_model(ctx.client, ctx.config.model, messages, tools, tool_choice="none", on_retry=on_retry,
                              reasoning_effort=ctx.config.reasoning_effort)
        text = format_final_response(response.choices[0].message.content)
    except JarvisAPIError:
        text = f"I reached my limit of {max_rounds} tool rounds before finishing. {progress_report(events)}"
    _pause_task(ctx, f"reached the limit of {max_rounds} tool rounds")
    return AgentResult(text, "incomplete", events, max_rounds)
