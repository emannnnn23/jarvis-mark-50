"""Linux system tools: time, status, processes, network and approved applications."""

from __future__ import annotations

import datetime as dt
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from config.settings import APP_ALIASES
from tools.filesystem import _human_bytes
from tools.registry import ToolError


try:
    import psutil
except ImportError:  # psutil is optional; /proc and standard utilities are used instead.
    psutil = None


def run_command(argv: list[str], timeout: float = 10, cwd: Path | None = None,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a fixed argument list (never a shell string). Raises ToolError on failure to run."""
    if not argv or not all(isinstance(part, str) for part in argv):
        raise ToolError("Internal error: command must be a non-empty list of strings.")
    executable = shutil.which(argv[0])
    if executable is None:
        raise ToolError(f"The utility '{argv[0]}' is not installed or not on PATH.")
    try:
        return subprocess.run(
            [executable, *argv[1:]], capture_output=True, text=True, timeout=timeout,
            cwd=cwd, env=env, shell=False, check=False, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"'{argv[0]}' timed out after {timeout:g} seconds.") from None
    except OSError as exc:
        raise ToolError(f"Could not run '{argv[0]}': {exc.strerror or exc}") from None


def launch_detached(argv: list[str], cwd: Path | None = None, settle_seconds: float = 0.5) -> dict[str, Any]:
    """Start a GUI application in its own session and check it did not fail immediately."""
    executable = shutil.which(argv[0])
    if executable is None:
        raise ToolError(f"'{argv[0]}' is not installed or not on PATH.")
    try:
        process = subprocess.Popen(
            [executable, *argv[1:]], cwd=cwd, shell=False, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ToolError(f"Could not start '{argv[0]}': {exc.strerror or exc}") from None
    time.sleep(settle_seconds)
    code = process.poll()
    if code not in (None, 0):
        raise ToolError(f"'{argv[0]}' exited immediately with code {code}.")
    return {"pid": process.pid, "still_running": code is None}


class SystemTools:
    """System and application tool handlers. Mixed into JarvisTools, which provides self.config and self.memory."""

    def get_time(self) -> dict[str, Any]:
        now = dt.datetime.now().astimezone()
        result = {
            "iso8601": now.isoformat(timespec="seconds"),
            "weekday": now.strftime("%A"),
            "human": now.strftime("%A %d %B %Y, %H:%M"),
            "utc_offset": now.strftime("%z") or None,
            "timezone_abbreviation": now.tzname(),
        }
        zone = None
        try:
            target = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in target:
                zone = target.split("zoneinfo/", 1)[1]
        except OSError:
            pass
        zone = zone or os.environ.get("TZ") or None
        result["timezone"] = zone
        if now.utcoffset() is None:
            result["limitation"] = "The system timezone is not configured; this time may not be local time."
        return result

    # -- system --------------------------------------------------------------
    def system_status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "hostname": platform.node(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
        }
        try:
            info["os"] = platform.freedesktop_os_release().get("PRETTY_NAME")
        except OSError:
            info["os"] = platform.platform()
        try:
            seconds = float(Path("/proc/uptime").read_text().split()[0])
            info["uptime"] = str(dt.timedelta(seconds=int(seconds)))
        except (OSError, ValueError, IndexError):
            pass

        cores = os.cpu_count() or 1
        cpu: dict[str, Any] = {"cores": cores}
        try:
            load1, load5, load15 = os.getloadavg()
            cpu["load_average"] = [round(load1, 2), round(load5, 2), round(load15, 2)]
        except OSError:
            pass
        if psutil is not None:
            cpu["usage_percent"] = psutil.cpu_percent(interval=0.5)
        info["cpu"] = cpu

        memory: dict[str, Any] = {}
        if psutil is not None:
            vm = psutil.virtual_memory()
            memory = {"total": vm.total, "available": vm.available, "used_percent": vm.percent}
        else:
            try:
                values = {}
                for line in Path("/proc/meminfo").read_text().splitlines():
                    key, value = line.split(":", 1)
                    values[key] = int(value.split()[0]) * 1024
                total, available = values["MemTotal"], values["MemAvailable"]
                memory = {"total": total, "available": available,
                          "used_percent": round(100 * (total - available) / total, 1)}
            except (OSError, KeyError, ValueError, ZeroDivisionError):
                memory = {"error": "memory information unavailable"}
        info["memory"] = {k: _human_bytes(v) if k in ("total", "available") else v for k, v in memory.items()}

        disks = []
        seen_devices = set()
        for mount in ("/", str(Path.home())):
            try:
                device = os.stat(mount).st_dev
                if device in seen_devices:
                    continue
                seen_devices.add(device)
                usage = shutil.disk_usage(mount)
                disks.append({"mount": mount, "total": _human_bytes(usage.total), "free": _human_bytes(usage.free),
                              "used_percent": round(100 * usage.used / usage.total, 1)})
            except OSError:
                continue
        info["disks"] = disks
        return info

    def list_processes(self, sort_by: str = "cpu", limit: int = 10) -> dict[str, Any]:
        limit = max(1, min(limit, 25))
        rows: list[dict[str, Any]] = []
        if psutil is not None:
            processes = list(psutil.process_iter(["pid", "name", "username", "memory_percent"]))
            for process in processes:
                try:
                    process.cpu_percent(None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            time.sleep(0.5)
            for process in processes:
                try:
                    rows.append({
                        "pid": process.info["pid"], "name": process.info["name"],
                        "user": process.info["username"],
                        "cpu_percent": round(process.cpu_percent(None), 1),
                        "memory_percent": round(process.info["memory_percent"] or 0.0, 1),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        else:
            completed = run_command(["ps", "-eo", "pid=,user=,%cpu=,%mem=,comm="], timeout=10)
            if completed.returncode != 0:
                raise ToolError(f"ps failed: {completed.stderr.strip()[:200]}")
            for line in completed.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) == 5:
                    rows.append({"pid": int(parts[0]), "user": parts[1], "cpu_percent": float(parts[2]),
                                 "memory_percent": float(parts[3]), "name": parts[4]})
        key = "cpu_percent" if sort_by == "cpu" else "memory_percent"
        rows.sort(key=lambda row: row[key], reverse=True)
        return {"sorted_by": sort_by, "processes": rows[:limit], "total_processes": len(rows)}

    def check_network(self) -> dict[str, Any]:
        checks = []
        for host in ("api.groq.com", "duckduckgo.com"):
            check = {"host": host}
            try:
                socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
                check["dns"] = "ok"
                with socket.create_connection((host, 443), timeout=4):
                    check["https_port"] = "reachable"
            except socket.gaierror:
                check["dns"] = "failed"
            except OSError as exc:
                check["https_port"] = f"unreachable ({exc.strerror or exc})"
            checks.append(check)
        online = any(c.get("https_port") == "reachable" for c in checks)
        result: dict[str, Any] = {"online": online, "checks": checks}
        if psutil is not None:
            result["interfaces_up"] = sorted(n for n, s in psutil.net_if_stats().items() if s.isup and n != "lo")
        return result

    # -- applications ---------------------------------------------------------
    def _resolve_app(self, name: str) -> tuple[str, list[list[str]]]:
        key = name.strip().lower()
        key = APP_ALIASES.get(key, key)
        if key not in self.config.apps:
            raise ToolError(f"'{name}' is not an approved application. Approved: {', '.join(sorted(self.config.apps))}.")
        return key, self.config.apps[key]

    def launch_app(self, name: str) -> dict[str, Any]:
        key, candidates = self._resolve_app(name)
        for argv in candidates:
            if shutil.which(argv[0]):
                outcome = launch_detached(argv)
                return {"app": key, "command": argv, **outcome,
                        "note": "Launch requested. JARVIS cannot see whether the window appeared."}
        tried = ", ".join(argv[0] for argv in candidates)
        raise ToolError(f"{key} is not installed (looked for: {tried}).")

    def _describe_launch(self, args: dict[str, Any]) -> tuple[str, list[str]]:
        try:
            key, candidates = self._resolve_app(args.get("name", ""))
            return f"Launch {key} ({' '.join(candidates[0])})", []
        except ToolError:
            return f"Launch {args.get('name')!r}", []
