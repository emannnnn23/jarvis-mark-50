"""The request lifecycle: the bounded model/tool loop."""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING

from core.context import build_messages, build_system_prompt
from core.policy import Permission
from intelligence.groq_client import JarvisAPIError, call_model
from tools.registry import (STATUS_DENIED, STATUS_ERROR, STATUS_SUCCESS, Tool, ToolValidationError, execute_tool,
                            parse_arguments, serialize_tool_result)

if TYPE_CHECKING:
    from config.settings import Config
    from core.policy import PermissionPolicy
    from memory.json_store import MemoryStore
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


def run_agent(ctx: AgentContext, user_input: str, history: list[dict[str, Any]] | None = None,
              history_summary: str = "", on_tool: Callable[[str], None] | None = None,
              on_retry: Callable[[str, float], None] | None = None,
              tool_groups: set[str] | None = None) -> AgentResult:
    """Run the model/tool loop until a final answer or the round limit.

    `tool_groups` limits which tools the model is shown (and may call); None means all of them.
    Raises JarvisAPIError if the model cannot be reached; KeyboardInterrupt cancels.
    """
    memories, profile, warning = [], None, None
    if ctx.memory is not None:
        memories = ctx.memory.get_relevant_memories(user_input, limit=5)
        profile = ctx.memory.profile()
        warning = ctx.memory.corruption_error
    system_prompt = build_system_prompt(ctx.config, memories, profile, history_summary, warning)
    messages = build_messages(system_prompt, history or [], user_input)
    tools = ctx.registry.schemas(tool_groups)
    offered = {schema["function"]["name"] for schema in tools}
    events: list[dict[str, Any]] = []
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
            return AgentResult(f"I couldn't finish: {exc} {progress_report(events)}", "incomplete", events, round_number)
        message = response.choices[0].message
        if not message.tool_calls:
            return AgentResult(format_final_response(message.content), "complete", events, round_number)

        messages.append(_assistant_tool_message(message))
        for call in message.tool_calls:
            name = call.function.name
            key = _canonical_call(name, call.function.arguments)
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
                tool = ctx.registry.get(name)
                if result["status"] == STATUS_SUCCESS and _changes_state(tool, call.function.arguments):
                    # Earlier results may now be stale (e.g. tests run before an edit): forget them.
                    previous_results.clear()
                previous_results[key] = result
                # Any result that ran (even a failed or timed-out one) may carry third-party text.
                if tool is not None and tool.untrusted_output and result["status"] != STATUS_DENIED:
                    tainted = True
            events.append(_event(name, call.function.arguments, result))
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
    return AgentResult(text, "incomplete", events, max_rounds)
