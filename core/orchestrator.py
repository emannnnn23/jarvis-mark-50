"""The request lifecycle: the bounded model/tool loop."""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING

from core.context import build_messages, build_system_prompt
from intelligence.groq_client import JarvisAPIError, call_model
from tools.registry import STATUS_DENIED, ToolValidationError, execute_tool, parse_arguments, serialize_tool_result

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


def run_agent(ctx: AgentContext, user_input: str, history: list[dict[str, Any]] | None = None,
              history_summary: str = "", on_tool: Callable[[str], None] | None = None,
              on_retry: Callable[[str, float], None] | None = None) -> AgentResult:
    """Run the model/tool loop until a final answer or the round limit.

    Raises JarvisAPIError if the model cannot be reached; KeyboardInterrupt cancels.
    """
    memories, profile, warning = [], None, None
    if ctx.memory is not None:
        memories = ctx.memory.get_relevant_memories(user_input, limit=5)
        profile = ctx.memory.profile()
        warning = ctx.memory.corruption_error
    system_prompt = build_system_prompt(ctx.config, memories, profile, history_summary, warning)
    messages = build_messages(system_prompt, history or [], user_input)
    tools = ctx.registry.schemas()
    events: list[dict[str, Any]] = []
    previous_results: dict[str, dict[str, Any]] = {}
    tainted = False  # becomes True once third-party content (web, files) is in the context

    for round_number in range(1, ctx.config.max_tool_rounds + 1):
        response = call_model(ctx.client, ctx.config.model, messages, tools, on_retry=on_retry,
                              reasoning_effort=ctx.config.reasoning_effort)
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
            else:
                if on_tool:
                    on_tool(name)
                result = execute_tool(ctx.registry, ctx.policy, name, call.function.arguments, tainted=tainted)
                previous_results[key] = result
                tool = ctx.registry.get(name)
                # Any result that ran (even a failed or timed-out one) may carry third-party text.
                if tool is not None and tool.untrusted_output and result["status"] != STATUS_DENIED:
                    tainted = True
            events.append({"tool": name, "status": result["status"]})
            messages.append({"role": "tool", "tool_call_id": call.id, "name": name,
                             "content": serialize_tool_result(result)})

    # Round limit reached: ask for a summary of confirmed progress with tools disabled.
    messages.append({"role": "system", "content": (
        f"The tool-round limit ({ctx.config.max_tool_rounds}) has been reached, so no more tools can run. "
        "Tell the user the task is incomplete, summarise only what the tool results confirm, and say what remains.")})
    try:
        response = call_model(ctx.client, ctx.config.model, messages, tools, tool_choice="none", on_retry=on_retry,
                              reasoning_effort=ctx.config.reasoning_effort)
        text = format_final_response(response.choices[0].message.content)
    except JarvisAPIError:
        done = ", ".join(f"{e['tool']} ({e['status']})" for e in events) or "nothing"
        text = f"I reached my limit of {ctx.config.max_tool_rounds} tool rounds before finishing. Steps taken: {done}."
    return AgentResult(text, "incomplete", events, ctx.config.max_tool_rounds)
