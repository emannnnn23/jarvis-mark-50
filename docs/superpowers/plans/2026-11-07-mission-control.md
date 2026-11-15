# Mission Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live 3D "Mission Control" browser interface for JARVIS MARK 50 whose every animation and number comes from real backend events, and from which Emanuel can send messages, cancel, and answer approvals through the existing request handler.

**Architecture:** The running `jarvis.py` process publishes scrubbed events on an in-process bus (`core/events.py`). When started with `--mission-control`, an aiohttp server on its own thread (127.0.0.1, token, Host/Origin checks) streams them over one WebSocket to a React + React Three Fiber page built from `mission_control/web/`. Inbound messages enter the terminal's main-thread loop through a queue; approvals go through a broker that the terminal and the browser both answer, first answer wins.

**Tech Stack:** Python 3.12, aiohttp 3.14.5 (already installed), psutil 7.2.2; Node 20, Vite, React 19, TypeScript, three 0.187.0, @react-three/fiber 9.8.2, @react-three/drei (version whose peer range accepts fiber 9), zustand 5, vitest.

**Spec:** `docs/superpowers/specs/2026-11-07-mission-control-design.md`

## Global Constraints

- Mission Control is **off by default**. With it off, `_read_line`, `terminal_confirm`, `VoiceEngine._synthesize` and every existing test behave exactly as today (793 tests, `tests/test_wake.py:671` asserts no `select` when hands-free is off).
- No new Python dependency except pinning `aiohttp==3.14.5` in `requirements.txt` (comment: already pulled in by edge-tts; JARVIS now imports it directly). aiohttp is imported only inside `mission_control/server.py`, lazily.
- Server binds `127.0.0.1` only; default port `8765` (`JARVIS_UI_PORT`, 1024-65535). Token `secrets.token_urlsafe(32)`, compared with `hmac.compare_digest`, sent only in the first WebSocket frame, never in logs, audit lines, process argv or request URLs.
- Every string leaving the backend passes `core.events.scrub` (= `safe_display(redact(clean_text(s)))`, redaction before truncation). The Groq key never reaches the frontend.
- A spoken "yes" never approves. Browser approvals are bound to a single-use confirmation id plus a content digest; cancel, EOF and timeout decline.
- Envelope for every server event: `{"v": 1, "seq": int, "type": str, "timestamp": epoch_ms, "request_id"?: str, ...payload}`.
- Frontend renders backend text only as React text nodes: no `dangerouslySetInnerHTML`, `innerHTML` or `eval` in `mission_control/web/src`.
- Performance budget for Intel HD 620: particles 400 / 1200 / 3000 (Low / Balanced / High), DPR `[1,1]` / `[1,1.25]` / `[1,1.75]`, bloom only in High and lazy-loaded, `frameloop` "never" when the tab is hidden.
- Style: `from __future__ import annotations`, docstrings that say why, dataclasses where the code uses them. Tests: temp dirs, no network, no Groq; servers bind port 0 on 127.0.0.1.
- Python suite: `.venv/bin/python -m pytest -q tests/ -p no:cacheprovider -W error::pytest.PytestUnraisableExceptionWarning -W error::ResourceWarning` (green after every task). Frontend (from Task 5): `cd mission_control/web && npm test && npm run typecheck && npm run build`.
- Implementers do not commit, push or change git config; the controller commits by path with no AI attribution. Never open or print `.env`.

## Review Focus

1. **Port 8765 already in use** (a second JARVIS, another app): JARVIS prints one yellow warning and the terminal works normally. Task 4 test `test_port_in_use_warns_and_terminal_still_runs`.
2. **Old browser tab after JARVIS restarts** (stale token): the page says "Open the link printed in the terminal" and stops reconnecting instead of hammering the server. Task 5 vitest `stops reconnecting after 4401` + Task 4 server test for 4401.
3. **Tab closed or reloaded while an approval is pending**: the reconnecting tab receives the pending confirmation in its snapshot and can still answer; a second tab sees it resolved when the first answers. Task 8 `test_pending_payload_in_snapshot_and_resolution_reaches_all_clients`.
4. **Mission Control not built yet** (`dist/` missing): `/` serves a plain page with the exact build command, and `--check` says "not built". Task 4 `test_missing_dist_serves_build_instructions`.
5. **psutil missing or failing**: SYSTEM node shows "unavailable" rather than zeros. Task 3 `test_sampler_without_psutil_reports_unavailable`.

---

### Task 1: Event bus

