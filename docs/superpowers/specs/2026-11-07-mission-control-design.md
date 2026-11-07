# Design: JARVIS Mission Control (Phase 5)

Approved by Emanuel on 2026-11-07 (plan review). Source: his 15-section "3D Mission Control Implementation Prompt".

## Context

Emanuel wants an immersive 3D "Mission Control" for JARVIS MARK 50 (his full 15-section spec): a holographic
core whose states come from **real** backend events, six clickable subsystem nodes (LLM, Memory, Tools,
System, Voice, Network), real psutil metrics, a live event stream, a command bar that uses the **existing**
request handler, camera controls, a performance mode for his i5-7300U / Intel HD 620, typed code, and no
broken terminal features. Today JARVIS is terminal-only: no server, no frontend, and several real events
(request start/end, LLM calls, tool end, speaking) have no hooks yet.

User decisions: merge slice 2 first; approvals may be answered in the browser too (first answer wins);
frontend = React + TypeScript + Vite + three.js + React Three Fiber + Drei.

## Architecture (decided)

- **Off by default.** `python jarvis.py --mission-control` (or `JARVIS_UI_ENABLED=true`). With it off, the
  terminal app's code paths (`input()` reader, `terminal_confirm`) stay byte-for-byte as today.
- **Backend = the existing Python process.** aiohttp 3.14.5 is already installed (edge-tts dependency) → pin it in
  requirements.txt; no other new Python dependency. Server runs on its own thread + event loop
  (`jarvis-mission-control`), separate from the `jarvis-voice` loop; imported lazily.
- **Event bus** `core/events.py`: module-level `emit()` like `audit()`; `EventBus.publish` never raises, bounded
  ring (500) for reconnect replay, `request_scope(source)` tags events; `scrub()` = redact → clean → truncate on
  every string (tool args/URLs can hold secrets). No-op unless Mission Control is running.
- **State is derived on the backend** by a pure `mission_control/state.py:StateTracker`
  (error > tool > processing > speaking > listening > idle; `offline` is client-side when the socket drops).
- **Security:** bind 127.0.0.1 only; Host check (anti DNS-rebinding) + Origin allowlist; random per-session token
  sent in the first WebSocket message (never in URLs/logs; browser opened via a 0600 redirect file so the token
  is not in process argv; fragment stripped with `history.replaceState`); strict CSP; rate/size/client limits;
  Groq key never leaves the backend.
- **Approvals in the browser:** one `build_confirmation_view()` feeds both the terminal printout and the browser,
  so both show identical escaped content (`reveal_invisible`, 400-line cap). `ConfirmationBroker` replaces the
  policy's `confirm` callable only when Mission Control is on: terminal select-based prompt + browser answer, first
  wins, single-use id + content digest, cancel/EOF/timeout = decline. If redaction changed anything in the view
  (looks like a secret), the browser may only **decline**; approve must be typed. A spoken "yes" still never approves.
- **Commands from the browser** go through an inbox queue polled by `_read_line` on the main thread (same path as
  typed text), so `run_agent`, policy and tools are untouched. Cancel reuses the wake-word path
  (`request_cancel()` → `_interrupt_pending` + SIGINT). Busy → `command.rejected busy`. Slash-command output is
  teed back as `command.output`. Terminal-only: `/forget`, `/memory reset`, `/tasks reset`, turning the mic on
  (`/wake on`, `/listen on`, `/talk`), and exit words.
- **Frontend** `mission_control/web/` (Vite + React 19 + TS + three 0.187 + R3F 9 + Drei + zustand; no Tailwind,
  zod, or Framer Motion). Pure `reduce()` store, hand-written type guards checked against a shared
  `mission_control/protocol_fixtures.json` (pytest + vitest both validate it). `dist/` git-ignored, lockfile
  committed; server shows a "not built yet: npm ci && npm run build" page when dist is missing.
- **Performance (HD 620):** shader-driven `Points` particles (400/1200/3000 by quality), 3 torus rings, fresnel
  reactor, one `InstancedMesh` for data packets, DPR caps, `frameloop` never (tab hidden) / demand (idle, 15 fps
  breathing) / always (active); bloom only in High mode, lazy-loaded; `PerformanceMonitor` auto-degrade;
  dispose on unmount; no backdrop blur in Low.

## Event contract (server → browser, envelope `{v:1, seq, type, timestamp, request_id?}`)

`session.start/end`, `jarvis.state`, `request.start/end` (source terminal|voice|browser), `assistant.message`,
`llm.call` (start/end, model, duration), `llm.retry`, `llm.error` (kind), `tool.execution`
(requested|awaiting_confirmation|running|success|error|denied|partial|unverified, duration), `memory.activity`
(retrieve|search|store), `task.update` (TaskView) + `task.progress`, `voice.output` (start|chunk|end, real word
timings), `voice.input` (armed|wake_detected|collecting|transcribing|recording|idle), `voice.level` (throttled
rms), `confirm.request/resolved`, `command.ack/rejected/output`, `system.metrics` (psutil, every 2 s only while a
client is connected), `network.status` (Groq TCP probe every 60 s + passive from LLM calls), `notice`.
Browser → server: `auth`, `command.submit`, `request.cancel`, `confirm.answer`, `ping`.

