"""Configuration: environment variables, .env loading and validated settings."""

from __future__ import annotations

import dataclasses
import os
import re
import shlex
import tomllib
from pathlib import Path

from core.audit import log


APP_NAME = "jarvis-mark-50"


VERSION = "1.0.0"


SCRIPT_DIR = Path(__file__).resolve().parent.parent  # the project root, where jarvis.py lives


# llama-3.3-70b-versatile (the original spec default) is no longer offered by Groq; gpt-oss-120b
# was verified live with native tool calling. Override with GROQ_MODEL.
DEFAULT_MODEL = "openai/gpt-oss-120b"


DEFAULT_VOICE = "en-GB-RyanNeural"


DEFAULT_STT_MODEL = "whisper-large-v3-turbo"


DEFAULT_WAKE_MODEL = "hey_jarvis"


PERMISSION_MODES = ("read_only", "standard", "confirm_all")


STT_BACKENDS = ("groq",)


REASONING_EFFORTS = ("", "low", "medium", "high")  # "" = the model's own default


# Executable candidates per application; the first one found on PATH is used.
DEFAULT_APPS: dict[str, list[list[str]]] = {
    "vscode": [["code"], ["codium"]],
    "firefox": [["firefox"]],
    "chrome": [["google-chrome"], ["chromium"], ["chromium-browser"]],
    "files": [["nautilus"], ["nemo"], ["thunar"]],
    "terminal": [["gnome-terminal"], ["kgx"], ["x-terminal-emulator"]],
    "text editor": [["gnome-text-editor"], ["gedit"]],
    "calculator": [["gnome-calculator"]],
    "system monitor": [["gnome-system-monitor"]],
    "settings": [["gnome-control-center"]],
}


APP_ALIASES = {
    "code": "vscode",
    "vs code": "vscode",
    "visual studio code": "vscode",
    "file manager": "files",
    "nautilus": "files",
    "gedit": "text editor",
    "editor": "text editor",
    "google chrome": "chrome",
    "chromium": "chrome",
    "task manager": "system monitor",
}


class ConfigError(Exception):
    """Configuration is missing or invalid; the message is safe to show the user."""


def _xdg_dir(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "").strip()
    return Path(value).expanduser() if value else Path.home() / fallback


def load_env_file(path: Path, env: dict[str, str] | None = None) -> bool:
    """Load KEY=VALUE lines from a .env file without overriding existing variables.

    A minimal parser keeps python-dotenv out of the dependency list.
    """
    env = os.environ if env is None else env
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value.find(value[0], 1) > 0:
            value = value[1:value.find(value[0], 1)]  # quoted: ignore anything after the closing quote
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        if key and key not in env:
            env[key] = value
    return True


_PLACEHOLDER_KEY = re.compile(
    r"^(your|<|\[|x{4,}|changeme|change[-_]me|replace|placeholder|insert|example|dummy|fake|test)",
    re.IGNORECASE,
)


def validate_api_key(key: str | None) -> str:
    """Return a cleaned API key or raise ConfigError explaining what is wrong."""
    key = (key or "").strip()
    if not key:
        raise ConfigError(
            "GROQ_API_KEY is not set. Create a key at https://console.groq.com/keys, then run\n"
            "  export GROQ_API_KEY='gsk_...'\n"
            "or put it in a .env file next to jarvis.py (see .env.example)."
        )
    lowered = key.lower()
    if _PLACEHOLDER_KEY.match(key) or "api_key" in lowered or "your" in lowered or "here" in lowered:
        raise ConfigError("GROQ_API_KEY still contains a placeholder value. Replace it with a real key.")
    if len(key) < 20 or any(ch.isspace() for ch in key):
        raise ConfigError("GROQ_API_KEY does not look like a valid key (too short or contains spaces).")
    if not key.startswith("gsk_"):
        log.warning("GROQ_API_KEY does not start with 'gsk_'; Groq may reject it.")
    return key