**Files:**
- Create: `core/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Produces:
  - `PROTOCOL_VERSION = 1`
  - `class EventBus(history: int = 500, clock: Callable[[], float] = time.time)` with `enabled: bool` (default False), `publish(type: str, *, _transient: bool = False, **data) -> dict | None` (never raises; returns the envelope), `subscribe(fn: Callable[[dict], None]) -> Callable[[], None]` (returns unsubscribe), `recent(after_seq: int | None = None) -> list[dict] | None` (None = the requested seq fell out of the ring → caller sends a snapshot), `request_scope(source: str) -> ContextManager[str]` (yields a new `request_id`; events published inside carry it).
  - Module singleton `BUS = EventBus()`; `emit(type: str, **data) -> None` (no-op when `not BUS.enabled`); `emit_throttled(key: str, interval: float, type: str, **data) -> None`; `scrub(value, *, max_str: int = 2000, max_items: int = 200, depth: int = 4) -> Any`.

- [ ] **Step 1: Write the failing tests** in `tests/test_events.py`:
  - `test_disabled_bus_emit_is_noop` — `emit` with `BUS.enabled = False` stores nothing.
  - `test_publish_assigns_monotonic_seq_and_envelope` — keys `v, seq, type, timestamp`; seq 1, 2, 3.
  - `test_failing_subscriber_never_raises_into_caller` — a subscriber raising `RuntimeError`; `publish` returns normally; other subscribers still called.
  - `test_ring_is_bounded_and_recent_reports_gap` — history 3, publish 5; `recent(after_seq=1)` is None; `recent(after_seq=3)` returns seqs 4, 5.
  - `test_transient_events_skip_the_ring` — `_transient=True` reaches subscribers, absent from `recent()`.
  - `test_request_scope_tags_events` — events inside carry the same `request_id`; outside have none.
  - `test_scrub_redacts_before_truncating` — `"x"*1990 + " gsk_" + "k"*30` truncated at 2000 contains no `gsk_k`; nested dict/list depth and item caps applied.
  - `test_concurrent_publishes_get_unique_seqs` — 8 threads × 200 publishes → 1600 distinct seqs.
  - `test_emit_throttled_limits_rate` — fake clock; interval 0.2 → second call within 0.2 s dropped.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest -q tests/test_events.py -p no:cacheprovider` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement `core/events.py`.** One `threading.Lock` held only for seq increment, envelope build and `deque(maxlen=history)` append; subscribers called outside the lock, each in `try/except Exception` logged at debug. `scrub` reuses `core.audit.redact`, `clean_text`, `safe_display`.
- [ ] **Step 4: Run** the same command — Expected: PASS. Then the full Python suite — Expected: all green.
- [ ] **Step 5: Hand back** (controller commits `core/events.py tests/test_events.py`).

### Task 2: Protocol and state tracker

**Files:**
- Create: `mission_control/__init__.py`, `mission_control/protocol.py`, `mission_control/protocol_fixtures.json`, `mission_control/state.py`
- Test: `tests/test_mission_state.py`

**Interfaces:**
- Consumes: Task 1 envelope.
- Produces:
  - `protocol.EVENT_TYPES: dict[str, frozenset[str]]` (type → required payload fields) covering every type in the spec's event contract: `session.start, session.end, jarvis.state, request.start, request.end, assistant.message, llm.call, llm.retry, llm.error, tool.execution, memory.activity, task.update, task.progress, voice.output, voice.input, voice.level, confirm.request, confirm.resolved, command.ack, command.rejected, command.output, system.metrics, network.status, notice`; `protocol.validate_event(ev: dict) -> list[str]` (problems; empty = valid).
  - `protocol_fixtures.json`: `{"events": [one valid sample per type]}` — the shared contract the frontend guards are tested against.
  - `state.StateTracker(clock=time.monotonic)` with `ERROR_HOLD = 4.0`, `apply(ev: dict) -> dict | None` (returns a `jarvis.state` payload `{"state", "detail", "since"}` only when the state changed), `tick() -> dict | None`, `snapshot() -> dict` (`{"state": ..., "subsystems": {"llm","memory","tools","system","voice","network"}: {"status": "online|degraded|offline|unknown|unavailable", ...}}`).
  - States `idle | listening | processing | speaking | tool | error`; priority error > tool > processing > speaking > listening > idle. `tool` from `tool.execution` running/awaiting_confirmation until a final status (detail = tool name or "awaiting approval"); `processing` while a request is active or `voice.input` phase `transcribing`; `speaking` between `voice.output` start and end; `listening` for `voice.input` phases `wake_detected | collecting | recording`; `error` for `ERROR_HOLD` seconds after a non-retryable `llm.error` or `request.end` status `error`, cleared by `request.start`.

- [ ] **Step 1: Write the failing tests:** `test_every_fixture_validates`, `test_fixtures_cover_every_event_type`, `test_validate_reports_missing_fields_and_unknown_type`, a parametrised `test_state_sequence` table (at least: request → processing → tool running → tool success → processing → request.end → idle; voice.output start/end → speaking → idle; wake_detected → listening; non-retryable llm.error → error then idle after 4 s via fake clock; retryable llm.error stays processing), `test_apply_returns_none_when_state_unchanged`, `test_subsystem_llm_status_follows_calls` (success → online, retry → degraded, network kind → offline).
- [ ] **Step 2: Run** `tests/test_mission_state.py` — Expected: FAIL.
- [ ] **Step 3: Implement** `protocol.py`, fixtures, `state.py` (pure: no I/O, no threads).
- [ ] **Step 4: Run** — Expected: PASS; full suite green.
- [ ] **Step 5: Hand back.**

### Task 3: Metrics sampler and shared HTTPS probe

