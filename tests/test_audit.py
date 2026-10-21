"""Secret redaction and the audit trail."""

from __future__ import annotations

import json
import unittest

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    FakeBrowser,
    Recorder,
    TempDirTestCase,
)
import jarvis  # noqa: E402


class RedactionTests(unittest.TestCase):
    def test_redact_removes_secrets(self):
        key = "gsk_" + "Z9" * 20
        text = (f"key={key} header Authorization: Bearer abcdefghijklmnop0123456789 "
                f"and password: hunter2 also pwd=letmein")
        out = jarvis.redact(text)
        self.assertNotIn(key, out)
        self.assertNotIn("abcdefghijklmnop0123456789", out)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("letmein", out)
        self.assertIn("[REDACTED]", out)
        self.assertIn("password: [REDACTED]", out)

    def test_redact_leaves_normal_text(self):
        self.assertEqual(jarvis.redact("The weather is nice today."), "The weather is nice today.")


class AuditLogTests(TempDirTestCase):
    def tearDown(self):
        for logger in (jarvis.log, jarvis.audit_log):
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
        super().tearDown()

    def test_audit_line_has_tool_and_status_but_not_key(self):
        self.assertEqual(jarvis.setup_logging(self.config), self.log_dir)
        memory = self.make_memory()
        registry = jarvis.build_registry(jarvis.JarvisTools(self.config, memory, browser_open=FakeBrowser()))
        policy = jarvis.PermissionPolicy("standard", confirm=Recorder(True))
        secret = "gsk_" + "Q7" * 20
        result = jarvis.execute_tool(registry, policy, "read_workspace_file", json.dumps({"path": f"{secret}.txt"}))
        self.assertEqual(result["status"], jarvis.STATUS_ERROR)
        self.assertNotIn(secret, json.dumps(result))
        for handler in jarvis.audit_log.handlers + jarvis.log.handlers:
            handler.flush()
        audit_text = (self.log_dir / "audit.log").read_text(encoding="utf-8")
        lines = [json.loads(line) for line in audit_text.splitlines() if line.strip()]
        entry = [e for e in lines if e.get("event") == "tool_call"][-1]
        self.assertEqual(entry["tool"], "read_workspace_file")
        self.assertEqual(entry["status"], "error")
        self.assertNotIn(secret, audit_text)
        self.assertNotIn(secret, (self.log_dir / "jarvis.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
