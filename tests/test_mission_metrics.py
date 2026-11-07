"""Mission Control metrics sampler and the shared HTTPS probe (no network, no real psutil values)."""

from __future__ import annotations

import socket
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from mission_control import metrics
from mission_control.metrics import MetricsSampler, probe_groq
from mission_control.protocol import validate_event
from tools import linux_system
from tools.linux_system import probe_https


class FakePsutil:
    """Minimal psutil stand-in that records how cpu_percent was called."""

    def __init__(self) -> None:
        self.cpu_calls: list[dict] = []
        self.disk_paths: list[str] = []

    def cpu_percent(self, interval=0.0, percpu=False):
        self.cpu_calls.append({"interval": interval})
        return 12.5

    def virtual_memory(self):
        return types.SimpleNamespace(total=8_000_000_000, available=5_000_000_000, used=2_500_000_000, percent=37.5)

    def disk_usage(self, path):
        self.disk_paths.append(str(path))
        return types.SimpleNamespace(total=500_000_000_000, used=100_000_000_000, free=400_000_000_000, percent=20.0)

    def boot_time(self):
        return 1000.0

    class Process:
        def create_time(self):
            return 2000.0


class MetricsSamplerTests(unittest.TestCase):
    def make(self, fake: FakePsutil | None = None, **kwargs) -> tuple[MetricsSampler, FakePsutil]:
        fake = fake or FakePsutil()
        return MetricsSampler(disk_path=Path("/some/where"), psutil_module=fake, **kwargs), fake

    def test_sample_uses_numbers_from_psutil(self) -> None:
        sampler, fake = self.make()
        with mock.patch.object(metrics.time, "time", return_value=1000.0 + 3600), \
                mock.patch.object(metrics.os, "getloadavg", return_value=(0.5, 1.0, 1.5)):
            payload = sampler.sample()
        self.assertTrue(sampler.available)
        self.assertEqual(payload["cpu_percent"], 12.5)
        self.assertEqual(payload["memory_percent"], 37.5)
        self.assertEqual(payload["memory_total"], 8_000_000_000)
        self.assertEqual(payload["memory_available"], 5_000_000_000)
        self.assertEqual(payload["memory_used"], 2_500_000_000)
        self.assertEqual(payload["disk_percent"], 20.0)
        self.assertEqual(payload["disk_total"], 500_000_000_000)
        self.assertEqual(payload["disk_used"], 100_000_000_000)
        self.assertEqual(payload["uptime_s"], 3600)
        self.assertEqual(payload["process_uptime_s"], 2600)  # now 4600 - process start 2000
        self.assertEqual(payload["load_avg"], [0.5, 1.0, 1.5])
        self.assertIsInstance(payload["os"], str)
        self.assertEqual(fake.disk_paths, ["/some/where"])
        for key in ("memory_total", "memory_available", "memory_used", "disk_total", "disk_used", "uptime_s"):
            self.assertIsInstance(payload[key], int)

    def test_payload_satisfies_protocol_contract(self) -> None:
        sampler, _ = self.make()
        payload = sampler.sample()
        envelope = {"v": 1, "seq": 1, "type": "system.metrics", "timestamp": 1762500000000, **payload}
        self.assertEqual(validate_event(envelope), [])

    def test_cpu_percent_is_non_blocking(self) -> None:
        sampler, fake = self.make()
        sampler.sample()
        sampler.sample()
        self.assertGreaterEqual(len(fake.cpu_calls), 3)  # one priming call in __init__ plus two samples
        self.assertTrue(all(call["interval"] is None for call in fake.cpu_calls))

    def test_sampler_without_psutil_reports_unavailable(self) -> None:
        sampler = MetricsSampler(psutil_module=None)
        self.assertFalse(sampler.available)
        self.assertIsNone(sampler.sample())

    def test_unexpected_psutil_error_returns_none_and_logs_debug(self) -> None:
        sampler, fake = self.make()
        fake.virtual_memory = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs("jarvis.mission_control.metrics", level="DEBUG") as logs:
            self.assertIsNone(sampler.sample())
        self.assertTrue(any("boom" in line for line in logs.output))

    def test_priming_failure_does_not_raise(self) -> None:
        fake = FakePsutil()
        fake.cpu_percent = mock.Mock(side_effect=RuntimeError("no cpu"))
        sampler = MetricsSampler(psutil_module=fake)
        self.assertIsNone(sampler.sample())

    def test_load_average_unavailable_returns_none(self) -> None:
        sampler, _ = self.make()
        with mock.patch.object(metrics.os, "getloadavg", side_effect=OSError), \
                self.assertLogs("jarvis.mission_control.metrics", level="DEBUG"):
            self.assertIsNone(sampler.sample())

    def test_first_sample_from_a_new_thread_primes_then_returns_none(self) -> None:
        sampler, fake = self.make()  # primed on this (test) thread
        results: list = []

        def run() -> None:
            results.extend([sampler.sample(), sampler.sample()])

        worker = threading.Thread(target=run)
        worker.start()
        worker.join()
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], dict)
        self.assertEqual(results[1]["cpu_percent"], 12.5)
        # priming in __init__, the discard read on the worker, then the real read
        self.assertEqual(len(fake.cpu_calls), 3)

    def test_os_name_comes_from_pretty_name_and_is_cached(self) -> None:
        sampler, _ = self.make()
        release = 'NAME="Zorin OS"\nPRETTY_NAME="Zorin OS 18.1"\n'
        with mock.patch.object(Path, "read_text", return_value=release) as read:
            self.assertEqual(sampler.sample()["os"], "Zorin OS 18.1")
            self.assertEqual(sampler.sample()["os"], "Zorin OS 18.1")
        self.assertEqual(read.call_count, 1)

    def test_os_name_falls_back_when_os_release_missing(self) -> None:
        sampler, _ = self.make()
        with mock.patch.object(Path, "read_text", side_effect=OSError):
            self.assertTrue(sampler.sample()["os"])


