# Plan: task continuity (Phase 4, slice 2)

## Intent

Emanuel: "I want JARVIS not just an AI assistant but one that can perform and execute tasks; I want
him to remember when I come back." So JARVIS must (1) carry multi-step jobs through with a visible
plan and progress, (2) save checkpoints so a job interrupted by Ctrl+C, "Hey JARVIS, stop", a rate
limit, the round limit or a restart can be explained and resumed, and (3) remember the recent
conversation across restarts.

## Design (decided)

- **Checkpoints cost no extra model calls.** The orchestrator records every tool event of the active
  task automatically. The model only writes the plan and ticks steps, through one `update_task` tool
  it can call in the same response as its other tool calls.
- **Storage:** `jarvis_tasks.json` next to jarvis.py (`Config.tasks_path`, env `JARVIS_TASKS_PATH`,
  already added), git-ignored, written atomically, behind a `TaskStore` class whose public methods
  are the interface a SQLite store would replace (same pattern as `memory/json_store.py`).
- **Startup:** reload the last `Config.restore_turns` (env `JARVIS_RESTORE_TURNS`, default 6, 0-20,
  already added) exchanges from the memory file's conversation log into the session; announce any
  unfinished task.
- **Resume:** "continue"/"resume" or `/resume [id]` reactivates the task and asks the model to check
  the current state first (files may have changed), then do the remaining steps. Approvals are
  always asked again; nothing carries over.
- **Verified completion:** a task that changed files cannot be finished until `run_project_tests`
  passed after the last change.

## Global constraints

- Python 3.12, existing style (see neighbouring modules): `from __future__ import annotations`,
  docstrings that say why, dataclasses where the code already uses them, no new dependencies.
- Never store secrets: pass every user/model-provided string through `core.audit.redact` and
  `core.audit.clean_text` before saving; cap lengths (goal 300, step text 200, evidence 300,
  request 500, last_error 300 chars).
- Atomic writes (temp file in the same folder, fsync, os.replace, mode 0600), and never overwrite a
  corrupted file silently (mirror MemoryStore: `corruption_error`, read-only until reset).
- Tests use temp dirs only, no network, no Groq; run with
  `.venv/bin/python -m pytest -q tests/ -p no:cacheprovider -W error::pytest.PytestUnraisableExceptionWarning -W error::ResourceWarning`.
  The whole suite must stay green (587 tests at the start).
- Do not commit, push or change git config. Never open or print `.env`.

---

### Task 1: TaskStore (new files only)

Files: create `memory/task_store.py`, create `tests/test_tasks.py`.

```python
TASK_STATUSES = ("active", "paused", "done", "abandoned")
STEP_STATUSES = ("pending", "in_progress", "done", "skipped", "failed")
MAX_TASKS = 50        # oldest done/abandoned tasks are dropped first
MAX_ACTIONS = 200     # per task, oldest dropped
CHANGING_TOOLS = frozenset({"edit_workspace_file", "create_workspace_file"})

class TaskStoreError(Exception): ...            # user-facing message

class TaskStore:
    def __init__(self, path: Path): ...
    corruption_error: str | None                # set by load() on bad JSON; save() then raises
    def load(self) -> None
    def save(self) -> None
    def reset_corrupted(self) -> Path           # move aside as jarvis_tasks.corrupt-<stamp>.json
    def start(self, goal: str, steps: list[str] | None = None, request: str = "") -> dict
        # new task "active"; any other active task becomes "paused" (reason "another task started")
    def active(self) -> dict | None
    def get(self, task_id: str) -> dict | None
    def unfinished(self) -> list[dict]          # active + paused, most recently updated first
    def recent(self, limit: int = 10) -> list[dict]
    def set_plan(self, task_id: str, steps: list[str]) -> dict   # replaces steps, keeps done ones by text
    def set_step(self, task_id: str, number: int, status: str, evidence: str | None = None) -> dict
        # number is 1-based; "done" requires non-empty evidence (TaskStoreError otherwise)
    def record_action(self, task_id: str, event: dict) -> None
        # event as produced by core.orchestrator._event: {"tool", "status", "target"?, "passed"?}
        # adds {"at": iso time}; a successful CHANGING_TOOLS event adds target to files_changed
    def record_error(self, task_id: str, message: str) -> None
    def pause(self, task_id: str, reason: str) -> dict
    def resume(self, task_id: str) -> dict      # becomes active; other active tasks paused
    def finish(self, task_id: str, summary: str) -> dict
        # raises TaskStoreError with verification_gap() text if a gap exists
    def abandon(self, task_id: str) -> dict
    def forget(self, task_id: str) -> dict
    def verification_gap(self, task_id: str) -> str | None
        # files changed and no run_project_tests action with passed=True after the last
        # successful CHANGING_TOOLS action -> "Run the project's tests (run_project_tests) after
        # the last change before finishing: <n> file(s) changed since the last passing run."
    def summary_for_prompt(self, task_id: str) -> str
        # <= 1500 chars, e.g.
        # "Task t1a2b3 (paused: rate limit): <goal>\nPlan:\n  1. [done] ...\n  2. [pending] ...\n
        #  Files changed: a.py, b.py\nLast error: ...\nLast actions: edit_workspace_file on a.py: success; ..."
```