**Files:**
- Create: `mission_control/metrics.py`
- Modify: `tools/linux_system.py` (extract the per-host probe out of `check_network`)
- Test: `tests/test_mission_metrics.py`

**Interfaces:**
- Produces:
  - `tools.linux_system.probe_https(host: str, port: int = 443, timeout: float = 4.0) -> dict` returning `{"host", "dns": "ok"|"failed", "https_port": "reachable"|"unreachable (...)", "latency_ms"?: int}`; `check_network` calls it (its output keys unchanged).
  - `metrics.MetricsSampler(disk_path: Path = Path.home(), psutil_module=<psutil or None>)` with `available: bool` and `sample() -> dict | None` → `system.metrics` payload: `cpu_percent, memory_percent, memory_total, memory_available, memory_used, disk_percent, disk_total, disk_used` (bytes as ints), `uptime_s, process_uptime_s, load_avg: [1,5,15], os` (freedesktop PRETTY_NAME, cached). `cpu_percent(interval=None)`, primed in `__init__` (never blocks).
  - `metrics.probe_groq(timeout: float = 3.0) -> dict` → `network.status` payload `{"groq": "up"|"down", "latency_ms"?, "checked_at", "source": "probe"}`.

- [ ] **Step 1: Write the failing tests:** `test_sample_uses_numbers_from_psutil` (fake psutil module object), `test_cpu_percent_is_non_blocking` (asserts `interval=None`), `test_sampler_without_psutil_reports_unavailable` (`available is False`, `sample()` is None), `test_probe_https_ok_and_failure` (patch `socket.getaddrinfo` / `socket.create_connection`), `test_check_network_output_unchanged` (existing keys), `test_probe_groq_maps_status`.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `tests/test_mission_metrics.py tests/test_tools.py` — Expected: PASS; full suite green.
- [ ] **Step 5: Hand back.**

### Task 4: Server, settings, startup wiring and first events

**Files:**
- Create: `mission_control/server.py`, `mission_control/launch.py`
- Modify: `config/settings.py`, `jarvis.py` (`parse_args`, `main`, `run_checks`), `ui/terminal.py` (`handle_message`, `say`), `requirements.txt`, `.gitignore`
- Test: `tests/test_mission_server.py`, additions to `tests/test_config.py` and the `--check` tests

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces:
  - Config fields: `ui_enabled: bool` (`JARVIS_UI_ENABLED`, false), `ui_port: int` (`JARVIS_UI_PORT`, 8765, 1024-65535), `ui_open_browser: bool` (`JARVIS_UI_OPEN_BROWSER`, true), `ui_dev_origin: str` (`JARVIS_UI_DEV_ORIGIN`, "", must match `^http://(localhost|127\.0\.0\.1):\d+$` else `ConfigError`), `confirm_timeout: int` (`JARVIS_CONFIRM_TIMEOUT`, 600, 0-3600; 0 = never).
  - Flags `--mission-control`, `--no-browser`.
  - `server.create_app(hub: Hub) -> web.Application`; `server.Hub(bus, tracker, sampler, token, port, dev_origin="", bridge=None, broker=None)` (bridge/broker used from Task 9); `server.MissionControlServer(bus, *, port: int, dist_dir: Path, dev_origin: str = "", token: str | None = None, sampler=None, bridge=None, broker=None)` with `token`, `port` (real port when 0 requested), `start(timeout: float = 5.0) -> str` (base URL; raises `MissionControlError`), `stop(timeout: float = 3.0) -> None` (idempotent); `MissionControlError(Exception)`.
  - `launch.open_browser_securely(url: str) -> bool` — writes a mode-0600 redirect HTML file in `$XDG_RUNTIME_DIR` (fallback `tempfile.gettempdir()`), opens `file://...` with `webbrowser.open`, deletes it after ~10 s.
  - `jarvis.start_mission_control(config, cli, voice) -> MissionControlServer | None` — on `MissionControlError`/`ImportError` prints one yellow warning and returns None; prints `Mission Control: http://127.0.0.1:<port>/#token=<token>` to the terminal only; `main` stops it in `finally` before `voice.shutdown()`.
  - HTTP: middleware enforces Host in `{127.0.0.1:<port>, localhost:<port>}` (else 421) and Origin in `{http://127.0.0.1:<port>, http://localhost:<port>, dev_origin}` for any request carrying Origin and for every `/ws` (missing Origin on `/ws` → 403); headers CSP `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' ws://127.0.0.1:<port> ws://localhost:<port>; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cache-Control: no-store` (not on `/assets`). Routes: `GET /` (dist `index.html`, or inline "not built" page naming `cd mission_control/web && npm ci && npm run build`), `GET /assets/*` (static, `follow_symlinks=False`, immutable cache), `GET /api/health` → `{"ok": true, "protocol": 1}`, `GET /api/snapshot` (Bearer token, else 401), `GET /ws` (`heartbeat=20`, `max_msg_size=65536`).
  - WebSocket: first frame `{"type": "auth", "token": str, "last_seq": int | null}` within 3 s (else close 4408); bad token → close 4401 after 250 ms; success → `{"type": "auth.ok", "protocol": 1}` then replay of `bus.recent(last_seq)` or `{"type": "snapshot", seq, session, state, subsystems, task, pending_confirmation, metrics, network, recent_events (≤200)}`. Limits: 4 clients (5th → 4409), 20 msgs/s per client (→ 4429), per-client send queue 512 (overflow → close 1013). `ping` → `pong`; unknown type → `{"type": "error", "code": "invalid"}`. Runner uses `access_log=None, handle_signals=False`; thread `jarvis-mission-control` with its own loop; metrics every 2 s only while a client is connected; Groq probe every 60 s and on auth if the last is older than 30 s; `tracker.tick()` every 1 s.
  - Events now published: `session.start` (version, provider "groq", model, permission_mode, voice/mic/wake availability, memory/tasks status, developer_mode), `session.end`, `request.start` (source, text ≤500) / `request.end` (status complete|incomplete|cancelled|error, rounds, duration_ms, error_kind) in each exit branch of `handle_message` inside `BUS.request_scope`, `assistant.message` from `say`.
  - `--check`: line "Mission Control server" (aiohttp importable) and "Mission Control UI" (built / not built: `cd mission_control/web && npm ci && npm run build`).
  - `.gitignore`: `mission_control/web/node_modules/`, `mission_control/web/dist/`, `*.tsbuildinfo`.

