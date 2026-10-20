# JARVIS MARK 50

JARVIS MARK 50 is a personal assistant for the Linux terminal, written as a single Python file (`jarvis.py`). It sends your requests to a Groq-hosted language model (default `openai/gpt-oss-120b`). The model can call 18 tools that JARVIS defines: web search, the clock, system status, a small JSON memory, Markdown notes, read-only file and git access inside one workspace folder, and launching a fixed list of desktop apps. Every tool call is checked against a permission policy that the model cannot change. Replies are printed and can also be spoken in a British voice. You type your messages, or speak them with opt-in push-to-talk or an opt-in hands-free mode that wakes on "Hey JARVIS". The model has no shell, cannot install software and cannot delete files. Think of it as a careful tool-using chat assistant with a JARVIS-style persona. It is not an autonomous agent.

- Version: 1.0.0 (`python jarvis.py --version`)
- Target: Zorin OS 18 (Ubuntu 24.04 "noble" base), Python 3.12. Requires Python 3.10 or newer.

## Status / verification

| Area | State |
| --- | --- |
| Web search, time, system status, processes, network check, memory, notes, path restrictions | Smoke-tested for real on Zorin OS 18 |
| Voice output (edge-tts plus `gst-play-1.0`) | Player detection confirmed; Zorin ships `gst-play-1.0` |
| Voice playback | A test sentence was synthesised in `en-GB-RyanNeural` and played through `gst-play-1.0` without errors |
| Live conversation with a Groq model | Verified on 2026-10-17 with `openai/gpt-oss-120b` and `openai/gpt-oss-20b`: the model called `get_time`, `search_web`, `remember_fact`, `open_youtube`, `search_memory` and `forget_fact`, and the sensitive delete was stopped at the confirmation prompt. |
| `llama-3.3-70b-versatile` (the original default) | No longer offered by Groq to this account, so the default is now `openai/gpt-oss-120b`. The startup check reports an unavailable model and lists alternatives. |
| Speech input: Groq transcription | Verified on 2026-10-17: `whisper-large-v3-turbo` and `whisper-large-v3` each returned an exact transcript of a generated test clip in about 0.4 s. Both models are available to the development account. |
| Speech input: full pipeline | Recorder process, WAV file, length and silence checks, Groq upload and transcript were verified end to end with a simulated microphone. The temporary files were removed afterwards. |
| Speech input: microphone | `arecord` opened the built-in microphone (HDA Intel PCH, CX8200 Analog) and wrote a valid 16 kHz mono WAV when stopped with SIGINT. The quiet room measured a peak of about 0.015, so the silence threshold was set to 0.03 (speech is typically 0.1 or more). |
| Speech input: real spoken voice | **Not tested yet.** Nobody has yet spoken into the microphone and checked the transcript. The recorder, silence checks, transcriber and CLI flow are covered by 69 automated tests. |
| Hands-free: engine install | Verified on 2026-10-17 on Python 3.12: openWakeWord 0.6.0 installs only with `pip install --no-deps openwakeword==0.6.0`, because its declared `tflite-runtime` dependency has no Python 3.12 build. The runtime dependencies come from `requirements-wake.txt`. Disk use is about 270 MB, mostly scipy (109 MB), onnxruntime (68 MB), scikit-learn (49 MB) and numpy (43 MB). The `hey_jarvis` model plus its feature models (about 7 MB) download once on first use. |
| Hands-free: wake-model scores | On generated speech, "Hey Jarvis, ..." in three different voices scored 0.998-0.999. JARVIS's own `en-GB-RyanNeural` reply scored 0.000. "Hello Jarvis, ..." scored 0.52, which is why the default threshold is 0.6. |
| Hands-free: CPU | Inference costs about 3 ms per 80 ms frame, or about 4% of one CPU core, for as long as hands-free mode is on. |
| Hands-free: end to end | Verified on 2026-10-17 with the real model and real Groq Whisper, on generated audio. (1) "Hey Jarvis, what is the weather like in Manila today?" from idle: one detection, transcript "what is the weather like in Manila today?". (2) "Hey Jarvis, stop" mixed over JARVIS's own voice while it was speaking: one detection, transcript "stop.", handled as a stop phrase. (3) JARVIS's voice alone: 0 detections. (4) Ordinary chatter without the wake word: 0 detections. |
| Hands-free: microphone | Real microphone, local only (nothing was transcribed): 6.1 s processed, maximum wake score 0.047 in a quiet room, no false triggers. The estimated room noise RMS was 0.0045, giving a speech threshold of 0.013. The push-to-talk recorder could open the microphone at the same time (PipeWire allows several capture clients). |
| Hands-free: real spoken "Hey JARVIS" | **Not tested yet.** Nobody has yet said "Hey JARVIS" into the microphone, so wake-up, interruption and end-of-request detection with a real voice are unverified. |

## Features

### Personality

JARVIS treats you as its creator: loyal, familiar and candid, in a British voice, addressing you as Sir. It thinks a command through and then carries it out, without asking permission or questions it can answer itself. If it sees a problem or a better approach it says so in one sentence, but still does what you asked when its tools allow. Its disagreement is advice, not refusal. This does not bypass any safety rule: sensitive actions still stop at the typed confirmation prompt, and it still cannot run shell commands, install software or delete files.

### Tools available to the model

