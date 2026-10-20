"""Configuration and .env loading."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    FAKE_KEY,
)
import jarvis  # noqa: E402


class ConfigTests(unittest.TestCase):
    def env(self, **extra):
        values = {"GROQ_API_KEY": FAKE_KEY}
        values.update(extra)
        return values

    def test_missing_api_key_raises_clear_error(self):
        with self.assertRaises(jarvis.ConfigError) as ctx:
            jarvis.Config.from_env(env={})
        message = str(ctx.exception)
        self.assertIn("GROQ_API_KEY is not set", message)
        self.assertIn("console.groq.com", message)

    def test_blank_api_key_raises(self):
        with self.assertRaises(jarvis.ConfigError):
            jarvis.Config.from_env(env={"GROQ_API_KEY": "   "})

    def test_placeholder_keys_rejected(self):
        for key in ("your_groq_api_key_here", "<your-key>", "xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                    "changeme_changeme_changeme", "[insert key here]", "gsk_short", "abc"):
            with self.subTest(key=key):
                with self.assertRaises(jarvis.ConfigError):
                    jarvis.Config.from_env(env={"GROQ_API_KEY": key})

    def test_key_with_spaces_rejected(self):
        with self.assertRaises(jarvis.ConfigError):
            jarvis.validate_api_key("gsk_aaaaaaaaaa bbbbbbbbbbbbbbbbbbbb")

    def test_valid_key_accepted_and_stripped(self):
        self.assertEqual(jarvis.validate_api_key(f"  {FAKE_KEY}\n"), FAKE_KEY)
        config = jarvis.Config.from_env(env=self.env())
        self.assertEqual(config.api_key, FAKE_KEY)
        self.assertEqual(config.model, jarvis.DEFAULT_MODEL)
        self.assertEqual(config.permission_mode, "standard")

    def test_groq_model_override(self):
        config = jarvis.Config.from_env(env=self.env(GROQ_MODEL="  openai/gpt-oss-120b "))
        self.assertEqual(config.model, "openai/gpt-oss-120b")

    def test_invalid_integers_rejected(self):
        for name, value in (("JARVIS_MAX_TOOL_ROUNDS", "abc"), ("JARVIS_MAX_TOOL_ROUNDS", "0"),
                            ("JARVIS_MAX_TOOL_ROUNDS", "99"), ("JARVIS_SEARCH_LIMIT", "1.5"),
                            ("JARVIS_SEARCH_LIMIT", "11"), ("JARVIS_HISTORY_TURNS", "-1")):
            with self.subTest(name=name, value=value):
                with self.assertRaises(jarvis.ConfigError) as ctx:
                    jarvis.Config.from_env(env=self.env(**{name: value}))
                self.assertIn(name, str(ctx.exception))

    def test_valid_integers_accepted(self):
        config = jarvis.Config.from_env(env=self.env(JARVIS_MAX_TOOL_ROUNDS="3", JARVIS_SEARCH_LIMIT="7"))
        self.assertEqual(config.max_tool_rounds, 3)
        self.assertEqual(config.search_limit, 7)

    def test_invalid_bool_rejected(self):
        with self.assertRaises(jarvis.ConfigError) as ctx:
            jarvis.Config.from_env(env=self.env(JARVIS_TTS_ENABLED="maybe"))
        self.assertIn("JARVIS_TTS_ENABLED", str(ctx.exception))

    def test_bool_values_parsed(self):
        self.assertFalse(jarvis.Config.from_env(env=self.env(JARVIS_TTS_ENABLED="off")).tts_enabled)
        self.assertTrue(jarvis.Config.from_env(env=self.env(JARVIS_TTS_ENABLED="YES")).tts_enabled)

    def test_invalid_permission_mode_rejected(self):
        with self.assertRaises(jarvis.ConfigError) as ctx:
            jarvis.Config.from_env(env=self.env(JARVIS_PERMISSION_MODE="god_mode"))
        self.assertIn("JARVIS_PERMISSION_MODE", str(ctx.exception))
        config = jarvis.Config.from_env(env=self.env(JARVIS_PERMISSION_MODE=" Read_Only "))
        self.assertEqual(config.permission_mode, "read_only")

    def test_invalid_log_level_rejected(self):
        with self.assertRaises(jarvis.ConfigError):
            jarvis.Config.from_env(env=self.env(JARVIS_LOG_LEVEL="loud"))

    def test_jarvis_apps_parsing(self):
        raw = "Obsidian=obsidian; music=flatpak run com.spotify.Client ;; editor2='/opt/My App/app' --flag"
        apps = jarvis.parse_app_overrides(raw)
        self.assertEqual(apps["obsidian"], [["obsidian"]])
        self.assertEqual(apps["music"], [["flatpak", "run", "com.spotify.Client"]])
        self.assertEqual(apps["editor2"], [["/opt/My App/app", "--flag"]])

        config = jarvis.Config.from_env(env=self.env(JARVIS_APPS="music=flatpak run com.spotify.Client"))
        self.assertEqual(config.apps["music"], [["flatpak", "run", "com.spotify.Client"]])
        self.assertIn("firefox", config.apps)  # defaults are kept

    def test_jarvis_apps_invalid_entries_rejected(self):
        for raw in ("noequals", "=cmd", "name=", "bad=\"unterminated"):
            with self.subTest(raw=raw):
                with self.assertRaises(jarvis.ConfigError):
                    jarvis.parse_app_overrides(raw)

    def test_path_settings_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = jarvis.Config.from_env(env=self.env(JARVIS_WORKSPACE=tmp, JARVIS_LOG_DIR=tmp + "/logs"))
            self.assertEqual(config.workspace, Path(tmp))
            self.assertEqual(config.log_dir, Path(tmp) / "logs")


class LoadEnvFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".env"

    def tearDown(self):
        self._tmp.cleanup()

    def test_does_not_override_and_handles_quotes_and_comments(self):
        self.path.write_text(
            "# a comment line\n"
            "\n"
            "export A=1\n"
            "B=\"hello world\"\n"
            "C='single # not a comment'\n"
            "D=value # trailing comment\n"
            "EXISTING=from_file\n"
            "not a valid line\n"
            "URL=https://example.com/a#frag\n",
            encoding="utf-8",
        )
        env = {"EXISTING": "keep_me"}
        self.assertTrue(jarvis.load_env_file(self.path, env))
        self.assertEqual(env["A"], "1")
        self.assertEqual(env["B"], "hello world")
        self.assertEqual(env["C"], "single # not a comment")
        self.assertEqual(env["D"], "value")
        self.assertEqual(env["EXISTING"], "keep_me")
        self.assertEqual(env["URL"], "https://example.com/a#frag")
        self.assertNotIn("not a valid line", env)

    def test_quoted_value_with_trailing_comment(self):
        self.path.write_text('E="quoted value" # comment\n', encoding="utf-8")
        env: dict[str, str] = {}
        jarvis.load_env_file(self.path, env)
        self.assertEqual(env["E"], "quoted value")

    def test_missing_file_returns_false(self):
        env: dict[str, str] = {}
        self.assertFalse(jarvis.load_env_file(self.path, env))
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