- [ ] **Step 1: Write the failing tests** (aiohttp `test_utils.TestServer`/`TestClient` on `create_app`): `test_wrong_host_is_421`, `test_ws_without_or_with_foreign_origin_is_403`, `test_ws_auth_timeout_closes_4408` (timeout patched to 0.05), `test_ws_bad_token_closes_4401`, `test_ws_good_token_gets_auth_ok_then_snapshot`, `test_last_seq_replays_and_gap_sends_snapshot`, `test_snapshot_api_requires_bearer`, `test_missing_dist_serves_build_instructions`, `test_dist_served_with_security_headers`, `test_asset_traversal_is_404`, `test_fifth_client_refused_4409`, `test_rate_limit_closes_4429`, `test_oversized_message_closes`, `test_server_thread_start_stop_rebind` (real `MissionControlServer(port=0)`: start, connect, stop, start again on the same port; no leaked threads), `test_port_in_use_warns_and_terminal_still_runs` (`start_mission_control` returns None, policy/CLI untouched), `test_token_never_logged` (capturing handler on `jarvis` and `jarvis.audit` through a full auth cycle), `test_secret_in_request_text_is_redacted_in_events`, `test_request_events_from_handle_message` (FakeGroqClient: complete, KeyboardInterrupt → cancelled, JarvisAPIError → error), `test_mission_control_off_changes_nothing` (no server, `BUS.enabled` False), config tests for each new variable (defaults, ranges, bad dev origin), `--check` lines.
- [ ] **Step 2: Run** `tests/test_mission_server.py tests/test_config.py` — Expected: FAIL.
- [ ] **Step 3: Implement** server, launch, settings, flags, wiring, publish points, requirements, gitignore.
- [ ] **Step 4: Run** — Expected: PASS; full suite green.
- [ ] **Step 5: Hand back.**

### Task 5: Frontend foundation (live, read-only)

**Files:**
- Create: `mission_control/web/` — `package.json`, `package-lock.json`, `vite.config.ts`, `tsconfig.json`, `index.html`, `src/main.tsx`, `src/App.tsx`, `src/types/events.ts`, `src/types/guards.ts`, `src/store/reducer.ts`, `src/store/store.ts`, `src/services/token.ts`, `src/services/socket.ts`, `src/hooks/useMissionSocket.ts`, `src/hooks/useDocumentVisible.ts`, `src/components/scene/{Scene,HoloGrid}.tsx`, `src/components/core/{JarvisCore,ReactorSphere,OrbitalRings,ParticleField}.tsx`, `src/components/core/materials/holoMaterial.ts`, `src/components/core/stateVisuals.ts`, `src/components/nodes/{SubsystemNodes,SubsystemNode}.tsx`, `src/components/nodes/layout.ts`, `src/components/panels/{SystemMonitorPanel,ConnectionBanner}.tsx`, `src/components/layout/{HudLayout,TopBar}.tsx`, `src/styles/theme.css`
- Test: `src/test/{guards,reducer,socket,noInnerHtml}.test.ts`

