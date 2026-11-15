"""Host metrics for the SYSTEM node and the Groq reachability probe for the NETWORK node.

The server calls `MetricsSampler.sample()` every 2 s, and only while a browser is connected, on its event
loop. So `sample()` must neither block nor raise: CPU use is read with `interval=None` (the percentage since
the previous call, primed at construction) and any unexpected psutil failure becomes `None`, which
the server shows as an "unavailable" SYSTEM node rather than as zeros.

psutil keeps its non-blocking CPU baseline per thread, so a read from a thread other than the one that
primed it returns a fake 0.0. The sampler therefore remembers which thread primed it; the first `sample()`
from a different thread primes again and returns None (no number is better than a made-up one)."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from tools.linux_system import probe_https

try:
    import psutil
except ImportError:  # psutil is optional; the SYSTEM node then reports "unavailable".
    psutil = None


log = logging.getLogger("jarvis.mission_control.metrics")

GROQ_HOST = "api.groq.com"
_UNSET: Any = object()


def _read_os_name() -> str:
    """The freedesktop PRETTY_NAME (e.g. "Zorin OS 18.1"), or a generic "Linux"."""
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                name = line.split("=", 1)[1].strip().strip('"').strip("'")
                if name:
                    return name
    except (OSError, UnicodeDecodeError):
        pass
    return "Linux"


class MetricsSampler:
    """Reads CPU, memory, disk, uptime and load; `psutil_module` is injectable so tests use fake numbers."""

    def __init__(self, disk_path: Path | None = None, psutil_module: Any = _UNSET) -> None:
        self._psutil = psutil if psutil_module is _UNSET else psutil_module
        self._disk_path = Path.home() if disk_path is None else disk_path
        self._os_name: str | None = None
        self._primed_thread: int | None = None
        self.available = self._psutil is not None
        if self.available:
            self._prime_cpu()

    def _prime_cpu(self) -> None:
        """Discard the first (always 0.0) non-blocking CPU read, on the calling thread."""
        try:
            self._psutil.cpu_percent(interval=None)
            self._primed_thread = threading.get_ident()
        except Exception as exc:  # noqa: BLE001 - priming must never stop JARVIS from starting
            log.debug("cpu_percent priming failed: %s", exc)

    def sample(self) -> dict[str, Any] | None:
        """A `system.metrics` payload, or None when psutil is missing or fails."""
        if self._psutil is None:
            return None
        if self._primed_thread != threading.get_ident():
            self._prime_cpu()  # baseline is per thread; the next sample from this thread is real
            return None
        try:
            cpu = float(self._psutil.cpu_percent(interval=None))
            memory = self._psutil.virtual_memory()
            disk = self._psutil.disk_usage(str(self._disk_path))
            now = time.time()
            return {
                "cpu_percent": cpu,
                "memory_percent": float(memory.percent),
                "memory_total": int(memory.total),
                "memory_available": int(memory.available),
                "memory_used": int(memory.used),
                "disk_percent": float(disk.percent),
                "disk_total": int(disk.total),
                "disk_used": int(disk.used),
                "uptime_s": max(0, int(now - self._psutil.boot_time())),
                "process_uptime_s": max(0, int(now - self._psutil.Process().create_time())),
                "load_avg": self._load_avg(),
                "os": self._os(),
            }
        except Exception as exc:  # noqa: BLE001 - a metrics hiccup must not take the server loop down
            log.debug("metrics sample failed: %s", exc)
            return None

    @staticmethod
    def _load_avg() -> list[float]:
        # An OSError here propagates to sample(), which reports "no metrics" instead of fake zeros.
        return [round(v, 2) for v in os.getloadavg()]

    def _os(self) -> str:
        if self._os_name is None:
            self._os_name = _read_os_name()
        return self._os_name


def probe_groq(timeout: float = 3.0) -> dict[str, Any]:
    """A `network.status` payload from a TCP probe of api.groq.com:443 (no request, so no key is involved).

    BLOCKS for up to `timeout` seconds (DNS resolution has no timeout of its own and can take longer), so
    async callers must run it in an executor, never directly on the event loop."""
    try:
        result = probe_https(GROQ_HOST, 443, timeout)
    except Exception as exc:  # noqa: BLE001 - probe_https handles OSError; anything else still means "down"
        log.debug("groq probe failed: %s", exc)
        result = {}
    up = result.get("https_port") == "reachable"
    payload: dict[str, Any] = {"groq": "up" if up else "down",
                               "checked_at": int(time.time() * 1000), "source": "probe"}
    if up and "latency_ms" in result:
        payload["latency_ms"] = result["latency_ms"]
    return payload