class ProbeHttpsTests(unittest.TestCase):
    def test_probe_https_ok_and_failure(self) -> None:
        connection = mock.MagicMock()
        with mock.patch.object(socket, "getaddrinfo", return_value=[]), \
                mock.patch.object(socket, "create_connection", return_value=connection) as connect:
            ok = probe_https("example.test")
        connect.assert_called_once_with(("example.test", 443), timeout=4.0)
        self.assertEqual(ok["host"], "example.test")
        self.assertEqual(ok["dns"], "ok")
        self.assertEqual(ok["https_port"], "reachable")
        self.assertIsInstance(ok["latency_ms"], int)
        self.assertGreaterEqual(ok["latency_ms"], 0)

        with mock.patch.object(socket, "getaddrinfo", return_value=[]), \
                mock.patch.object(socket, "create_connection", side_effect=ConnectionRefusedError(111, "Connection refused")):
            refused = probe_https("example.test", port=8443, timeout=1.5)
        self.assertEqual(refused["dns"], "ok")
        self.assertEqual(refused["https_port"], "unreachable (Connection refused)")
        self.assertNotIn("latency_ms", refused)

        with mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")), \
                mock.patch.object(socket, "create_connection") as connect:
            dns_failed = probe_https("nope.test")
        connect.assert_not_called()
        self.assertEqual(dns_failed["dns"], "failed")
        self.assertNotIn("latency_ms", dns_failed)

    def test_check_network_output_unchanged(self) -> None:
        tools = linux_system.SystemTools.__new__(linux_system.SystemTools)

        def getaddrinfo(host, *args, **kwargs):
            if host == "duckduckgo.com":
                raise socket.gaierror(-2, "Name or service not known")
            return []

        with mock.patch.object(socket, "getaddrinfo", side_effect=getaddrinfo), \
                mock.patch.object(socket, "create_connection", return_value=mock.MagicMock()), \
                mock.patch.object(linux_system, "psutil", None):
            result = tools.check_network()
        self.assertEqual(result, {
            "online": True,
            "checks": [
                {"host": "api.groq.com", "dns": "ok", "https_port": "reachable"},
                {"host": "duckduckgo.com", "dns": "failed"},
            ],
        })

    def test_check_network_offline(self) -> None:
        tools = linux_system.SystemTools.__new__(linux_system.SystemTools)
        with mock.patch.object(socket, "getaddrinfo", return_value=[]), \
                mock.patch.object(socket, "create_connection", side_effect=OSError(101, "Network is unreachable")), \
                mock.patch.object(linux_system, "psutil", None):
            result = tools.check_network()
        self.assertFalse(result["online"])
        self.assertEqual(result["checks"][0], {"host": "api.groq.com", "dns": "ok",
                                               "https_port": "unreachable (Network is unreachable)"})


class ProbeGroqTests(unittest.TestCase):
    def test_probe_groq_maps_status(self) -> None:
        up = {"host": "api.groq.com", "dns": "ok", "https_port": "reachable", "latency_ms": 42}
        with mock.patch.object(metrics, "probe_https", return_value=up) as probe:
            result = probe_groq()
        probe.assert_called_once_with("api.groq.com", 443, 3.0)
        self.assertEqual(result["groq"], "up")
        self.assertEqual(result["latency_ms"], 42)
        self.assertEqual(result["source"], "probe")
        self.assertIsInstance(result["checked_at"], int)
        self.assertEqual(validate_event({"v": 1, "seq": 1, "type": "network.status",
                                         "timestamp": 1762500000000, **result}), [])

        for down in ({"host": "api.groq.com", "dns": "failed"},
                     {"host": "api.groq.com", "dns": "ok", "https_port": "unreachable (timed out)"}):
            with mock.patch.object(metrics, "probe_https", return_value=down):
                result = probe_groq(timeout=1.0)
            self.assertEqual(result["groq"], "down")
            self.assertNotIn("latency_ms", result)
            self.assertEqual(result["source"], "probe")

    def test_probe_groq_never_raises(self) -> None:
        with mock.patch.object(metrics, "probe_https", side_effect=RuntimeError("bad")):
            result = probe_groq()
        self.assertEqual(result["groq"], "down")


if __name__ == "__main__":
    unittest.main()