**Interfaces:**
- Consumes: Task 2 fixtures (`../../protocol_fixtures.json`), Task 4 WebSocket protocol and close codes.
- Produces:
  - `types/events.ts`: discriminated union `ServerEvent` (one member per `EVENT_TYPES` key) plus `AuthOk`, `Snapshot`; `ClientMessage` = `auth | command.submit | request.cancel | confirm.answer | ping`; `TaskView`, `ConfirmPayload`, `JarvisState = 'idle'|'listening'|'processing'|'speaking'|'tool'|'error'|'offline'`.
  - `guards.ts`: `isServerEvent(x: unknown): x is ServerEvent` (unknown types → false).
  - `reducer.ts`: `reduce(state: MissionState, ev: ServerEvent | LocalEvent): MissionState` (pure; event log ring of 500); `LocalEvent` = `connection` status changes.
  - `store.ts`: zustand `useMission` with `dispatch(ev)`.
  - `token.ts`: `takeToken(): string | null` — reads `#token=`, stores in `sessionStorage`, strips the hash with `history.replaceState(null, "", location.pathname)`.
  - `socket.ts`: `class MissionSocket(url, token, onEvent, onStatus)` with `connect()`, `send(msg: ClientMessage)`, `close()`; backoff `0.5 s × 2^n`, cap 15 s, ±20 % jitter; no reconnect after 4401 / 4403 / 4409 (status `auth_failed`); reconnect if no message for 45 s; resends `last_seq`.
  - Visuals per Global Constraints: `ParticleField` = one `THREE.Points` with motion in the vertex shader (`uTime, uIntensity, uState`); 3 `TorusGeometry` rings; reactor `IcosahedronGeometry(1, 3)` with fresnel `holoMaterial`; grid as one `LineSegments`; six nodes on `layout.ts` positions labelled LLM CORE, MEMORY, TOOLS, SYSTEM, VOICE, NETWORK, greyed with "UNAVAILABLE" when the snapshot says so; `stateVisuals.ts` maps `JarvisState` → `{rotationSpeed, color, intensity}` (amber for error, dimmed for offline). All geometries/materials disposed on unmount.
  - Scripts: `dev` (`vite --port 5173 --strictPort`, proxy `/api` and `/ws` to `http://127.0.0.1:${JARVIS_UI_PORT||8765}`), `build` (`tsc -b && vite build`, outDir `dist`, manualChunks three / r3f, no sourcemaps), `test` (`vitest run`), `typecheck` (`tsc -b --noEmit`).

- [ ] **Step 1: Write the failing vitest tests:** guards accept every fixture and reject a mutated copy of each; reducer applies snapshot then events (state, metrics, log cap 500); `MissionSocket` with a fake WebSocket and fake timers — sends auth first, backs off 0.5/1/2 s with jitter bounds, `stops reconnecting after 4401`, reconnects after 45 s silence with `last_seq`; `noInnerHtml` scans `src/` for `dangerouslySetInnerHTML|innerHTML|eval(`.
- [ ] **Step 2: Run** `npm test` — Expected: FAIL.
- [ ] **Step 3: Implement** the scaffold, services, store and scene.
- [ ] **Step 4: Run** `npm test && npm run typecheck && npm run build` — Expected: PASS, `dist/index.html` exists. Manual: `python jarvis.py --mission-control`, page shows the core idle, metrics updating every 2 s, core turns to processing during a typed request, offline when JARVIS quits.
- [ ] **Step 5: Hand back** (controller commits source and lockfile; `dist/` and `node_modules/` stay ignored).

### Task 6: Full outbound telemetry

**Files:**
- Modify: `tools/registry.py` (`execute_tool`, `finish()`), `intelligence/groq_client.py` (`call_model`), `memory/json_store.py` (`get_relevant_memories`, `search_memory`, `save`), `memory/task_store.py` (`_transaction`, new pure `task_view(task: dict) -> dict`), `voice/output.py` (`speak`), `voice/wake.py` (`WakeWordListener.__init__(..., on_level=None)`, `UtteranceCollector.last_rms`), `ui/terminal.py` (`_on_wake`, `_on_command`, `_on_status`, `_voice_command`, `voice_turn`, `start_wake`, `stop_wake`, `on_progress` lambda, `_pause_active_task`), `jarvis.py` (wire `on_level`)
- Test: additions to `tests/test_tools.py`, `tests/test_orchestrator.py` (call_model tests), `tests/test_memory.py`, `tests/test_tasks.py`, `tests/test_voice_output.py`, `tests/test_wake.py`, `tests/test_terminal.py`

**Interfaces:**
- Consumes: Task 1 `emit`, `emit_throttled`, `scrub`; Task 2 payload fields.
- Produces: `tool.execution` statuses `requested` (before policy), `awaiting_confirmation` (when the policy will ask), `running` (before submit), final status with `duration_ms` (in `finish()`), `target` built like `_event` and scrubbed; `llm.call` start/end (model, attempt, duration_ms, ok, error_kind), `llm.retry` (reason, delay), `llm.error` (kind, message, retryable) before each raise; `memory.activity` op `retrieve|search|store` with `count`; `task.update` `{task: TaskView | null}` after a committed save only (`TaskView = {id, goal, status, steps: [{n, text, status}], updated_at, paused_reason?, untrusted_plan}`, from `active()` else `unfinished()[0]`); `task.progress` `{message}`; `voice.output` start / end / error (`timing: "none"` until Task 11); `voice.input` phases; `voice.level` `{rms, phase: "armed"|"collecting"}` throttled to 5 Hz armed / 10 Hz collecting; `notice` from `_pause_active_task` and developer-mode auto switch.