def _env_int(env: dict[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}.")
    return value


def _env_float(env: dict[str, str], name: str, default: float, minimum: float, maximum: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}.")
    return value


def _env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}.")


def parse_app_overrides(raw: str) -> dict[str, list[list[str]]]:
    """Parse JARVIS_APPS, e.g. "obsidian=obsidian; music=flatpak run com.spotify.Client"."""
    apps: dict[str, list[list[str]]] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ConfigError(f"JARVIS_APPS entry {entry!r} must look like name=command.")
        name, command = entry.split("=", 1)
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ConfigError(f"JARVIS_APPS entry {entry!r} is not a valid command: {exc}") from None
        if not name.strip() or not argv:
            raise ConfigError(f"JARVIS_APPS entry {entry!r} must look like name=command.")
        apps[name.strip().lower()] = [argv]
    return apps


DEFAULT_PERMISSIONS_FILE = SCRIPT_DIR / "config" / "permissions.toml"
COMMAND_KINDS = ("tests", "lint")
_COMMAND_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")


@dataclasses.dataclass(frozen=True)
class ProjectCommand:
    """A command JARVIS may run inside a workspace project. Defined by the user, never by the model."""
    name: str
    argv: tuple[str, ...]
    kind: str = "tests"
    detect: tuple[str, ...] = ()
    npm_script: str | None = None
    timeout: int = 600
    confirm: bool = True
    description: str = ""


@dataclasses.dataclass(frozen=True)
class ProjectRule:
    """Per-project choices from [projects.<folder>]: which command to use, and whether to ask first."""
    tests: str | None = None
    lint: str | None = None
    confirm: bool | None = None


@dataclasses.dataclass
class Permissions:
    applications: dict[str, list[list[str]]]
    commands: dict[str, ProjectCommand]
    projects: dict[str, ProjectRule]