Task record shape (JSON):
`{"id": "t" + 6 hex, "goal", "status", "created_at", "updated_at", "paused_reason": str|None,
"request", "steps": [{"text", "status", "evidence", "updated_at"}], "actions": [...],
"files_changed": [...], "last_error": str|None, "summary": str|None}`; file: `{"tasks": [...]}`.
IDs unique; timestamps ISO with offset (`datetime.now().astimezone().isoformat(timespec="seconds")`).

Tests (tests/test_tasks.py): create/load/persist across a fresh instance; atomic write leaves no temp
files; corrupted file untouched and save refused; reset_corrupted; start pauses the previous active
task; set_step bounds and "done needs evidence"; record_action files_changed; verification_gap cases
(no changes; change then failing test; change then passing test; passing test then another change);
finish refused on a gap, allowed after a passing run; caps (MAX_TASKS keeps unfinished, MAX_ACTIONS);
redaction of a fake gsk_ key in goal/evidence/error; summary_for_prompt content and length cap.

### Task 2: Conversation restore across restarts (parallel with Task 1)

Files: `core/session.py`, `memory/json_store.py`, `ui/terminal.py` (startup only), tests in
`tests/test_memory.py` and `tests/test_terminal.py`.

- `MemoryStore.recent_conversations(limit: int) -> list[dict]`: copies of the last `limit` entries
  of `data["conversations"]` (oldest first); `[]` for limit <= 0.
- `Conversation.restore(entries: list[dict]) -> int`: appends (user, assistant) turns from entries
  that have both texts, respecting max_turns/max_chars trimming; returns how many were restored.
- `JarvisCLI`: a method `restore_conversation()` called at the start of `loop()` (before `banner()`)
  restores `config.restore_turns` entries; if any were restored, the banner prints one dim line:
  `Picking up where we left off (last talked <when>).` where `<when>` is human-readable from the last
  entry's timestamp: "a moment ago" (< 2 min), "N minutes ago", "N hours ago", "yesterday",
  "N days ago". Also pass the last-conversation time into the system prompt context as
  `- Last conversation with the user: <when> (<iso timestamp>)` via a new
  `build_system_prompt(..., last_seen: str = "")` argument and `run_agent(..., last_seen: str = "")`
  passthrough; the CLI computes it once at startup.
- Restored turns are already redacted and truncated (they come from the saved log); do not re-save
  them (no duplicates in the log).

### Task 3: Orchestrator, tool and CLI integration (after Tasks 1 and 2)

Files: `core/orchestrator.py`, `core/context.py`, `tools/catalog.py`, `tools/memory_tools.py` or a new
`tools/task_tools.py` mixin, `ui/terminal.py`, `jarvis.py` (wiring), tests.

- `AgentContext` gains `tasks: TaskStore | None = None`.
- Tool `update_task` (group "core", Permission.READ_ONLY with a comment: it edits only JARVIS's own
  task notes), schema (strings/ints only; the validator has no arrays):
  `action` enum ["start", "plan", "step", "finish", "abandon"]; `goal` string (start);
  `steps` string, one step per line (start/plan); `step` integer >= 1 (step); `status` enum
  STEP_STATUSES (step); `evidence` string (step, required for done); `summary` string (finish).
  Description tells the model to call it in the same response as its other tool calls.
  It acts on the active task (start creates one). Returns the updated plan as text plus the
  verification gap if finishing is refused (status error).
- run_agent: when `ctx.tasks` has an active task, record each tool event via `record_action`
  (skip `update_task` itself); on an incomplete result (round limit or API failure) call
  `pause(task_id, reason)` and `record_error`; the CLI pauses on KeyboardInterrupt with reason
  "interrupted by the user". `on_progress(message)` callback fired when update_task changes a step:
  message `"[2/3] <step text>: done"`.
- Prompt: `build_system_prompt(..., task_summary: str = "")` adds `Current task:` + summary when a
  task is active or the most recent unfinished one is paused; OPERATING_RULES gains: for jobs with
  more than two steps or any file change, call update_task start with a short plan first; tick steps
  with evidence; finish only with evidence; when resuming, re-check state before acting.
- CLI: banner line for the most recent unfinished task: `Unfinished task: <goal> (<done>/<total>
  steps done, paused <when>: <reason>). Say "continue" or type /tasks.`; `/tasks` lists unfinished
  and recent tasks with ids; `/resume [id]` (default most recent unfinished) and the plain messages
  "continue", "resume", "carry on" (case-insensitive, optional "jarvis"/punctuation) resume it by
  sending the model: `Continue the unfinished task: <goal>. Check the current state first (files may
  have changed since), then do the remaining steps.`; `/abandon [id]`; `/tasks clear` forgets
  done/abandoned tasks. Progress lines printed dim. HELP_TEXT and /status updated.
- `jarvis.py` main(): build the TaskStore from `config.tasks_path`, load it, warn on corruption like
  memory, pass it into AgentContext.
- Tests for all of the above with FakeGroqClient scripts (tests/test_tasks.py or test_orchestrator.py,
  test_terminal.py).