Permission classes: **R** = read-only, **C** = controlled action, **S** = sensitive action (see [Permission system](#permission-system)).

| Group | Tool | Class | What it does |
| --- | --- | --- | --- |
| Information | `search_web` | R | DuckDuckGo search. Returns titles, URLs and snippets only; pages are not opened. |
| | `get_time` | R | Local date, time, weekday, UTC offset and timezone. |
| Browser | `open_youtube` | C | Opens a YouTube *search results* page in the default browser. Nothing plays. |
| | `open_url` | C | Opens an `http`/`https` address in the default browser (DuckDuckGo if none is given). |
| Memory | `remember_fact` | C | Saves a fact, with priority `normal` or `high`. Secrets are refused. |
| | `search_memory` | R | Keyword search over facts, or the newest facts for an empty query. Also returns the profile. |
| | `update_fact` | S | Replaces the text of a fact, by id (shows old and new text before asking). |
| | `forget_fact` | S | Permanently deletes a fact, by id. |
| | `set_user_profile` | C | Stores your name and/or location. |
| System | `system_status` | R | Hostname, OS, kernel, uptime, CPU load, memory, and disk usage for `/` and home. |
| | `list_processes` | R | Top processes by CPU or memory (1-25 rows). |
| | `check_network` | R | DNS and HTTPS (port 443) reachability of `api.groq.com` and `duckduckgo.com`. |
| Apps and projects | `launch_app` | C | Starts an app from the approved list (see `JARVIS_APPS`). |
| | `open_project` | C | Opens a workspace directory in the configured editor (`JARVIS_EDITOR`). |
| | `git_status` | R | `git status --short --branch` and `git diff --stat` for a repository in the workspace. |
| Files and notes | `list_directory` | R | Lists a workspace directory (up to 200 entries). |
| | `read_file` | R | Reads a text file in the workspace. Credentials in the content are redacted. |
| | `create_note` | C, or S when overwriting | Writes a Markdown note to the notes folder. Overwriting an existing note is sensitive. |

Built-in app names for `launch_app`: `vscode` (`code`, falls back to `codium`), `firefox`, `chrome` (`google-chrome` / `chromium` / `chromium-browser`), `files` (`nautilus` / `nemo` / `thunar`), `terminal` (`gnome-terminal` / `kgx` / `x-terminal-emulator`), `text editor` (`gnome-text-editor` / `gedit`), `calculator`, `system monitor`, `settings`. It also accepts aliases such as "vs code", "file manager", "task manager" and "google chrome".

### How a request is handled

- The model may call tools for up to `JARVIS_MAX_TOOL_ROUNDS` rounds (default 8). If it runs out of rounds, it is asked to say the task is incomplete and to summarise only what the tool results confirm.
- If the model repeats an identical tool call within one request, the call does not run again. The earlier result is reused, so an action never runs twice by accident.
- Every tool result carries a status (`success`, `error`, `denied`, `partial` or `unverified`). The system prompt tells the model to report that status accurately.
- Web results and file contents are marked as untrusted data, and the model is told not to follow instructions found inside them.
- A tool result longer than 10,000 characters is cut short before it is sent to the model.

### Voice input (push-to-talk)

- Off by default. Turn it on with `--listen`, `/listen on` or `JARVIS_STT_ENABLED=true`, or use `/talk` for a single spoken message.
- Press Enter on an empty line to start recording and Enter again to stop. The microphone is open only between those two key presses.
- The recording is transcribed by Groq's hosted Whisper (`whisper-large-v3-turbo` by default) using the same API key. The transcript is then handled exactly like a typed message.
- Details, privacy and safety checks: [Voice input](#voice-input).

### Hands-free mode

- Off by default. Turn it on with `--wake`, `/wake on` or `JARVIS_WAKE_ENABLED=true`. It needs the optional wake-word engine (`requirements-wake.txt`) and `arecord`.
- Say "Hey JARVIS", then your request. The wake word is detected locally with [openWakeWord](https://github.com/dscripka/openWakeWord). Only the request that follows is sent to Groq for transcription.
- Saying "Hey JARVIS" while JARVIS is speaking stops the speech. While it is working, it cancels the request like Ctrl+C. "Hey JARVIS, stop" only stops.
- Typing keeps working at the same time.
- Details, privacy and tuning: [Hands-free mode](#hands-free-mode-wake-word).

## Quick start

```bash
# 1. System packages
sudo apt install python3-venv python3-pip
sudo apt install mpv            # optional: a more robust MP3 player for voice (see Voice)

# 2. Virtual environment and dependencies
cd ~/Documents/jarvis-mark-50
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. API key: create one at https://console.groq.com/keys
cp .env.example .env
chmod 600 .env
nano .env                       # set GROQ_API_KEY=gsk_...

# 4. Check the setup (prints no secrets)
.venv/bin/python jarvis.py --check

# 5. Run
.venv/bin/python jarvis.py

# 6. Optional: run with push-to-talk voice input
.venv/bin/python jarvis.py --listen

# 7. Optional: hands-free mode ("Hey JARVIS"). Install the wake-word engine first.
# pip warns that tflite-runtime is not installed; that is expected and harmless.
.venv/bin/pip install --no-deps openwakeword==0.6.0
.venv/bin/pip install -r requirements-wake.txt
.venv/bin/python jarvis.py --wake
```

`--check` reports on the Python version, the required modules (`groq`, `ddgs`), the optional modules (`edge_tts`, `psutil`), the audio player, the API key, the model, the speech model (`JARVIS_STT_MODEL`), the microphone recorder (`arecord` or `pw-record`), the wake-word engine (with the configured `JARVIS_WAKE_MODEL`), the memory file, the workspace and the editor. If a key is set, it makes live calls to list the models available to your account. The speech model is checked only when a key is set. A missing speech model, recorder or wake-word engine is a warning, not a failure. Exit code 0 means all required checks passed, and 1 means at least one failed.

### Command-line options

| Flag | Effect |
| --- | --- |
| `--text` | Text-only for this run (no speech). `/voice on` can still turn speech on. |
| `--model MODEL` | Overrides `GROQ_MODEL` for this run. |
| `--read-only` | Forces permission mode `read_only` for this run. |
| `--listen` | Starts with push-to-talk on (see [Voice input](#voice-input)). Audio is sent to Groq. Can be combined with `--text`. |
| `--wake` | Starts with hands-free mode on (see [Hands-free mode](#hands-free-mode-wake-word)). Detection is local; the request after the wake word is sent to Groq. Can be combined with `--text` and `--listen`. |
| `--check` | Runs the environment diagnostics and exits. No API key is needed to start it. |
| `--version` | Prints the version and exits. |

At startup JARVIS confirms that the key works and that the model is offered to your account. A bad key (401), access denied (403) or an unavailable model stops it with exit code 2, as does a configuration error. A temporary failure such as a rate limit or a network problem only prints a warning, and JARVIS starts anyway.

## Configuration

Settings come from environment variables. `jarvis.py` automatically loads a `.env` file located **next to `jarvis.py`**. Variables already exported in your shell are never overridden by the file. `.env.example` lists every variable with its default.

| Variable | Default | Allowed values / notes |
| --- | --- | --- |
| `GROQ_API_KEY` | (none, required) | Key from console.groq.com/keys. Placeholder-looking values are rejected. A key that does not start with `gsk_` is logged as a warning. |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Any chat model id available to your Groq account. |
| `JARVIS_REASONING_EFFORT` | (model default) | `low`, `medium` or `high`. How hard a reasoning model such as gpt-oss thinks before answering. `high` gives more careful answers but uses more of the per-minute and daily token quota. Leave it empty for other models. |
| `JARVIS_VOICE` | `en-GB-RyanNeural` | An edge-tts voice name (`.venv/bin/edge-tts --list-voices`). |
| `JARVIS_TTS_ENABLED` | `true` | `true/false`, `1/0`, `yes/no`, `on/off` |
| `JARVIS_MEMORY_PATH` | `<dir of jarvis.py>/jarvis_memory.json` | Path; `~` is expanded. |
| `JARVIS_WORKSPACE` | `~/Projects` | Root for the file, project and git tools. |
| `JARVIS_NOTES_DIR` | `~/Documents/JARVIS Notes` | Where notes are written. |
| `JARVIS_LOG_DIR` | `$XDG_DATA_HOME/jarvis-mark-50/logs` (normally `~/.local/share/jarvis-mark-50/logs`) | Path. |
| `JARVIS_MAX_TOOL_ROUNDS` | `8` | 1-25 |
| `JARVIS_SEARCH_LIMIT` | `5` | 1-10. Also caps the model's own `max_results`. |
| `JARVIS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `JARVIS_HISTORY_TURNS` | `10` | 0-50 recent exchanges sent as context. |
| `JARVIS_PERMISSION_MODE` | `standard` | `read_only`, `standard`, `confirm_all` |
| `JARVIS_EDITOR` | `code` | Command used by `open_project`, split like a shell command (for example `codium` or `code --new-window`). |
| `JARVIS_SAVE_CONVERSATIONS` | `true` | Keep the bounded conversation log in the memory file. |
| `JARVIS_APPS` | (none) | Extra or replacement apps: `name=command; name2=command two`. A built-in name is replaced. |
| `JARVIS_STT_ENABLED` | `false` | `true/false` etc. Starts with push-to-talk on, like `--listen`. `/talk` works either way. |
| `JARVIS_STT_BACKEND` | `groq` | Only `groq`. Any other value is a configuration error. |
| `JARVIS_STT_MODEL` | `whisper-large-v3-turbo` | A Groq speech-to-text model id, for example `whisper-large-v3`. |
| `JARVIS_STT_LANGUAGE` | `en` | Language code passed to Whisper (for example `en`, `de`, `fr`). Unset means `en`. Set it to an empty value (`JARVIS_STT_LANGUAGE=`) for auto-detection. Must be a 2-3 letter code (for example `tl` for Tagalog); anything else stops startup with a configuration error. |
| `JARVIS_STT_MAX_SECONDS` | `60` | 5-300. Recording stops automatically after this many seconds. A hands-free request is capped at 20 s or this value, whichever is smaller. |
| `JARVIS_WAKE_ENABLED` | `false` | `true/false` etc. Starts with hands-free mode on, like `--wake`. |
| `JARVIS_WAKE_MODEL` | `hey_jarvis` | A built-in openWakeWord model name (`alexa`, `hey_mycroft`, `hey_jarvis`, `hey_rhasspy`, `timer`, `weather`) or a path to an `.onnx` file (`~` is expanded). An unknown name or a missing file makes hands-free unavailable; it does not stop startup. Only "Hey JARVIS" is removed from transcripts. |
| `JARVIS_WAKE_THRESHOLD` | `0.6` | 0.1-0.99. Wake-model score needed to trigger while JARVIS is not speaking. Lower is more sensitive. |
| `JARVIS_WAKE_SPEAKING_THRESHOLD` | `0.75` | 0.1-0.99. Score needed while JARVIS is speaking, that is, to interrupt it. |
| `JARVIS_WAKE_END_SILENCE` | `1.0` | 0.4-3.0 seconds of quiet that end a hands-free request. |
| `JARVIS_WAKE_CHIME` | `true` | `true/false` etc. Plays a short two-tone chime when the wake word is heard. It plays in text-only mode too, if an audio player is found. |

An invalid value, such as a number out of range, an unknown mode or a malformed `JARVIS_APPS` entry, stops startup with `Configuration error: ...` and exit code 2. `NO_COLOR` disables coloured output, and `XDG_DATA_HOME` moves the default log directory.

### Groq usage and limits

Free-tier limits are small. On the development account (October 2026), `openai/gpt-oss-120b` allowed 8,000 tokens per minute and 1,000 requests per day; a single tool-using question costs roughly 4,000 tokens, so asking several in quick succession triggers a short wait. JARVIS prints `Groq is rate-limiting requests... Retrying in Ns` while it waits. Check your current limits at https://console.groq.com/settings/limits.

- Every model call sends the definitions of all 18 tools, about 1.7k tokens, on top of the system prompt, up to 5 memories and the recent history.
- A question that uses tools takes at least two model calls: one to choose the tools and one to answer. Multi-step tasks take more. Startup and `--check` also make one call to list models.
- A tool-heavy session can therefore use up the daily allowance quickly. To stretch it, lower `JARVIS_HISTORY_TURNS`, or switch to another model with `GROQ_MODEL` or `--model`, since each model has its own quota.
- JARVIS retries transient failures automatically: per-minute rate limits, timeouts, network errors and server errors. It makes up to 3 retries with backoff, honours `retry-after`, and gives up if asked to wait more than 30 s. A malformed tool call from the model gets one retry at a lower temperature. An exhausted daily quota is not retried.
- Replies are limited to 1,024 completion tokens, at temperature 0.4.
- Each spoken message is one transcription request, including every hands-free request (even "Hey JARVIS, stop"). Speech models have their own free-tier limits, counted in audio seconds, separate from the chat model's quota. A transient transcription failure is retried once, waiting at most 5 s.

## In-chat commands

Talk in plain English: for example "search for the latest Python release", "remember that I prefer concise answers", "how is the system doing?", "show git status for my-project".

| Command | Effect |
| --- | --- |
| `/help` (or `help`) | Shows help. |
| `/tools` | Lists the tools and their permission classes (`create_note` shows "depends on arguments"). |
| `/memory [query]` | Shows the profile and up to 50 stored facts, filtered by the optional query. |
| `/memory reset` | Only when the memory file is corrupted: backs it up and starts a fresh memory (asks first). |
| `/forget <id>` | Deletes one memory (asks first). Ids are shown by `/memory`. |
| `/voice on` / `/voice off` | Turns spoken replies on or off. |
| `/stop` | Stops speaking. |
| `/talk` | Records and sends one spoken message, whether push-to-talk is on or off. |
| `/listen on` / `/listen off` | Turns push-to-talk on or off. `/listen` alone shows the current state. |
| Enter on an empty line | With push-to-talk on: starts recording. Press Enter again to stop. |
| `/wake on` / `/wake off` | Turns hands-free mode on or off. `/wake off` closes the microphone. `/wake` alone shows the current state. |
| "Hey JARVIS" (spoken, hands-free on) | Starts a spoken request. While JARVIS is speaking or working, it interrupts first (see [Interrupting JARVIS](#interrupting-jarvis)). |
| `/clear` | Forgets this session's conversation context. Saved memories are unaffected. |
| `/status` | Shows the model, voice, permission mode, memory file, workspace, notes, logs, tool-round limit, the speech backend and model with the push-to-talk state, and whether hands-free is on, with the wake model and threshold. |
| `exit`, `quit`, `goodbye`, `bye jarvis`, Ctrl+D | Leaves JARVIS. |
| Ctrl+C while JARVIS is working | Cancels the current request. Steps that already finished are not undone. |

## Permission system

Each tool has one of three permission classes:

- **Read-only**: looks at things and changes nothing.
- **Controlled action**: changes something in a limited, low-risk way, such as opening a browser tab, launching an app, saving a memory, or creating a new note.
- **Sensitive action**: destructive or irreversible: changing or deleting a memory, or overwriting an existing note.

The permission mode decides what happens to each class:

| Class | `read_only` | `standard` (default) | `confirm_all` |
| --- | --- | --- | --- |
| Read-only | runs | runs | runs |
| Controlled action | denied | runs (asks you if the request is *tainted*, see below) | asks you |
| Sensitive action | denied | asks you | asks you |

**Prompt-injection guard (tainting).** `search_web`, `read_file`, `list_directory` and `git_status` return third-party text that could contain hidden instructions. Once one of them has returned content during a request, every later controlled action in that same request also needs your confirmation, and the prompt says why. This stops a booby-trapped web page or file from quietly opening a URL that carries your data, or writing an "instruction" into memory. The taint resets with each new message you type.

How confirmation works:

- JARVIS itself prints the prompt and reads your answer from the terminal (`input()`). The model has no part in it. The prompt shows the tool, the exact operation (for example, which memory will be deleted and its current text) and the affected paths.
- Type `yes` (or `y`) to proceed. Anything else cancels: another word, Enter, Ctrl+C or Ctrl+D.
- Each confirmation covers a single operation. There is no "always allow".
- If standard input is not an interactive terminal, operations that need confirmation are denied.
- A denied operation goes back to the model with status `denied`, and the model is told not to describe it as done.

The permission mode only governs tool calls made by the model. The `/forget` and `/memory reset` commands that you type always ask for confirmation, and they work in `read_only` mode too. The conversation log is also still saved in `read_only` mode (turn it off with `JARVIS_SAVE_CONVERSATIONS=false`).

## Filesystem boundaries

- **Workspace root** (`JARVIS_WORKSPACE`, default `~/Projects`): `list_directory`, `read_file`, `open_project` and `git_status` only work inside it. Relative paths are resolved against the root. Absolute paths are accepted only if they lie inside it.
- **Symlinks are resolved** before the check, so a link inside the workspace that points outside it is refused.
- **Protected names**: a path is refused if any part of it below the workspace root matches one of these. `list_directory` hides matching entries and reports how many it hid.
  - Directories: `.ssh`, `.gnupg`, `.aws`, `.azure`, `.kube`, `.docker`, `.password-store`, `.pki`, `.mozilla`, `google-chrome`, `chromium`, `BraveSoftware`, `keyrings`, `.git`
  - Files: `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.kdbx`, `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`, `.netrc`, `.pgpass`, `.git-credentials`, `.npmrc`, `.pypirc`, `credentials`, `credentials.*`, `secrets.*`, `*.secret`, `master.key`, `.envrc`, `*_history` (shell histories), `.htpasswd`
  - Still allowed: `.env.example`, `.env.sample`, `.env.template`
- **`read_file`** only reads text files; files with NUL bytes are treated as binary and refused. It reads at most the first 200 KB and returns 6,000 characters by default (`max_chars` 100-9,000). Known credential formats in the content are redacted.
- **`git_status`** is read-only and hardened against hostile repositories: it overrides `core.fsmonitor`, `core.hooksPath` and every configured `filter.*` driver, disables external diff and textconv programs, credential prompts, optional locks and system config, and ignores inherited `GIT_*` variables. `GIT_CEILING_DIRECTORIES` stops git from finding a repository above the workspace (such as a dotfiles repo in `$HOME`), and the repository's top level must be inside the workspace. These protections are covered by tests that run real git against a booby-trapped repository.
- **Notes folder** (`JARVIS_NOTES_DIR`) is separate from the workspace, and `create_note` can only write there. The file name comes from the title (letters, digits, `.`, `_`, `-`; spaces become `-`; at most 80 characters; `.md` added), so a title cannot escape the folder. A `# Title` heading is added if the content does not start with one. Notes are written atomically. An existing note is replaced only when `overwrite` is set, which is a sensitive action.
- There is no general shell, no delete tool and no way to write files outside the notes folder and the memory file.

## Memory

- **File**: `jarvis_memory.json` next to `jarvis.py` (or `JARVIS_MEMORY_PATH`). It is created automatically if missing and is gitignored because it holds personal data. Writes go to a temporary file, are fsync'd, then renamed into place, with file mode `600`.
- **Schema**:

  ```json
  {
    "user_name": null,
    "location": null,
    "facts": [
      {"id": "f1a2b3c", "text": "...", "created_at": "2026-10-17T21:00:00+02:00",
       "updated_at": null, "source": "user", "priority": "normal"}
    ],
    "conversations": [
      {"timestamp": "...", "user": "...", "assistant": "..."}
    ]
  }
  ```

  Unknown keys are kept, and older plain-string facts are upgraded automatically.
- **Limits**: 500 facts, 500 characters per fact.
- **Deduplication**: a fact that matches an existing one after ignoring case, extra whitespace and trailing `.`/`!` is not stored twice. JARVIS replies "Already remembered".
- **Secret refusal**: facts and profile values are refused if they look like credentials. That covers known key formats (Groq `gsk_`, `sk-`, GitHub, Slack, AWS, Google, PEM private keys, Bearer tokens), phrases such as "password is ..." or "token: ...", 13-19 digit numbers that may be card numbers, and long random-looking tokens. This is a heuristic, so false positives are possible.
- **Context per turn**: up to 5 facts are added to the system prompt with each request, ranked by keyword overlap with your message, then high priority, then most recent. Once 5 or more facts exist, five are always sent even if none match. Your name and location are sent too.
- **Conversation log**: if `JARVIS_SAVE_CONVERSATIONS=true`, the last 50 exchanges are kept, with each side redacted and cut to 500 characters. The log is a record only and is not sent back to the model.
- **Corruption handling**: if the file is not valid JSON or not a JSON object, JARVIS never overwrites it. It starts with an empty memory held only in RAM, shows a warning in the banner and tells the model that memories cannot be saved. You can repair the file by hand, or type `/memory reset`. After you confirm, it renames the broken file to `jarvis_memory.corrupt-YYYYMMDD-HHMMSS.json` and starts a fresh memory.

## Voice

- Speech uses [edge-tts](https://github.com/rany2/edge-tts) with the voice `en-GB-RyanNeural` by default (`JARVIS_VOICE`).
- **Privacy**: edge-tts uses Microsoft's online speech service, so the text of every spoken reply is sent to Microsoft for synthesis, and voice needs an internet connection. Use `--text`, `/voice off` or `JARVIS_TTS_ENABLED=false` if that is not acceptable. Groq also sees your messages and the tool results, because it hosts the model.
- edge-tts produces MP3. JARVIS plays it with the first player it finds, in this order: `mpv`, `ffplay`, `mpg123`, `gst-play-1.0`. Zorin OS ships `gst-play-1.0`, so voice usually works out of the box. Otherwise run `sudo apt install mpv`.
- Replies are cleaned up before speaking. Code blocks become "I've put the code on screen", URLs become "the link on screen", and Markdown symbols are dropped. Long replies are split into chunks of about 350 characters, and the next chunk is synthesised while the current one plays.
- A new reply, starting a new request or, in hands-free mode, saying "Hey JARVIS" interrupts speech. `/stop` stops it.
- **Fallback**: text is always printed. Voice failures never stop the assistant. The first failure prints a notice. After 3 consecutive failures JARVIS switches to text-only, and `/voice on` retries. If edge-tts is not installed or no player is found, JARVIS starts text-only and says why.
- Error replies from Groq are printed but not spoken. Temporary MP3 files live in a `jarvis-tts-*` directory in the system temp folder and are deleted on exit.

## Voice input

Push-to-talk speech-to-text. You press a key to start and stop every recording, and nothing listens in the background. For a wake word and continuous listening, see [Hands-free mode](#hands-free-mode-wake-word).

### Using it

```bash
.venv/bin/python jarvis.py --listen        # or type /listen on inside JARVIS
```

1. Press **Enter on an empty line** (or type `/talk`). JARVIS prints `[mic] Listening... press Enter to stop (limit 60s, Ctrl+C cancels).`
2. Speak, then press **Enter** to stop.
3. JARVIS prints `Transcribing...`, then `You (voice): <transcript>`, and sends the transcript straight away. There is no step to edit it first.

- **Ctrl+C while recording** cancels: the recording is discarded and nothing is sent. Ctrl+C during `Transcribing...` cancels the turn, but by then the audio may already have been uploaded.
- **Time limit**: recording stops automatically after `JARVIS_STT_MAX_SECONDS` (default 60 s, allowed 5-300). JARVIS asks you to press Enter, then transcribes what was recorded.
- **Typing still works** with push-to-talk on. Only an empty line starts a recording. `/talk` records one message even when push-to-talk is off.
- The transcript is handled exactly like typed text, so a transcript of just "exit" or "goodbye" ends the session, and "help" shows the help.
- `--text` only turns off spoken replies, so `--text --listen` gives voice input with text-only replies.
- **Recorder**: JARVIS uses the first recorder it finds: `arecord` (package `alsa-utils`), then `pw-record` (package `pipewire`). Zorin OS 18 includes both by default. It records the default input device as 16 kHz mono 16-bit WAV. Choose the microphone in Settings > Sound > Input. `wpctl status` lists the available sources. `pactl` was not installed on the Zorin OS 18 test machine, so use `wpctl` instead.
- If no recorder is found, push-to-talk stays off and JARVIS prints `Speech input unavailable: ...`. Everything else keeps working.
- Speech-input errors (microphone, network, Groq) print a message. They never end the session.

### Privacy

- **Opt-in, never always-on.** Push-to-talk records nothing unless you start a recording with Enter (push-to-talk on) or `/talk`. The microphone is open only between the two key presses, or until the time limit. Hands-free mode is different: while it is on, the microphone stays open (see [Hands-free privacy](#hands-free-privacy)).
- **Audio goes to Groq.** Each recording is uploaded to Groq's speech-to-text API (`JARVIS_STT_MODEL`) with your `GROQ_API_KEY`. The local WAV file lives in a `jarvis-stt-*` directory in the system temp folder. It is deleted straight after transcription, and also when a recording is rejected or cancelled. The directory is removed on exit.
- **One disclosure per session.** Before your first recording in a session, JARVIS prints: `Privacy: Your recording is sent to Groq (<model>) for transcription. JARVIS deletes its local copy straight afterwards.`
- **The audit log never contains the transcript.** A `speech_input` event records only the mode, the backend, the outcome, the duration in seconds and the number of characters. After transcription, the transcript is your message: like typed text, it goes to the chat model and, with `JARVIS_SAVE_CONVERSATIONS=true`, into the redacted conversation log.
- **Spoken confirmations are impossible.** Confirmation prompts read only typed input from the terminal. Push-to-talk never records while a prompt waits. In hands-free mode the listener keeps running, but saying "Hey JARVIS" at a prompt cancels the whole request. Saying "yes" cannot approve an action; you must type `yes`.
- **JARVIS does not hear itself during push-to-talk.** It stops speaking before every recording starts.

### Safety checks

These run on the local WAV before anything is uploaded:

| Check | Result |
| --- | --- |
| Shorter than 0.4 s | `That recording was too short. Press Enter, speak, then press Enter again.` Nothing is uploaded. |
| Peak level below 0.03 of full scale (silence) | `I heard only silence. Check that the microphone is unmuted and selected in Settings > Sound > Input.` Nothing is uploaded. |

After transcription, phrases Whisper tends to invent for near-silent audio ("Thank you.", "Thanks for watching!", "Thank you for watching.", "you", "Bye.", ".") are dropped if the recording's peak level was below 0.08. JARVIS then prints `I didn't catch anything.` and sends nothing. The same phrases spoken at a normal level are kept.

## Hands-free mode (wake word)

Say "Hey JARVIS", then your request. The wake word is detected on this computer by [openWakeWord](https://github.com/dscripka/openWakeWord); only the request that follows is sent to Groq for transcription. Saying the wake word while JARVIS is speaking or working interrupts it. Hands-free mode is off by default and opt-in.

### Installing the wake-word engine

The engine is optional and is not part of `requirements.txt`.

```bash
.venv/bin/pip install --no-deps openwakeword==0.6.0
.venv/bin/pip install -r requirements-wake.txt
```

- `--no-deps` is needed because openWakeWord 0.6.0 declares `tflite-runtime` as a dependency, and `tflite-runtime` has no build for Python 3.12. JARVIS uses openWakeWord's ONNX models through onnxruntime instead. pip warns that `tflite-runtime` is not installed; that is expected and harmless.
- `requirements-wake.txt` installs onnxruntime, numpy, scipy, scikit-learn, tqdm and requests. Expect about 270 MB on disk, mostly scipy (109 MB), onnxruntime (68 MB), scikit-learn (49 MB) and numpy (43 MB).
- The first time hands-free starts, JARVIS prints `Downloading the 'hey_jarvis' wake-word model (about 7 MB, one time only)...`. The model and its feature models are saved inside openWakeWord's package folder in `.venv` and reused afterwards. A new virtual environment downloads them again.
- The listener streams audio with `arecord` (package `alsa-utils`). `pw-record` is not used for hands-free.
- `--check` reports `wake-word engine: openWakeWord (hey_jarvis)` when all of its runtime packages (openwakeword, onnxruntime, numpy, scipy, scikit-learn, tqdm, requests) are installed, or a warning naming the first missing one. It checks without importing them and does not download or load the model.

### Using it

```bash
.venv/bin/python jarvis.py --wake            # or type /wake on inside JARVIS
.venv/bin/python jarvis.py --wake --text     # hands-free input, text-only replies
.venv/bin/python jarvis.py --wake --listen   # hands-free plus push-to-talk
```

When it starts, JARVIS prints: `Hands-free on: listening on this computer for "Hey JARVIS". Nothing leaves this machine until you say it; then only your request is sent to Groq for transcription. /wake off to stop.`

1. Say **"Hey JARVIS"**. JARVIS plays a short two-tone chime (turn it off with `JARVIS_WAKE_CHIME=false`) and prints `[wake] Listening...`.
2. Say your request, either in one breath ("Hey JARVIS, what time is it?") or after a pause. You have 5 s after the wake word to start speaking. Otherwise JARVIS prints `I didn't hear a command. Say "Hey JARVIS" to try again.` and sends nothing.
3. Stop talking. A 1 s pause ends the request. When the chime is on, sound in the first 0.8 s after the wake word does not count as speech, so the chime itself cannot end a request early (`JARVIS_WAKE_END_SILENCE`, 0.4-3.0 s). A request also ends after 20 s (or `JARVIS_STT_MAX_SECONDS` if smaller); use push-to-talk for longer dictation. Once you have started speaking, anything quieter than a quarter of your own speaking level counts as the pause, so a steady fan or background voices cannot keep the recording open. The background level JARVIS learns is capped (0.02 RMS) and never learned from its own voice, so a slowly rising noise cannot make it deaf.
4. JARVIS prints `Transcribing...`, then `You (voice): <transcript>`, and sends the transcript straight away, like push-to-talk.

- **Typing still works** at the same time. Whichever comes first, a typed line or a spoken request, is handled. Status lines such as `[wake] Listening...` can appear in the middle of a line you are typing; what you typed is kept.
- The transcript is handled like typed text, so "Hey JARVIS, goodbye" ends the session and "Hey JARVIS, help" shows the help.
- If only the wake word was heard, JARVIS prints `I didn't catch a request after the wake word.` and sends nothing to the model. An empty hands-free turn never starts a push-to-talk recording, even with push-to-talk on.
- While a request is being collected, the wake word is not detected again. After each request, detection pauses for 1.5 s. The 5 s start window and the 1.5 s pause are fixed.
- While a push-to-talk recording runs (`/talk`, or Enter with push-to-talk on), the hands-free listener ignores the microphone. It keeps the capture device open, which is fine with PipeWire (Zorin OS 18's default) but would make push-to-talk fail with "busy" on a raw ALSA `hw:` device.
- If the microphone stream ends, JARVIS prints `Hands-free listening stopped: the microphone stream ended. Type /wake on to try again.` Everything else keeps working.
- `/wake off` stops the listener and closes the microphone, and discards any request it had captured but not yet transcribed. Leaving JARVIS does the same. If the listener crashes, its microphone is closed too. `/status` shows whether hands-free is on.

### Interrupting JARVIS

| You say | While JARVIS is speaking | While JARVIS is working (thinking or running tools) | While idle |
| --- | --- | --- | --- |
| "Hey JARVIS, ..." plus a request | Speech stops at once, then the new request is handled. | The request is cancelled exactly like Ctrl+C, then the new request is handled. | The request is handled. |
| "Hey JARVIS, stop" (or another stop phrase) | Speech stops. Nothing is sent to the model. | The request is cancelled. Nothing new is sent to the model. | JARVIS prints `Stopped.` |

- After an interruption JARVIS prints `[wake] Interrupted. Listening...` instead of `[wake] Listening...`.
- A cancelled request prints `Request cancelled. Any step that had already finished is not undone.` Anything already completed stays done, and a tool that was already running may still finish (see [Known limitations](#known-limitations)).
- At a confirmation prompt, the wake word cancels the whole request: the action does not run and the model gets no further turn. A spoken "yes" can never approve an action.
- The wake word also interrupts a spoken request that is still being transcribed, so "Hey JARVIS, stop" works at every stage.
- **Stop phrases** only stop. The whole transcript, ignoring case and punctuation, must be one of: "stop", "stop it", "stop talking", "cancel", "never mind" ("nevermind"), "quiet", "be quiet", "shush", "that's enough" ("thats enough"), "enough", "nothing", "forget it". Anything longer, such as "stop, please", is sent to the model as a request.
- **Why the wake word and not any sound.** The laptop microphone hears JARVIS's own voice from the speakers, and JARVIS has no acoustic echo cancellation, so interrupting on any loud sound would make it interrupt itself. The wake model scored JARVIS's own voice at 0.000 in testing. While JARVIS is speaking, the threshold is also raised from 0.6 to 0.75 (`JARVIS_WAKE_SPEAKING_THRESHOLD`). Headphones make interruption more reliable, because the microphone then does not pick up JARVIS's voice.

### Hands-free privacy

- **Opt-in, off by default.** Only `--wake`, `/wake on` or `JARVIS_WAKE_ENABLED=true` turns it on.
- **The microphone stays open while it is on.** An `arecord` process streams 16 kHz mono audio to JARVIS continuously until `/wake off` or exit.
- **Detection is local.** The wake-word model runs on this computer. No audio leaves the machine, and none is written to disk, until the wake word is heard.
- **After the wake word, only the request is sent to Groq** for transcription (`JARVIS_STT_MODEL`, with your `GROQ_API_KEY`). The upload includes about 1 s of pre-roll from just before the detection, so it can contain the wake word itself and, during an interruption, some of JARVIS's own speech. The audio is written to a temporary WAV in the `jarvis-stt-*` directory and deleted straight after transcription.
- **The wake phrase is removed from the transcript**, together with anything before it, before the transcript is shown or sent to the model. If Whisper did not write the wake phrase, nothing is removed.
- **The same local checks as push-to-talk** run before upload: audio shorter than 0.4 s or with a peak below 0.03 is not uploaded (`I didn't hear a command after the wake word.`), and the invented-phrase filter applies afterwards.
- **Audit events never contain audio or transcripts.** Each detection writes a `wake_word` event with the model name and the score. Each request writes a `speech_input` event with `mode` `wake` and outcome `transcribed` or `silent`.
- The startup line shown above is the session's privacy disclosure; the push-to-talk disclosure is not repeated after it.

## Logging and audit trail

| File | Contents | Rotation |
| --- | --- | --- |
| `~/.local/share/jarvis-mark-50/logs/jarvis.log` | Application log at `JARVIS_LOG_LEVEL` | 1 MB, 3 backups |
| `~/.local/share/jarvis-mark-50/logs/audit.log` | One JSON line per event | 1 MB, 5 backups |

- The log directory is created with mode `700` (change the location with `JARVIS_LOG_DIR`). If it cannot be written, JARVIS prints a warning and runs without logs.
- Audit events:
  - `session_start`, with the model and permission mode
  - `tool_call`, with the tool, whether the policy allowed it, the result status, the duration, and a short summary of the message, error or result keys
  - `speech_input`, with the mode (`push_to_talk` or `wake`), the speech backend, the outcome (`transcribed`, `too_short`, `silent` or `cancelled`; hands-free requests use only `transcribed` and `silent`), the recording length in seconds and the transcript's character count (never the transcript itself).
  - `wake_word`, with the wake-word model name and the detection score (never audio)
  - `session_end`
- Tool arguments are not written to the audit log.
- Everything written to either log passes through redaction: API keys and tokens in the formats listed under Memory, private keys, Bearer tokens, and `password=` / `token is ...` style assignments become `[REDACTED]`. The HTTP client is kept at WARNING level, so request headers (and with them the key) are never logged.

## Running tests

```bash
.venv/bin/pip install -r requirements.txt   # includes pytest
.venv/bin/python -m pytest -q
```

Tests live in `tests/`. `jarvis.py` accepts injected fakes (browser opener, search client, speech synthesiser, confirmation callback) so the tests can run without real side effects.

## Troubleshooting on Zorin OS

| Symptom | Fix |
| --- | --- |
| No sound / "No audio player found" | Run `sudo apt install mpv`. If you rely on `gst-play-1.0`, make sure the MP3 plugins are present: `sudo apt install gstreamer1.0-plugins-base-apps gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly gstreamer1.0-libav`. Check the output device in Settings > Sound. `--check` shows which player was found. |
| "Voice output failed (no network connection?)" | edge-tts needs internet access to Microsoft's speech service. Check the connection, VPN and firewall. If it fails while the network is fine, Microsoft may have changed its service; try `.venv/bin/pip install -U edge-tts`. Use `--text` in the meantime. |
| "Groq rejected the API key (401)" | Check the key at console.groq.com/keys. An exported `GROQ_API_KEY` in your shell beats `.env`, so run `unset GROQ_API_KEY` if an old one is exported (for example in `~/.bashrc`). |
| "The model ... is unavailable or retired" / "isn't available to this account" | Set `GROQ_MODEL` (or use `--model`) to a current model from https://console.groq.com/docs/models. `--check` suggests models your account can use. |
| "Groq is rate-limiting requests; please wait a moment" | This is a per-minute limit. JARVIS already retried. Wait a moment and ask again. |
| "The Groq usage quota for this model is exhausted" | This is the daily limit. Wait for it to reset or switch `GROQ_MODEL`. See https://console.groq.com/settings/limits. |
| "DuckDuckGo is rate-limiting searches" | Wait a minute or two and search less often. Shared IPs such as VPNs hit this more often. |
| `open_project` says "The configured editor 'code' is not installed" | Install VS Code from the `.deb` at https://code.visualstudio.com (`sudo apt install ./code_*.deb`) or with `sudo snap install code --classic`. Or set `JARVIS_EDITOR`, for example `codium` or `flatpak run com.visualstudio.code`. |
| `--check` warns the workspace does not exist / list_directory says `'.' does not exist` | Run `mkdir -p ~/Projects`, or set `JARVIS_WORKSPACE` to the folder that holds your projects. |
| `open_url` / `open_youtube` report success but nothing opens | JARVIS can only confirm that it sent the request to the browser. Check `xdg-settings get default-web-browser`, set one with `xdg-settings set default-web-browser firefox.desktop`, and make sure you are running inside the desktop session, not over SSH. |
| "Memory file ... is not valid JSON" | Find the error with `python3 -m json.tool jarvis_memory.json` and fix it, or type `/memory reset` in JARVIS to back the file up and start fresh. |
| `Configuration error: ...` on startup | A variable in `.env` or your environment has an invalid value. The message names it. |
| "I heard only silence" | The microphone is muted, or the wrong input is selected. Check Settings > Sound > Input (input level and device). `wpctl status` lists the sources and shows the default one. Test outside JARVIS with `arecord -d 3 /tmp/t.wav && aplay /tmp/t.wav`. |
| "The microphone could not be opened (... busy ...)" | Another program is using the microphone, for example a video call or a browser tab. Close it and try again. |
| "No microphone recorder found" / "Speech input unavailable" | Run `sudo apt install alsa-utils` (provides `arecord`). `--check` shows which recorder was found. |
| "Transcription failed: The speech model ... is unavailable" | Set `JARVIS_STT_MODEL=whisper-large-v3`. `--check` warns when the configured speech model is not offered to your account. |
| "Transcription failed: Groq is rate-limiting requests" / "...speech quota ... is used up" | Speech models have their own free-tier limits, counted in audio seconds. Wait, switch `JARVIS_STT_MODEL` to another Whisper model, or type instead. See https://console.groq.com/settings/limits. |
| Hands-free does not wake up | Speak closer to the microphone and say "Hey JARVIS" clearly ("Hello Jarvis" scored only 0.52 in testing). Lower `JARVIS_WAKE_THRESHOLD` to `0.5`. Check the input level and device in Settings > Sound > Input; `wpctl status` shows the default source. |
| Hands-free wakes up by itself | Raise `JARVIS_WAKE_THRESHOLD`, for example to `0.7` or `0.8`. |
| JARVIS interrupts itself while speaking | Raise `JARVIS_WAKE_SPEAKING_THRESHOLD` (up to `0.99`), turn the speaker volume down, or use headphones. |
| Hands-free cuts me off mid-request | Raise `JARVIS_WAKE_END_SILENCE`, for example to `1.5` or `2.0`. The pause is measured from the last sound the microphone heard after the wake word. |
| "Hands-free unavailable: The wake-word engine is not installed" | Run `.venv/bin/pip install --no-deps openwakeword==0.6.0` and then `.venv/bin/pip install -r requirements-wake.txt`. The pip warning about `tflite-runtime` is expected. `--check` shows whether the engine imports. |
| "Could not download the wake-word model: ..." | The first start downloads about 7 MB from GitHub, so it needs internet access once. Connect and type `/wake on` again, or download in advance with `.venv/bin/python -c "import openwakeword.utils; openwakeword.utils.download_models(model_names=['hey_jarvis'])"`. After that, detection works offline (transcription still needs Groq). |
| "Unknown wake word model ..." / "Wake-word model file ... not found" | Set `JARVIS_WAKE_MODEL` to a built-in name (`hey_jarvis` by default) or to the path of an existing `.onnx` file. |
| "Hands-free unavailable: Hands-free mode streams audio with arecord" | Run `sudo apt install alsa-utils`. `pw-record` alone is not enough for hands-free. |
| "Hands-free unavailable: The microphone could not be opened (...)" | `arecord` started but exited at once, usually no capture device or a busy one. The detail in brackets is arecord's own message. Check Settings > Sound > Input. |
| "Hands-free listening stopped: the microphone stream ended" | `arecord` exited, for example because the microphone was unplugged or is in use by another program. Fix that, then type `/wake on`. |
| High CPU use with hands-free on | About 4% of one CPU core is expected: the model scores every 80 ms frame, even while idle. Type `/wake off` when you do not need it. |

## Known limitations

- **No offline speech recognition.** Every recording is sent to Groq. A local Whisper backend (for example faster-whisper) is a possible future backend; it is not implemented.
- **No acoustic echo cancellation.** The microphone hears JARVIS's own voice, so voice interruption relies on the wake word and the raised speaking threshold. Headphones help.
- **One wake phrase, English only.** Only "Hey JARVIS" is removed from transcripts. The other built-in openWakeWord models (all English) and custom `.onnx` models work for detection, but their wake phrase stays in the transcript.
- **The pre-roll can include JARVIS's own speech** during an interruption. That audio is uploaded to Groq with the request, and if Whisper does not write the wake phrase, it can end up in the transcript.
- **A cancelled tool that was already running may finish**, whether you cancel with Ctrl+C or with the wake word (see the tool-timeout item below).
- **Hands-free typing uses a plain line reader**, so there is no line-editing history.
- **The hands-free listener uses `arecord` only.** `pw-record` is not used for streaming.
- **Recognition quality depends on the laptop microphone** and on room noise. With a real voice, neither push-to-talk transcription nor the wake word has been tested yet (see [Status](#status--verification)).
- **Rate limits are noticeable on the free tier.** Rapid tool-heavy questions can wait 10-15 s each for Groq's per-minute token budget.
- **Search returns snippets only.** JARVIS does not open or read the result pages.
- **Memory of conversations is limited.** Context covers only the current session: the last `JARVIS_HISTORY_TURNS` exchanges, plus one-line summaries of older questions. Tool results are not carried between turns. The saved conversation log is a bounded record and is not fed back to the model.
- **Tool timeouts cannot kill a hung tool.** Tools run in worker threads, and Python cannot stop a thread. A read-only tool that times out is reported as an error. A state-changing tool that times out is reported as `unverified`, because it may still finish. Pressing Ctrl+C has the same effect: the request is abandoned, but a tool already running may complete.
- **App launches cannot be confirmed visually.** JARVIS checks that the process did not exit straight away, but it cannot see whether a window appeared.
- Memory retrieval is keyword-based, not semantic.
- **Tainting is per request.** If a reply repeats text from a web page or file, the next message you send starts untainted. Redaction is pattern-based and cannot catch every secret format, so keep real credentials out of the workspace.

## Roadmap

JARVIS stays a single agent throughout. Later versions add capabilities, not more agents.

| Version | Focus |
| --- | --- |
| V1.1 | Voice input. Done: push-to-talk with Groq Whisper, the "Hey JARVIS" wake word and interrupting JARVIS by voice. Still to do: local (offline) transcription. |
| V1.2 | Developer assistant features |
| V2 | SQLite-backed memory (the `MemoryStore` interface is designed to be swapped) |
| V2.1 | Desktop UI |
| V3 | More advanced agent behaviour, still a single agent |

## Project layout

| Path | Purpose |
| --- | --- |
| `jarvis.py` | The whole application: config, logging, memory, permissions, tools, voice, speech input, hands-free wake word, agent loop, CLI |
| `requirements.txt` | Pinned Python dependencies |
| `requirements-wake.txt` | Optional runtime dependencies for the wake-word engine (install `openwakeword` itself with `--no-deps`, see [Hands-free mode](#hands-free-mode-wake-word)) |
| `.env.example` | Configuration template; copy to `.env` |
| `jarvis_memory.json` | Your memory file (gitignored; recreated if missing) |
| `tests/` | pytest suite |
