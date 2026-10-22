"""The Groq provider: client setup, model checks, chat calls and error translation."""

from __future__ import annotations

import time
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING

from config.settings import ConfigError
from core.audit import log, redact

if TYPE_CHECKING:
    from config.settings import Config


try:  # The Groq SDK is required for chatting, but not for --help or the tests.
    import groq
except ImportError:  # pragma: no cover - exercised only when the dependency is missing
    groq = None


class JarvisAPIError(Exception):
    """A Groq API failure translated into a user-facing message."""

    def __init__(self, message: str, kind: str = "unknown", retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.retry_after = retry_after


def _error_details(exc: Exception) -> tuple[str, str]:
    """Extract (code, message) from a Groq SDK error body."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error", body)
        if isinstance(inner, dict):
            return str(inner.get("code") or inner.get("type") or ""), str(inner.get("message") or "")
    return "", str(exc)


def classify_api_error(exc: Exception, model: str = "") -> JarvisAPIError:
    if isinstance(exc, JarvisAPIError):
        return exc
    code, message = _error_details(exc)
    lowered = f"{code} {message}".lower()
    status = getattr(exc, "status_code", None)
    if groq is not None:
        if isinstance(exc, groq.AuthenticationError):
            return JarvisAPIError("Groq rejected the API key (401). Check GROQ_API_KEY.", "auth")
        if isinstance(exc, groq.PermissionDeniedError):
            return JarvisAPIError("Groq denied access (403). This key may not have access to the model, "
                                  "or the account is restricted.", "permission")
        if isinstance(exc, groq.NotFoundError):
            return JarvisAPIError(f"The model {model!r} was not found. Set GROQ_MODEL to an available model.", "model")
        if isinstance(exc, groq.RateLimitError):
            retry_after = None
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    retry_after = float(response.headers.get("retry-after"))
                except (TypeError, ValueError):
                    pass
            if any(word in lowered for word in ("per day", "tokens per day", "requests per day", "tpd", "rpd", "quota")):
                return JarvisAPIError("The Groq usage quota for this model is exhausted for now. Try again later, "
                                      "or set GROQ_MODEL to a different model.", "quota")
            return JarvisAPIError("Groq is rate-limiting requests; please wait a moment.", "rate_limit", True, retry_after)
        if isinstance(exc, groq.BadRequestError):
            if "tool_use_failed" in lowered:
                return JarvisAPIError("The model produced a malformed tool call.", "tool_use_failed", True)
            if "decommission" in lowered or "model_not_found" in lowered or "does not exist" in lowered:
                return JarvisAPIError(f"The model {model!r} is unavailable or retired. Set GROQ_MODEL to a current model.", "model")
            if "context" in lowered and "length" in lowered:
                return JarvisAPIError("The conversation is too long for the model. Type /clear to start afresh.", "context")
            return JarvisAPIError(f"Groq rejected the request: {redact(message)[:200]}", "bad_request")
        if isinstance(exc, groq.APITimeoutError):
            return JarvisAPIError("The request to Groq timed out.", "timeout", True)
        if isinstance(exc, groq.APIConnectionError):
            return JarvisAPIError("Cannot reach Groq. Check the internet connection.", "network", True)
        if isinstance(exc, groq.APIStatusError) and ((status or 0) >= 500 or status == 498):
            return JarvisAPIError("Groq is having trouble right now (server error).", "server", True)
    return JarvisAPIError(f"Unexpected error talking to Groq: {type(exc).__name__}: {redact(exc)[:200]}", "unknown")


def initialize_client(config: Config):
    """Create the Groq client. SDK retries are off; call_model applies its own bounded policy."""
    if groq is None:
        raise ConfigError("The Groq SDK is not installed. Run: pip install -r requirements.txt")
    return groq.Groq(api_key=config.api_key, timeout=45.0, max_retries=0)


def list_model_ids(client, model: str = "") -> list[str]:
    try:
        return sorted(m.id for m in client.models.list().data)
    except Exception as exc:  # noqa: BLE001
        raise classify_api_error(exc, model) from exc


def verify_model(client, model: str) -> None:
    """Confirm the key works and the model is offered to this account."""
    available = list_model_ids(client, model)
    if model not in available:
        non_chat = ("guard", "whisper", "orpheus", "tts", "safeguard")
        chat_models = [m for m in available if not any(word in m for word in non_chat)]
        suggestions = ", ".join(chat_models)[:300]
        raise JarvisAPIError(f"The model {model!r} isn't available to this account. "
                             f"Set GROQ_MODEL to one of: {suggestions or ', '.join(available[:8])}", "model")


_tool_validation_flag_supported = True


def _flag_unsupported() -> None:
    global _tool_validation_flag_supported
    _tool_validation_flag_supported = False


# Free-tier per-minute limits can ask for waits of a minute or so in the middle of a multi-step task.
MAX_RETRY_WAIT = 60


def call_model(client, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
               tool_choice: str = "auto", max_retries: int = 3, sleep: Callable[[float], None] = time.sleep,
               on_retry: Callable[[str, float], None] | None = None, reasoning_effort: str = ""):
    """One chat completion with bounded, backed-off retries for transient failures only."""
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.4, "max_completion_tokens": 1024}
    if reasoning_effort:  # only reasoning models (e.g. gpt-oss) accept this
        kwargs["reasoning_effort"] = reasoning_effort
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
        if _tool_validation_flag_supported:
            # JARVIS validates every tool call itself and returns precise errors the model can
            # correct, instead of Groq rejecting the whole request over one malformed call.
            kwargs["disable_tool_validation"] = True
    attempt = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            error = classify_api_error(exc, model)
            if error.kind == "bad_request" and "disable_tool_validation" in str(error) \
                    and kwargs.pop("disable_tool_validation", None):
                _flag_unsupported()  # this model or account does not take the option: carry on without it
                continue
            if not error.retryable or attempt >= max_retries:
                raise error from exc
            if error.kind == "tool_use_failed":
                if attempt >= 1:  # one retry at a lower temperature is enough
                    raise error from exc
                kwargs["temperature"] = 0.1
                delay = 0.0
            else:
                delay = error.retry_after if error.retry_after is not None else min(2 ** attempt, 8)
                if delay > MAX_RETRY_WAIT:
                    raise error from exc
            log.info("Retrying Groq call after %s (attempt %d)", error.kind, attempt + 1)
            if on_retry and delay:
                on_retry(str(error), delay)
            attempt += 1
            if delay:
                sleep(delay)
