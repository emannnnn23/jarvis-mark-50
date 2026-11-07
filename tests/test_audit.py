"""Secret redaction and the audit trail."""

from __future__ import annotations

import json
import random
import re
import time
import unittest

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    FakeBrowser,
    Recorder,
    TempDirTestCase,
)
import jarvis  # noqa: E402
import core.audit as audit_module  # noqa: E402
from core.audit import reveal_invisible  # noqa: E402


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


# The regex redact() used before it was rewritten to run in linear time, kept verbatim so the
# rewrite can be checked against it.
_OLD_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_ -]?key|secret|token|password|passwd|pwd|passphrase|access[_-]?key|private[_-]?key|pin(?: code| number)?)"
    r"(?:[_-][A-Za-z0-9]+)*)"
    r"([\"']?\s*[:=]\s*|\s+is\s+)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)


def _old_redact(text: str) -> str:
    text = str(text)
    for pattern in audit_module._SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return _OLD_SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def _old_contains_secret(text: str) -> bool:
    if any(p.search(text) for p in audit_module._SECRET_PATTERNS) or _OLD_SECRET_ASSIGNMENT.search(text):
        return True
    return bool(audit_module._CARD_NUMBER.search(text) or audit_module._HIGH_ENTROPY_TOKEN.search(text))


_ASSIGNMENT_CORPUS = [
    "DB_PASSWORD=x", '"api_key": "abc"', "AWS_SECRET_ACCESS_KEY: x", "my password is hunter2",
    "pin code is 1234", "pin number: 5678", "api key=xyz", "api-key=1", "private_key='k'",
    "x-auth-token: t", "GITHUB_TOKEN=ghp_" + "a" * 24,
    "password policy", "tokenizer.py", "secretary=Ann", "spinning=3", "the pin is in",
    "https://example.com/cb?token=abc&x=1&y=2", "Password: Hunter2", "PASSWORD=ABC", "ApiKey=Zed",
    'token="two words here" next', "token='it''s' after", "secret=a,b", "secret=a;b", "{secret=a}",
    "{'password': 'p1', 'user': 'u'}", "xpassword=1", "x_password=1", "x-password=1", "1password=2",
    "my_api_key = abc", "api_key_id: 5", "token_name=foo-bar", "pwd = ", "pwd=,", "pwd= ,x",
    "pin    is   9", "pin code    is 1", "pin  code is 1", "pin\tcode is 1", "api  key=1", "api\tkey=1",
    "api_ key=1", "foo-api key=1", "foo api key=1", "access key=1", "access-key=1", "accesskey=1",
    "private key=1", "private-key: 1", "privatekey=1", "pin number is 77", "PIN CODE IS 4321",
    "the pin number: 99 and token=7", "token=a token=b", 'token="a token=b" password=c',
    "token = = x", "token is = x", "token is is x", "tokens=1", "mytoken=1", "token-=x", "-token=x",
    "token_=x", "_token=x", "--token=abc", "token==x", "token:=x", "token :x", 'token"=x', "token'=x",
    "secret: \"unterminated", "secret: 'unterminated", "password\n=x", "password=\nx", "pass word=1",
    "a-b-c-password-d-e=7", "x:token=1", "(password=1)", "[token]=1", "token=1\npassword=2\nsecret=3",
    "Bearer token is abc", "the secret is out, the token is here", "secret is", "secret is ", "is token",
    "pin=1 pin code is 2 pin number: 3", "kpassword=1", "password_=1", "password-1-2=3",
    "", "=", "token", "token=", "api key", "pin code", "\u212aey=1 token\u212a=5", "caf\u00e9_token=1",
    "naïve password is hunter2",
]


class SecretAssignmentRewriteTests(unittest.TestCase):
    def test_secret_assignment_rewrite_matches_old_behaviour(self):
        self.assertGreaterEqual(len(_ASSIGNMENT_CORPUS), 40)
        for text in _ASSIGNMENT_CORPUS:
            with self.subTest(text=text):
                self.assertEqual(jarvis.redact(text), _old_redact(text))
                self.assertEqual(audit_module.contains_secret(text), _old_contains_secret(text))

    def test_secret_assignment_rewrite_matches_old_behaviour_on_random_text(self):
        pieces = ["token", "password", "pin", "api", "key", "code", "number", "secret", "is", " ", "  ",
                  "_", "-", "=", ":", '"', "'", ",", ";", "}", "x", "abc", "1", "\t", "\n", "API",
                  "Key", "pwd", "access", "private", "passphrase", "é", "TOKEN", "="]
        rng = random.Random(50)
        for _ in range(3000):
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))
            self.assertEqual(jarvis.redact(text), _old_redact(text), msg=repr(text))
            self.assertEqual(audit_module.contains_secret(text), _old_contains_secret(text), msg=repr(text))

    def test_redact_is_linear_on_long_hyphenated_strings(self):
        for unit in ("a-", "a_", "token-", "x=-"):
            text = unit * 50000
            started = time.perf_counter()
            jarvis.redact(text)
            self.assertLess(time.perf_counter() - started, 0.5, msg=unit)


class RevealInvisibleTests(unittest.TestCase):
    def test_invisible_and_direction_characters_are_shown_and_counted(self):
        text = "a\u202eb\u2066c\u200bd\u2028e\ufefff"
        self.assertEqual(reveal_invisible(text),
                         ("a\\u{202e}b\\u{2066}c\\u{200b}d\\u{2028}e\\u{feff}f", 5))

    def test_ordinary_text_is_unchanged(self):
        for text in ("", "plain text", "line one\nline two", "col\tcol", "café ünïcode 日本", "if x == 1:\n\treturn"):
            with self.subTest(text=text):
                self.assertEqual(reveal_invisible(text), (text, 0))


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