def load_permissions(path: Path) -> Permissions:
    """Read the permissions file. A missing file means no extra apps and no project commands."""
    path = Path(path)
    if not path.exists():
        return Permissions({}, {}, {})
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path} could not be read as TOML: {exc}") from None

    def fail(message: str):
        raise ConfigError(f"{path}: {message}")

    def string_list(value, where: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            fail(f"{where} must be a list of non-empty strings.")
        return tuple(value)

    unknown = set(data) - {"applications", "commands", "projects"}
    if unknown:
        fail(f"unknown section(s): {', '.join(sorted(unknown))}.")
    for section in ("applications", "commands", "projects"):
        if not isinstance(data.get(section, {}), dict):
            fail(f"[{section}] must be a table, e.g. [{section}] on its own line followed by entries.")

    applications: dict[str, list[list[str]]] = {}
    for name, command in (data.get("applications") or {}).items():
        if not isinstance(command, str) or not command.strip():
            fail(f"applications.{name} must be a command string.")
        try:
            applications[name.strip().lower()] = [shlex.split(command)]
        except ValueError as exc:
            fail(f"applications.{name} is not a valid command: {exc}.")

    commands: dict[str, ProjectCommand] = {}
    allowed_keys = {"argv", "kind", "detect", "npm_script", "timeout", "confirm", "description"}
    for name, spec in (data.get("commands") or {}).items():
        where = f"commands.{name}"
        if not _COMMAND_NAME.fullmatch(name):
            fail(f"{where}: names use lowercase letters, digits, '-' and '_'.")
        if not isinstance(spec, dict):
            fail(f"{where} must be a table.")
        if set(spec) - allowed_keys:
            fail(f"{where}: unknown key(s) {', '.join(sorted(set(spec) - allowed_keys))}.")
        argv = string_list(spec.get("argv"), f"{where}.argv")
        if not argv:
            fail(f"{where}.argv must not be empty.")
        kind = spec.get("kind", "tests")
        if kind not in COMMAND_KINDS:
            fail(f"{where}.kind must be one of {', '.join(COMMAND_KINDS)}.")
        timeout = spec.get("timeout", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 5 <= timeout <= 3600:
            fail(f"{where}.timeout must be a whole number of seconds between 5 and 3600.")
        confirm = spec.get("confirm", True)
        if not isinstance(confirm, bool):
            fail(f"{where}.confirm must be true or false.")
        npm_script = spec.get("npm_script")
        if npm_script is not None and (not isinstance(npm_script, str) or not npm_script.strip()):
            fail(f"{where}.npm_script must be a script name.")
        commands[name] = ProjectCommand(
            name=name, argv=argv, kind=kind, detect=string_list(spec.get("detect", []), f"{where}.detect"),
            npm_script=npm_script, timeout=timeout, confirm=confirm, description=str(spec.get("description", "")),
        )

    projects: dict[str, ProjectRule] = {}
    for name, spec in (data.get("projects") or {}).items():
        where = f"projects.{name}"
        if not isinstance(spec, dict) or set(spec) - {"tests", "lint", "confirm"}:
            fail(f"{where} may only set tests, lint and confirm.")
        for kind in COMMAND_KINDS:
            chosen = spec.get(kind)
            if chosen is not None and (chosen not in commands or commands[chosen].kind != kind):
                fail(f"{where}.{kind} must name a [commands] entry of kind {kind!r}.")
        if "confirm" in spec and not isinstance(spec["confirm"], bool):
            fail(f"{where}.confirm must be true or false.")
        projects[name] = ProjectRule(spec.get("tests"), spec.get("lint"), spec.get("confirm"))
    return Permissions(applications, commands, projects)


@dataclasses.dataclass
class Config:
    api_key: str = dataclasses.field(repr=False)
    model: str = DEFAULT_MODEL
    reasoning_effort: str = ""
    voice: str = DEFAULT_VOICE
    tts_enabled: bool = True
    memory_path: Path = SCRIPT_DIR / "jarvis_memory.json"
    workspace: Path = Path.home() / "Projects"
    notes_dir: Path = Path.home() / "Documents" / "JARVIS Notes"
    log_dir: Path = _xdg_dir("XDG_DATA_HOME", ".local/share") / APP_NAME / "logs"
    max_tool_rounds: int = 8
    search_limit: int = 5
    log_level: str = "INFO"
    history_turns: int = 10
    permission_mode: str = "standard"
    editor: str = "code"
    save_conversations: bool = True
    stt_enabled: bool = False
    stt_backend: str = "groq"
    stt_model: str = DEFAULT_STT_MODEL
    stt_language: str = "en"
    stt_max_seconds: int = 60
    wake_enabled: bool = False
    wake_model: str = DEFAULT_WAKE_MODEL
    wake_threshold: float = 0.6
    wake_speaking_threshold: float = 0.75
    wake_end_silence: float = 1.0
    wake_chime: bool = True
    apps: dict[str, list[list[str]]] = dataclasses.field(default_factory=lambda: dict(DEFAULT_APPS))
    permissions_file: Path = DEFAULT_PERMISSIONS_FILE
    commands: dict[str, ProjectCommand] = dataclasses.field(default_factory=dict)
    projects: dict[str, ProjectRule] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, require_api_key: bool = True) -> "Config":
        env = dict(os.environ if env is None else env)
        if require_api_key:
            api_key = validate_api_key(env.get("GROQ_API_KEY"))
        else:
            api_key = (env.get("GROQ_API_KEY") or "").strip()

        log_level = (env.get("JARVIS_LOG_LEVEL") or "INFO").strip().upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"JARVIS_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL, got {log_level!r}.")
        mode = (env.get("JARVIS_PERMISSION_MODE") or "standard").strip().lower()
        if mode not in PERMISSION_MODES:
            raise ConfigError(f"JARVIS_PERMISSION_MODE must be one of {', '.join(PERMISSION_MODES)}.")
        reasoning_effort = (env.get("JARVIS_REASONING_EFFORT") or "").strip().lower()
        if reasoning_effort not in REASONING_EFFORTS:
            raise ConfigError("JARVIS_REASONING_EFFORT must be low, medium or high (or empty for the model default).")
        stt_backend = (env.get("JARVIS_STT_BACKEND") or "groq").strip().lower()
        if stt_backend not in STT_BACKENDS:
            raise ConfigError(f"JARVIS_STT_BACKEND must be one of {', '.join(STT_BACKENDS)}.")
        stt_language = (env.get("JARVIS_STT_LANGUAGE") if "JARVIS_STT_LANGUAGE" in env else "en").strip().lower()
        if stt_language and not re.fullmatch(r"[a-z]{2,3}", stt_language):
            raise ConfigError("JARVIS_STT_LANGUAGE must be a language code such as en or tl, or empty to auto-detect.")

        def path_setting(name: str, default: Path) -> Path:
            raw = (env.get(name) or "").strip()
            return Path(raw).expanduser() if raw else default

        permissions_file = path_setting("JARVIS_PERMISSIONS_FILE", DEFAULT_PERMISSIONS_FILE)
        permissions = load_permissions(permissions_file)
        apps = dict(DEFAULT_APPS)
        apps.update(permissions.applications)  # the permissions file, then JARVIS_APPS, override built-ins
        apps.update(parse_app_overrides(env.get("JARVIS_APPS", "")))
        data_home = Path(env["XDG_DATA_HOME"]).expanduser() if env.get("XDG_DATA_HOME") else Path.home() / ".local/share"

        return cls(
            api_key=api_key,
            model=(env.get("GROQ_MODEL") or DEFAULT_MODEL).strip(),
            reasoning_effort=reasoning_effort,
            voice=(env.get("JARVIS_VOICE") or DEFAULT_VOICE).strip(),
            tts_enabled=_env_bool(env, "JARVIS_TTS_ENABLED", True),
            memory_path=path_setting("JARVIS_MEMORY_PATH", SCRIPT_DIR / "jarvis_memory.json"),
            workspace=path_setting("JARVIS_WORKSPACE", Path.home() / "Projects"),
            notes_dir=path_setting("JARVIS_NOTES_DIR", Path.home() / "Documents" / "JARVIS Notes"),
            log_dir=path_setting("JARVIS_LOG_DIR", data_home / APP_NAME / "logs"),
            max_tool_rounds=_env_int(env, "JARVIS_MAX_TOOL_ROUNDS", 8, 1, 25),
            search_limit=_env_int(env, "JARVIS_SEARCH_LIMIT", 5, 1, 10),
            log_level=log_level,
            history_turns=_env_int(env, "JARVIS_HISTORY_TURNS", 10, 0, 50),
            permission_mode=mode,
            editor=(env.get("JARVIS_EDITOR") or "code").strip(),
            save_conversations=_env_bool(env, "JARVIS_SAVE_CONVERSATIONS", True),
            stt_enabled=_env_bool(env, "JARVIS_STT_ENABLED", False),
            stt_backend=stt_backend,
            stt_model=(env.get("JARVIS_STT_MODEL") or DEFAULT_STT_MODEL).strip(),
            stt_language=stt_language,
            stt_max_seconds=_env_int(env, "JARVIS_STT_MAX_SECONDS", 60, 5, 300),
            wake_enabled=_env_bool(env, "JARVIS_WAKE_ENABLED", False),
            wake_model=(env.get("JARVIS_WAKE_MODEL") or DEFAULT_WAKE_MODEL).strip(),
            wake_threshold=_env_float(env, "JARVIS_WAKE_THRESHOLD", 0.6, 0.1, 0.99),
            wake_speaking_threshold=_env_float(env, "JARVIS_WAKE_SPEAKING_THRESHOLD", 0.75, 0.1, 0.99),
            wake_end_silence=_env_float(env, "JARVIS_WAKE_END_SILENCE", 1.0, 0.4, 3.0),
            wake_chime=_env_bool(env, "JARVIS_WAKE_CHIME", True),
            apps=apps,
            permissions_file=permissions_file,
            commands=permissions.commands,
            projects=permissions.projects,
        )