## Phases (each ends with the full pytest suite green; from Phase 1 also `npm test && npm run build`)

1. **Visual foundation on a live read-only wire.** `core/events.py`; `mission_control/{protocol, state, metrics,
   server, launch}.py`; Config `ui_enabled/ui_port/ui_open_browser/ui_dev_origin/confirm_timeout`; flags
   `--mission-control`, `--no-browser`; `--check` lines; session/request/assistant/metrics/network events.
   Frontend scaffold: socket (reconnect/backoff, close codes), guards, store, scene, core, rings, particles, grid,
   six static nodes, system monitor, connection banner. Core shows real idle/processing/offline.
2. **Interactions + full telemetry.** Publishers in `tools/registry.execute_tool` (start, running, `finish()`),
   `intelligence/groq_client.call_model`, `MemoryStore`, `TaskStore._transaction`, `VoiceEngine.speak`,
   `WakeWordListener` (`on_level`), CLI voice callbacks. Camera rig (orbit, zoom limits, reset, parallax,
   focus), node detail panels, animated links/packets, event stream (filters, auto-scroll, pause, clear,
   expandable details), task panel, performance toggle, adaptive frameloop.
3. **Backend integration inbound.** `ConfirmationView` refactor (terminal parity test first),
   `mission_control/confirm.py` (broker), `mission_control/bridge.py` (submit/cancel/answer, allowlist),
   `JarvisCLI.attach_mission`, `request_cancel`, inbox polling, stdout tee; command bar, confirmation panel,
   cancel button; README/HELP wording on browser approvals.
4. **Polish + optimisation.** Word-boundary speech pulse (edge-tts `boundary="WordBoundary"` stream, only when
   Mission Control is on), lazy bloom, auto-degrade, reduced motion, keyboard/ARIA, disposal/memory audit, README
   "Mission Control" section, `.env.example`.

## Critical files

Modify: `jarvis.py` (flags, `start_mission_control`, shutdown order), `config/settings.py`, `ui/terminal.py`
(`_read_line`, `_on_wake`→`request_cancel`, `handle_message`, `_step`, `terminal_confirm`/`print_details`),
`tools/registry.py`, `intelligence/groq_client.py`, `memory/json_store.py`, `memory/task_store.py`,
`voice/output.py`, `voice/wake.py`, `tools/linux_system.py` (extract `probe_https`), `requirements.txt`,
`.gitignore`, `README.md`, `.env.example`.
New: `core/events.py`, `mission_control/*.py`, `mission_control/protocol_fixtures.json`, `mission_control/web/**`,
`tests/test_events.py`, `test_mission_state.py`, `test_mission_server.py`, `test_mission_metrics.py`,
`test_confirm_broker.py`.
Reuse: `core.audit.redact/clean_text/safe_display/reveal_invisible`, `print_details` logic, `_busy_scope` +
`_interrupt_lock` + SIGINT cancel path, `policy.evaluate` (unchanged), `tests/helpers.py` (`FakeGroqClient`,
`Recorder`, `FakeDetector`, `TempDirTestCase`), aiohttp `test_utils`.

## My calls (correctable)

- Approvals from the browser time out after 600 s (`JARVIS_CONFIRM_TIMEOUT`, 0 = never) only when Mission Control is on.
- A review that looks like it contains a secret can only be declined in the browser.
- Turning the microphone on, destructive resets and quitting stay terminal-only.
- `dist/` is not committed; you run `npm ci && npm run build` once (and after updates).

## Verification

- `.venv/bin/python -m pytest -q tests/ -p no:cacheprovider -W error::pytest.PytestUnraisableExceptionWarning -W error::ResourceWarning`
  stays green every phase; new tests cover the bus (no-raise, redaction, ring), state table, server security
  (Host/Origin/token/close codes/limits/CSP/missing dist), metrics with a fake psutil, broker race (first answer
  wins, digest, stale id, timeout, cancel), browser commands/cancel, and one end-to-end run on port 0 with
  `FakeGroqClient`: browser command → tool → browser approval → reply.
- Token never appears in logs (test); a `gsk_` string through every event type is redacted (test).
- Frontend: `npm test` (reducer, guards vs fixtures, socket backoff, escaping, no `innerHTML`), `npm run typecheck`,
  `npm run build`.
- Manual on Emanuel's machine: `python jarvis.py --mission-control --wake`, check fps in Chrome (Balanced ≥30),
  say "Hey JARVIS, what time is it?" and watch listening → processing → speaking; run a file edit and approve it
  in the browser; cancel from the browser; kill the network to see offline/error states; `--check` lines.