- [ ] **Step 1: Write the failing tests:** `test_tool_events_cover_lifecycle` (success, denied, unknown tool), `test_tool_target_url_secret_is_redacted` (`open_url` with `?api_key=gsk_…`), `test_llm_events_on_success_retry_and_error` (FakeGroqClient scripted errors), `test_memory_activity_events`, `test_task_update_after_commit_not_after_rollback` (patch `save_memory` to raise), `test_task_view_shape`, `test_voice_output_start_end_events` (`_synthesize_override`), `test_wake_on_level_throttled` (FakeDetector, `quiet()`/`loud()` frames), `test_cli_voice_input_phases`, `test_no_events_when_bus_disabled` (spot check registry + memory).
- [ ] **Step 2: Run** the touched test files — Expected: FAIL.
- [ ] **Step 3: Implement** the publish points (each one line or two; no behaviour change when the bus is disabled).
- [ ] **Step 4: Run** — Expected: PASS; full suite green.
- [ ] **Step 5: Hand back.**

### Task 7: Frontend interactions

**Files:**
- Create: `src/components/scene/CameraRig.tsx`, `src/components/nodes/{DataLink,NodeDetailPanel}.tsx`, `src/components/panels/{EventStreamPanel,TaskPanel}.tsx`, `src/hooks/{useAdaptiveFrameloop,usePerformanceMode}.ts`
- Modify: `Scene.tsx`, `SubsystemNode(s).tsx`, `HudLayout.tsx`, `TopBar.tsx`, `reducer.ts`
- Test: `src/test/{reducer,frameloop,eventStream}.test.ts`

**Interfaces:**
- Consumes: Task 6 event payloads.
- Produces: `CameraRig` (Drei `OrbitControls`, min/max distance, polar limits so the core stays in view, reset button, subtle pointer parallax, eased focus on the selected node, FOV/distance from viewport aspect); node hover/selection → `NodeDetailPanel` per subsystem (LLM: provider, model, status, last call duration; MEMORY: last op/count; TOOLS: active tool, status, recent list; SYSTEM: metrics + OS; VOICE: mic availability, input phase, speaking; NETWORK: Groq status, latency, checked_at); `DataLink` pulses + one `InstancedMesh` of ≤32 packets triggered by `tool.execution`, `llm.call`, `memory.activity`, `voice.*`; `EventStreamPanel` (timestamp, category chip, status, auto-scroll with "jump to latest", pause/resume with buffered count, clear, expandable detail rendered as text); `TaskPanel` (goal, steps with status, paused reason, untrusted warning); `usePerformanceMode()` → `'low'|'balanced'|'high'` persisted in localStorage (try/catch), `prefers-reduced-motion` forces low; Responsive (spec §12): below 900 px wide the side panels become overlays opened from the TopBar, particle count halves, the core stays centred and the CommandBar stays docked; `useAdaptiveFrameloop()` → `'never'` hidden, `'always'` for active states / camera interaction / 1.5 s after an event, else `'demand'` with invalidate at 15 fps (0 in low).

- [ ] **Step 1: Write the failing tests:** reducer handles every Task 6 event type and the subsystem details; `useAdaptiveFrameloop` decision function (pure helper `frameloopFor({hidden, state, interacting, lastEventAt, now, quality})`) table test; event stream paused buffer + clear.
- [ ] **Step 2: Run** `npm test` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `npm test && npm run typecheck && npm run build` — Expected: PASS. Manual: Chrome FPS meter in Balanced ≥ 30 fps on Emanuel's machine during a request; nodes clickable; camera reset works.
- [ ] **Step 5: Hand back.**

### Task 8: Shared confirmation view and broker

**Files:**
- Modify: `ui/terminal.py` (`print_details`, `terminal_confirm`)
- Create: `mission_control/confirm.py`
- Test: `tests/test_confirm_broker.py`, additions to `tests/test_terminal.py`

