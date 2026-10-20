"""The concrete tool set and the registry of every tool the model may call."""

from __future__ import annotations

import webbrowser
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING

from core.policy import Permission
from memory.json_store import MAX_FACT_LENGTH
from tools.browser import BrowserTools
from tools.developer import DeveloperTools
from tools.filesystem import FileTools
from tools.linux_system import SystemTools
from tools.memory_tools import MemoryTools
from tools.registry import Tool, ToolRegistry
from tools.web_search import WebSearchTools

if TYPE_CHECKING:
    from config.settings import Config
    from memory.json_store import MemoryStore


class JarvisTools(WebSearchTools, BrowserTools, MemoryTools, SystemTools, FileTools, DeveloperTools):
    """The concrete tool handlers. Each returns JSON-friendly data or raises ToolError."""

    def __init__(self, config: Config, memory: MemoryStore,
                 browser_open: Callable[[str], bool] = webbrowser.open,
                 ddgs_factory: Callable[..., Any] | None = None):
        self.config = config
        self.memory = memory
        self.browser_open = browser_open
        self.ddgs_factory = ddgs_factory


def _string(description: str, max_length: int = 500, min_length: int = 1) -> dict[str, Any]:
    return {"type": "string", "description": description, "minLength": min_length, "maxLength": max_length}


def _object(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


def build_registry(tools: JarvisTools) -> ToolRegistry:
    """Declare every tool the model may call, with its schema and permission class."""
    R, C, S = Permission.READ_ONLY, Permission.CONTROLLED_ACTION, Permission.SENSITIVE_ACTION
    registry = ToolRegistry()
    for tool in (
        Tool("search_web", "Search the web with DuckDuckGo. Returns titles, snippets and URLs (snippets only).",
             _object({"query": _string("Search query", 300),
                      "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Optional result count"}},
                     ["query"]),
             tools.search_web, R, timeout=30, untrusted_output=True),
        Tool("get_time", "Get the current local date, time and timezone.", _object(), tools.get_time, R, timeout=5),
        Tool("open_youtube", "Open a YouTube search results page in the browser.",
             _object({"search": _string("What to search for on YouTube", 200)}, ["search"]),
             tools.open_youtube, C, lambda a: (f"Open YouTube search for {a['search']!r}", []), timeout=15),
        Tool("open_url", "Open an http(s) web address in the default browser.",
             _object({"url": _string("Web address; omit to open the DuckDuckGo home page", 2000, 0)}),
             tools.open_url, C, lambda a: (f"Open {a.get('url') or 'the browser'!r}", []), timeout=15),
        Tool("remember_fact", "Save a durable fact or preference the user wants remembered. Never secrets.",
             _object({"fact": _string("The fact, written as a full sentence about the user", MAX_FACT_LENGTH),
                      "priority": {"type": "string", "enum": ["normal", "high"], "description": "Optional"}},
                     ["fact"]),
             tools.remember_fact, C, timeout=10),
        Tool("search_memory", "Search remembered facts by keyword, or list recent facts with an empty query. Returns fact ids.",
             _object({"query": _string("Keywords; empty lists recent facts", 200, 0)}),
             tools.search_memory, R, timeout=10),
        Tool("update_fact", "Replace the text of a remembered fact. Find its id with search_memory first.",
             _object({"fact_id": _string("Fact id, e.g. f1a2b3c", 20), "new_text": _string("Replacement text", MAX_FACT_LENGTH)},
                     ["fact_id", "new_text"]),
             tools.update_fact, S, lambda a: tools._describe_fact_change(a, "Update"), timeout=10),
        Tool("forget_fact", "Permanently delete a remembered fact by id. Find its id with search_memory first.",
             _object({"fact_id": _string("Fact id, e.g. f1a2b3c", 20)}, ["fact_id"]),
             tools.forget_fact, S, lambda a: tools._describe_fact_change(a, "Permanently delete"), timeout=10),
        Tool("set_user_profile", "Store the user's name and/or location, only when the user states them.",
             _object({"user_name": _string("User's name", 100), "location": _string("User's location", 200)}),
             tools.set_user_profile, C, timeout=10),
        Tool("system_status", "Report hostname, OS, uptime, CPU load, memory and disk usage.", _object(),
             tools.system_status, R, timeout=15),
        Tool("list_processes", "List the top running processes by CPU or memory.",
             _object({"sort_by": {"type": "string", "enum": ["cpu", "memory"]},
                      "limit": {"type": "integer", "minimum": 1, "maximum": 25}}),
             tools.list_processes, R, timeout=15),
        Tool("check_network", "Check DNS and HTTPS connectivity to the internet.", _object(),
             tools.check_network, R, timeout=20),
        Tool("launch_app", "Launch an approved desktop application: " + ", ".join(sorted(tools.config.apps)) + ".",
             _object({"name": _string("Application name", 60)}, ["name"]),
             tools.launch_app, C, tools._describe_launch, timeout=15),
        Tool("open_project", "Open a project directory inside the workspace in the configured code editor.",
             _object({"path": _string("Directory path relative to the workspace", 500)}, ["path"]),
             tools.open_project, C, lambda a: (f"Open {a['path']!r} in {tools.config.editor}", [a["path"]]), timeout=15),
        Tool("list_directory", "List files in a workspace directory. Paths are relative to the workspace root.",
             _object({"path": _string("Directory; '.' is the workspace root", 500, 0)}),
             tools.list_directory, R, timeout=10, untrusted_output=True),
        Tool("read_file", "Read a text file inside the workspace. Protected files (.env, keys) are refused.",
             _object({"path": _string("File path relative to the workspace", 500),
                      "max_chars": {"type": "integer", "minimum": 100, "maximum": 9000}},
                     ["path"]),
             tools.read_file, R, timeout=10, untrusted_output=True),
        Tool("create_note", "Create a Markdown note in the notes folder. Set overwrite only if the user agreed.",
             _object({"title": _string("Note title", 120), "content": _string("Note body (Markdown)", 20000),
                      "overwrite": {"type": "boolean", "description": "Replace an existing note with this title"}},
                     ["title", "content"]),
             tools.create_note, tools._note_permission, tools._describe_note, timeout=10),
        Tool("git_status", "Read-only git status and diff summary for a repository inside the workspace.",
             _object({"path": _string("Repository directory relative to the workspace", 500)}, ["path"]),
             tools.git_status, R, timeout=35, untrusted_output=True),
    ):
        registry.register(tool)
    return registry