**Interfaces:**
- Produces:
  - In `ui/terminal.py`: frozen dataclass `ConfirmationView(label, tool, operation, affected_paths: tuple[str, ...], reason, hidden: int, lines: tuple[tuple[str, str], ...], omitted: int)` with `digest() -> str` (sha256 of canonical JSON); `build_confirmation_view(request: ConfirmationRequest) -> ConfirmationView` (exactly today's `reveal_invisible(clean_text(...))`, `MAX_DETAIL_LINES = 400`, line kinds `add|del|hunk|ctx`); `print_confirmation(view, prompt_hint: str = "") -> None`; `print_details` and `terminal_confirm` keep their behaviour, built on these.
  - In `mission_control/confirm.py`: `StdinAnswerReader.poll(timeout: float) -> str | None` (select + readline; `""` → `EOFError`); `ConfirmationBroker(reader=None, clock=time.monotonic, timeout: float = 600.0, isatty=lambda: sys.stdin.isatty(), publish=events.emit)` with `confirm(request) -> bool` (the policy's confirm callable), `answer_from_browser(confirm_id: str, digest: str, approved: bool) -> str` returning `"ok" | "stale" | "already_answered" | "digest_mismatch" | "terminal_only"`, `pending_payload() -> dict | None`.
  - Semantics: not a TTY → False, nothing published; prints the view as today with prompt `Type 'yes' to proceed (or answer in Mission Control), anything else cancels:`; publishes transient `confirm.request {id, digest, tool, label, operation, affected_paths, reason, hidden, lines: [{k, t}], omitted, browser_approvable, expires_at}` where `browser_approvable` is False when `redact()` changed any string in the view (browser may then only decline); first answer wins under one lock; timeout (0 = none) and EOF decline; `KeyboardInterrupt` re-raised (channel `cancelled`); `finally` clears the pending id, publishes `confirm.resolved {id, approved, channel: "terminal"|"browser"|"timeout"|"cancelled"}`, and writes `audit({"event": "confirmation", "tool", "approved", "channel"})`.

- [ ] **Step 1: Write the parity test first:** `test_terminal_output_byte_identical_after_refactor` — capture today's `print_details`/`terminal_confirm` output for diffs with bidi and zero-width characters, more than 400 lines, `+++`/`---` headers, and compare after the refactor (fixture captured before changing code).
- [ ] **Step 2: Write the broker tests:** terminal yes / no; browser approve and decline via a `FakeReader` that calls `answer_from_browser` on poll N; browser answer after terminal → `already_answered`; wrong id → `stale`; wrong digest → `digest_mismatch`; secret in diff → approve `terminal_only`, decline `ok`; timeout with fake clock declines; EOF declines; KeyboardInterrupt re-raised and resolved `cancelled`; non-TTY returns False and publishes nothing; `test_browser_decline_drops_developer_approval` (through `PermissionPolicy` + `tool.declined`); 8-thread race → exactly one winner; `test_pending_payload_in_snapshot_and_resolution_reaches_all_clients` (pending in snapshot, resolved published once).
- [ ] **Step 3: Run** `tests/test_confirm_broker.py tests/test_terminal.py` — Expected: FAIL (parity test passes against old code once the fixture is captured).
- [ ] **Step 4: Implement** the view refactor and broker.
- [ ] **Step 5: Run** — Expected: PASS; full suite green.
- [ ] **Step 6: Hand back.**

### Task 9: Inbound commands, cancel and wiring

**Files:**
- Create: `mission_control/bridge.py`
- Modify: `ui/terminal.py` (`attach_mission`, `request_cancel`, `_on_wake`, `_read_line`, `_step`, `_busy_scope`, `HELP_TEXT`), `mission_control/server.py` (message handlers), `jarvis.py` (`start_mission_control` builds broker + bridge, swaps `policy.confirm`, restores `terminal_confirm` on failure), `README.md` (confirmation wording)
- Test: additions to `tests/test_terminal.py`, `tests/test_mission_server.py`; new `tests/test_mission_e2e.py`

**Interfaces:**
- Consumes: Task 8 broker; Task 4 server.
- Produces:
  - `bridge.SubmitResult` enum `ACCEPTED, BUSY, QUEUE_FULL, TERMINAL_ONLY, INVALID, TOO_LONG`; `bridge.browser_command_policy(line: str) -> tuple[bool, str]`; `bridge.MissionBridge(inbox: queue.Queue, cancel: Callable[[], bool], broker: ConfirmationBroker)` with `busy: threading.Event`, `submit(command_id: str, text: str) -> SubmitResult` (text ≤ 4000), `cancel() -> bool`, `answer(confirm_id, digest, approved) -> str`.
  - Terminal-only from the browser: `/forget`, `/memory reset`, `/tasks reset`, `/wake on`, `/listen on`, `/talk`, and every `EXIT_WORDS` entry. Everything else (plain messages, "continue", `/help`, `/tools`, `/status`, `/memory [query]`, `/tasks`, `/tasks clear`, `/resume [id]`, `/abandon [id]`, `/dev …`, `/voice on|off`, `/stop`, `/clear`, `/wake off`, `/listen off`) allowed.
  - `JarvisCLI.attach_mission(bridge) -> None` (creates `_inbox = queue.Queue(maxsize=1)`, separate from `_wake_events`); `JarvisCLI.request_cancel() -> bool` (extracted from `_on_wake`: under `_interrupt_lock`, if `_busy`, set `_interrupt_pending` and `os.kill(os.getpid(), SIGINT)`; always `voice.stop()`); `_read_line` keeps the plain `input()` fast path when no mission is attached, otherwise polls `_wake_events`, `_inbox` and `select([stdin], 0.1)`; a browser line prints `You (browser): <text>` and sets source `browser`; browser slash commands run under a stdout tee that publishes `command.output {command_id, command, text ≤ 8000}` (ANSI stripped, scrubbed); `request.start` source `browser`.
  - WebSocket handlers: `command.submit {id, text}` → `command.ack {command_id, status: "accepted"|"queued"}` or `command.rejected {command_id, reason}`; `request.cancel`; `confirm.answer {id, digest, approved}` → `confirm.answer.result {id, result}`.
  - `HELP_TEXT` and README: approvals are typed in the terminal or answered in Mission Control; a spoken yes never approves.

- [ ] **Step 1: Write the failing tests:** `test_read_line_returns_browser_command_with_mission_attached` (patch `select.select`), existing input-only tests unchanged, `test_exit_word_from_browser_rejected`, parametrised `test_browser_command_policy`, `test_request_cancel_busy_sends_sigint` / `not_busy_only_stops_voice` (patch `os.kill`), `test_submit_while_busy_rejected`, `test_slash_command_output_captured_without_ansi`, `test_start_mission_control_restores_terminal_confirm_on_failure`, server handler tests for each inbound type, and `tests/test_mission_e2e.py::test_browser_command_tool_approval_reply` — real server on port 0, FakeGroqClient scripting an `edit_workspace_file` in a temp workspace; CLI loop on the test's main thread fed by the inbox; aiohttp client thread submits, approves via `confirm.answer`; assert the event order `request.start → tool.execution awaiting_confirmation → confirm.request → confirm.resolved(channel=browser) → tool.execution success → assistant.message → request.end`, and the file changed.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — Expected: PASS; full suite green.
- [ ] **Step 5: Hand back.**

### Task 10: Frontend command bar, approvals and cancel

**Files:**
- Create: `src/components/layout/CommandBar.tsx`, `src/components/panels/{ConfirmationPanel,ReviewLines}.tsx`
- Modify: `HudLayout.tsx`, `reducer.ts`, `socket.ts`
- Test: `src/test/{reviewLines,commandBar,reducer}.test.ts(x)`

**Interfaces:**
- Consumes: Task 9 inbound protocol; Task 8 `confirm.request` payload.
- Produces: `CommandBar` (bottom centre; Enter sends `command.submit` with `crypto.randomUUID()`; chip queued / running / rejected with reason / terminal-only; Cancel enabled while state is `processing` or `tool`; ↑/↓ local history; latest `assistant.message` and `command.output` shown above it as text); `ConfirmationPanel` (label, operation, paths, reason, `ReviewLines`, "!! N invisible characters shown as \u{…}" and "… M more lines not shown" exactly like the terminal, Approve/Decline sending `confirm.answer` with the payload's `digest`; amber banner "This looks like it contains a secret: answer in the terminal" and only Decline when `browser_approvable` is false; closes on `confirm.resolved`); `ReviewLines` = `<pre>` with one `<span className={kind}>` per line, `white-space: pre`.

- [ ] **Step 1: Write the failing tests:** `ReviewLines` renders `<script>` and `‮` escaped (`renderToStaticMarkup`); confirmation shows only Decline when not browser-approvable; reducer clears the panel on `confirm.resolved`; command chip transitions on ack / rejected.
- [ ] **Step 2: Run** `npm test` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `npm test && npm run typecheck && npm run build` — Expected: PASS. Manual: send "what time is it?" from the browser; ask for a file edit and approve it in the browser; cancel a long request from the browser.
- [ ] **Step 5: Hand back.**

### Task 11: Real speech pulse, polish and docs

**Files:**
- Modify: `voice/output.py` (`_synthesize`, `speak`), `src/hooks/useSpeechPulse.ts` (new), `JarvisCore.tsx`, `Scene.tsx`, `README.md`, `.env.example`, `jarvis.py` (`--check` wording if needed)
- Test: additions to `tests/test_voice_output.py`; `src/test/speechPulse.test.ts`

**Interfaces:**
- Produces: when `events.BUS.enabled`, `_synthesize` uses `edge_tts.Communicate(text, voice, boundary="WordBoundary").stream()` (audio chunks to the file; WordBoundary offsets/durations converted from 100 ns to ms) and returns `list[tuple[int, int]]`; otherwise unchanged `.save()`. `voice.output` gains `chunk {index, duration_ms?, words: [[offset_ms, dur_ms], …], timing: "exact"|"none"}` emitted right before each `_play`. `useSpeechPulse()` returns a 0-1 envelope from the current chunk's words, aligned to receipt time + 150 ms player latency. Polish: lazy `@react-three/postprocessing` Bloom in High only; Drei `PerformanceMonitor` drops one quality level on sustained low fps; keyboard focus and Enter on nodes; ARIA labels on panels; disposal check. README "Mission Control" section (start, build, dev mode with `JARVIS_UI_DEV_ORIGIN=http://localhost:5173`, security model, browser approval rules, performance modes, env vars) and `.env.example` entries for every `JARVIS_UI_*` variable and `JARVIS_CONFIRM_TIMEOUT`.

- [ ] **Step 1: Write the failing tests:** `test_word_boundaries_streamed_when_bus_enabled` (fake `edge_tts` module whose `stream()` yields audio and WordBoundary items), `test_save_path_unchanged_when_bus_disabled`; vitest envelope peaks inside word windows and is 0 between them.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full Python suite and `npm test && npm run typecheck && npm run build` — Expected: PASS. Manual (Emanuel): `python jarvis.py --mission-control --wake`, say "Hey JARVIS, what time is it?" and watch listening → processing → speaking with the pulse on the words; switch Low/Balanced/High; check `--check`.
- [ ] **Step 5: Hand back.**
