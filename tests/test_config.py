"""Configuration and .env loading."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    FAKE_KEY,
    TempDirTestCase,
    printed_text,
)
import jarvis  # noqa: E402
from config.settings import (  # noqa: E402
    COMMAND_KINDS,
    DEFAULT_APPS,
    DEFAULT_PERMISSIONS_FILE,
)


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



class ShippedPermissionsFileTests(unittest.TestCase):
    """config/permissions.toml as shipped (loaded read-only)."""

    def setUp(self):
        self.permissions = jarvis.load_permissions(DEFAULT_PERMISSIONS_FILE)

    def test_default_path_and_kinds(self):
        self.assertEqual(DEFAULT_PERMISSIONS_FILE, jarvis.SCRIPT_DIR / "config" / "permissions.toml")
        self.assertTrue(DEFAULT_PERMISSIONS_FILE.is_file())
        self.assertEqual(COMMAND_KINDS, ("tests", "lint"))

    def test_exact_command_names_and_kinds(self):
        commands = self.permissions.commands
        self.assertEqual(list(commands), ["rails-test", "npm-test", "flutter-test", "pytest", "npm-lint"])
        self.assertEqual({name: spec.kind for name, spec in commands.items()},
                         {"rails-test": "tests", "npm-test": "tests", "flutter-test": "tests",
                          "pytest": "tests", "npm-lint": "lint"})
        for name, spec in commands.items():
            with self.subTest(name=name):
                self.assertIsInstance(spec, jarvis.ProjectCommand)
                self.assertEqual(spec.name, name)
                self.assertTrue(spec.detect)
                self.assertTrue(spec.confirm)  # running project code asks first unless the user opts out
                self.assertTrue(5 <= spec.timeout <= 3600)

    def test_shipped_command_details(self):
        commands = self.permissions.commands
        self.assertEqual(commands["rails-test"].argv, ("bin/rails", "test"))
        self.assertEqual(commands["rails-test"].detect, ("bin/rails",))
        self.assertEqual(commands["npm-test"].argv, ("npm", "test", "--silent"))
        self.assertEqual(commands["npm-test"].npm_script, "test")
        self.assertEqual(commands["npm-lint"].argv, ("npm", "run", "lint", "--silent"))
        self.assertEqual(commands["npm-lint"].npm_script, "lint")
        self.assertEqual(commands["npm-lint"].timeout, 300)
        self.assertEqual(commands["flutter-test"].argv, ("flutter", "test"))
        self.assertEqual(commands["pytest"].argv, ("python3", "-m", "pytest", "-q"))
        self.assertIn("pyproject.toml", commands["pytest"].detect)
        self.assertIsNone(commands["pytest"].npm_script)

    def test_no_project_rules_or_extra_applications(self):
        self.assertEqual(self.permissions.projects, {})
        self.assertEqual(self.permissions.applications, {})


class LoadPermissionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-perm-")
        self.path = Path(self._tmp.name) / "permissions.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, text: str) -> jarvis.Permissions:
        self.path.write_text(text, encoding="utf-8")
        return jarvis.load_permissions(self.path)

    def assert_rejected(self, text: str, fragment: str) -> None:
        with self.assertRaises(jarvis.ConfigError) as ctx:
            self.load(text)
        message = str(ctx.exception)
        self.assertIn(str(self.path), message)
        self.assertIn(fragment, message)

    def test_missing_file_allows_nothing(self):
        permissions = jarvis.load_permissions(self.path)
        self.assertEqual((permissions.applications, permissions.commands, permissions.projects), ({}, {}, {}))

    def test_empty_file_allows_nothing(self):
        permissions = self.load("")
        self.assertEqual((permissions.applications, permissions.commands, permissions.projects), ({}, {}, {}))

    def test_full_file_parsed(self):
        permissions = self.load(
            '[applications]\n'
            'Obsidian = "obsidian --safe"\n'
            'player = "\'/opt/My Player/run\' --x"\n'
            '[commands.unit]\n'
            'argv = ["python", "-m", "pytest"]\n'
            'detect = ["pyproject.toml"]\n'
            'timeout = 5\n'
            'description = "unit tests"\n'
            '[commands.web-lint_2]\n'
            'argv = ["npm", "run", "lint"]\n'
            'kind = "lint"\n'
            'npm_script = "lint"\n'
            'timeout = 3600\n'
            'confirm = false\n'
            '[projects.app]\n'
            'tests = "unit"\n'
            'lint = "web-lint_2"\n'
            'confirm = false\n'
            '[projects."other app"]\n'
        )
        self.assertEqual(permissions.applications, {"obsidian": [["obsidian", "--safe"]],
                                                    "player": [["/opt/My Player/run", "--x"]]})
        self.assertEqual(permissions.commands["unit"], jarvis.ProjectCommand(
            name="unit", argv=("python", "-m", "pytest"), kind="tests", detect=("pyproject.toml",),
            npm_script=None, timeout=5, confirm=True, description="unit tests"))
        lint = permissions.commands["web-lint_2"]
        self.assertEqual((lint.kind, lint.npm_script, lint.timeout, lint.confirm, lint.detect),
                         ("lint", "lint", 3600, False, ()))
        self.assertEqual(permissions.projects["app"], jarvis.ProjectRule("unit", "web-lint_2", False))
        self.assertEqual(permissions.projects["other app"], jarvis.ProjectRule(None, None, None))

    def test_command_defaults(self):
        spec = self.load('[commands.t]\nargv = ["make", "test"]\n').commands["t"]
        self.assertEqual(spec, jarvis.ProjectCommand("t", ("make", "test")))
        self.assertEqual((spec.kind, spec.detect, spec.npm_script, spec.timeout, spec.confirm, spec.description),
                         ("tests", (), None, 600, True, ""))

    def test_dataclasses_are_frozen(self):
        spec = jarvis.ProjectCommand("t", ("make",))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.argv = ("rm", "-rf", "/")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            jarvis.ProjectRule().confirm = False

    def test_invalid_toml_rejected(self):
        self.assert_rejected("[commands.t\nargv = 1", "could not be read as TOML")

    def test_unknown_section_rejected(self):
        self.assert_rejected('[shell]\ncmd = "bash"\n', "unknown section(s): shell")

    def test_invalid_applications_rejected(self):
        for text, fragment in (('[applications]\nvim = 3\n', "applications.vim must be a command string"),
                               ('[applications]\nvim = "  "\n', "applications.vim must be a command string"),
                               ('[applications.vim]\nx = "vim"\n', "applications.vim must be a command string"),
                               ('[applications]\nvim = "vim \'unterminated"\n', "applications.vim is not a valid command")):
            with self.subTest(text=text):
                self.assert_rejected(text, fragment)

    def test_invalid_command_names_rejected(self):
        for name in ("Upper", "-dash-first", '"has space"', '"a.b"', "a" * 42):
            with self.subTest(name=name):
                self.assert_rejected(f'[commands.{name}]\nargv = ["x"]\n', "names use lowercase letters")

    def test_invalid_command_fields_rejected(self):
        cases = (
            ('[commands]\nt = "make test"\n', "commands.t must be a table"),
            ('[commands.t]\nargv = ["x"]\nshell = true\n', "commands.t: unknown key(s) shell"),
            ('[commands.t]\nkind = "tests"\n', "commands.t.argv must be a list of non-empty strings"),
            ('[commands.t]\nargv = "make test"\n', "commands.t.argv must be a list of non-empty strings"),
            ('[commands.t]\nargv = ["make", 1]\n', "commands.t.argv must be a list of non-empty strings"),
            ('[commands.t]\nargv = ["make", " "]\n', "commands.t.argv must be a list of non-empty strings"),
            ('[commands.t]\nargv = []\n', "commands.t.argv must not be empty"),
            ('[commands.t]\nargv = ["x"]\nkind = "build"\n', "commands.t.kind must be one of tests, lint"),
            ('[commands.t]\nargv = ["x"]\ndetect = "package.json"\n', "commands.t.detect must be a list"),
            ('[commands.t]\nargv = ["x"]\nnpm_script = ""\n', "commands.t.npm_script must be a script name"),
            ('[commands.t]\nargv = ["x"]\nnpm_script = 3\n', "commands.t.npm_script must be a script name"),
            ('[commands.t]\nargv = ["x"]\nconfirm = "no"\n', "commands.t.confirm must be true or false"),
        )
        for text, fragment in cases:
            with self.subTest(text=text):
                self.assert_rejected(text, fragment)

    def test_timeout_bounds(self):
        for value in ("4", "3601", "0", "-5", "10.0", '"60"', "true"):
            with self.subTest(timeout=value):
                self.assert_rejected(f'[commands.t]\nargv = ["x"]\ntimeout = {value}\n',
                                     "commands.t.timeout must be a whole number of seconds between 5 and 3600")
        for value in (5, 3600):
            with self.subTest(timeout=value):
                self.assertEqual(self.load(f'[commands.t]\nargv = ["x"]\ntimeout = {value}\n')
                                 .commands["t"].timeout, value)

    def test_invalid_project_rules_rejected(self):
        commands = '[commands.unit]\nargv = ["x"]\n[commands.lint]\nargv = ["y"]\nkind = "lint"\n'
        cases = (
            ('[projects]\napp = "unit"\n', "projects.app may only set tests, lint and confirm"),
            ('[projects.app]\ncommand = "bash"\n', "projects.app may only set tests, lint and confirm"),
            ('[projects.app]\ntests = "missing"\n', "projects.app.tests must name a [commands] entry of kind 'tests'"),
            ('[projects.app]\ntests = "lint"\n', "projects.app.tests must name a [commands] entry of kind 'tests'"),
            ('[projects.app]\nlint = "unit"\n', "projects.app.lint must name a [commands] entry of kind 'lint'"),
            ('[projects.app]\nconfirm = "false"\n', "projects.app.confirm must be true or false"),
        )
        for text, fragment in cases:
            with self.subTest(text=text):
                self.assert_rejected(commands + text, fragment)

    def test_sections_that_are_not_tables_rejected(self):
        # A section written as a plain value (e.g. `commands = ["pytest"]`) must give a ConfigError,
        # which main() reports cleanly, not an AttributeError traceback.
        for text in ('applications = "firefox"\n', 'commands = ["pytest"]\n', "projects = 3\n"):
            with self.subTest(text=text):
                with self.assertRaises(jarvis.ConfigError) as ctx:
                    self.load(text)
                self.assertIn(str(self.path), str(ctx.exception))


class ConfigPermissionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-perm-")
        self.path = Path(self._tmp.name) / "perms.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def from_env(self, **extra) -> jarvis.Config:
        env = {"GROQ_API_KEY": FAKE_KEY}
        env.update(extra)
        return jarvis.Config.from_env(env=env)

    def test_dataclass_defaults(self):
        config = jarvis.Config(api_key=FAKE_KEY)
        self.assertEqual(config.permissions_file, DEFAULT_PERMISSIONS_FILE)
        self.assertEqual((config.commands, config.projects), ({}, {}))
        self.assertEqual(config.apps, DEFAULT_APPS)

    def test_default_file_used_when_unset_or_blank(self):
        shipped = jarvis.load_permissions(DEFAULT_PERMISSIONS_FILE)
        for extra in ({}, {"JARVIS_PERMISSIONS_FILE": "   "}):
            with self.subTest(extra=extra):
                config = self.from_env(**extra)
                self.assertEqual(config.permissions_file, DEFAULT_PERMISSIONS_FILE)
                self.assertEqual(config.commands, shipped.commands)
                self.assertEqual(config.projects, shipped.projects)

    def test_file_from_env_is_loaded(self):
        self.path.write_text('[commands.check]\nargv = ["make", "check"]\n'
                             '[projects.app]\ntests = "check"\nconfirm = false\n', encoding="utf-8")
        config = self.from_env(JARVIS_PERMISSIONS_FILE=f"  {self.path}  ")
        self.assertEqual(config.permissions_file, self.path)
        self.assertEqual(list(config.commands), ["check"])
        self.assertEqual(config.commands["check"].argv, ("make", "check"))
        self.assertEqual(config.projects, {"app": jarvis.ProjectRule("check", None, False)})

    def test_missing_file_means_no_commands(self):
        config = self.from_env(JARVIS_PERMISSIONS_FILE=str(self.path))
        self.assertEqual(config.permissions_file, self.path)
        self.assertEqual((config.commands, config.projects), ({}, {}))
        self.assertEqual(config.apps, DEFAULT_APPS)

    def test_invalid_file_is_a_config_error(self):
        self.path.write_text('[commands.t]\nargv = []\n', encoding="utf-8")
        with self.assertRaises(jarvis.ConfigError) as ctx:
            self.from_env(JARVIS_PERMISSIONS_FILE=str(self.path))
        self.assertIn(str(self.path), str(ctx.exception))

    def test_apps_precedence_builtin_then_file_then_env(self):
        self.path.write_text('[applications]\nfirefox = "firefox-esr --private"\nObsidian = "obsidian"\n'
                             'notes = "gnome-text-editor"\n', encoding="utf-8")
        config = self.from_env(JARVIS_PERMISSIONS_FILE=str(self.path))
        self.assertEqual(config.apps["firefox"], [["firefox-esr", "--private"]])  # file replaces built-in
        self.assertEqual(config.apps["obsidian"], [["obsidian"]])
        self.assertEqual(config.apps["chrome"], DEFAULT_APPS["chrome"])  # other built-ins kept

        config = self.from_env(JARVIS_PERMISSIONS_FILE=str(self.path),
                               JARVIS_APPS="obsidian=flatpak run md.obsidian.Obsidian; firefox=librewolf")
        self.assertEqual(config.apps["obsidian"], [["flatpak", "run", "md.obsidian.Obsidian"]])  # env wins
        self.assertEqual(config.apps["firefox"], [["librewolf"]])
        self.assertEqual(config.apps["notes"], [["gnome-text-editor"]])
        self.assertEqual(config.apps["vscode"], DEFAULT_APPS["vscode"])
        self.assertEqual(DEFAULT_APPS["firefox"], [["firefox"]])  # the built-in table is not mutated


class PermissionsFileCheckTests(TempDirTestCase):
    def check_lines(self, config) -> list[str]:
        with mock.patch("jarvis.initialize_client", side_effect=AssertionError("no network allowed")), \
                mock.patch("builtins.print") as printed:
            jarvis.run_checks(config)
        return [line for line in printed_text(printed).splitlines() if "permissions file" in line]

    def test_existing_file_is_ok_with_counts(self):
        path = self.tmp / "perms.toml"
        path.write_text("", encoding="utf-8")
        commands = {"a": jarvis.ProjectCommand("a", ("x",)), "b": jarvis.ProjectCommand("b", ("y",)),
                    "c": jarvis.ProjectCommand("c", ("z",), kind="lint")}
        config = self.make_config(api_key="not-a-key", permissions_file=path, commands=commands,
                                  projects={"app": jarvis.ProjectRule(confirm=False)})
        lines = self.check_lines(config)
        self.assertEqual(len(lines), 1)
        self.assertIn("[ok  ] permissions file", lines[0])
        self.assertIn(str(path), lines[0])
        self.assertIn("2 test and 1 lint commands, 1 project rules", lines[0])

    def test_missing_file_is_a_warning(self):
        path = self.tmp / "absent.toml"
        lines = self.check_lines(self.make_config(api_key="not-a-key", permissions_file=path))
        self.assertEqual(len(lines), 1)
        self.assertIn("[warn] permissions file", lines[0])
        self.assertIn(f"{path} not found; no project commands are allowed", lines[0])

if __name__ == "__main__":
    unittest.main()
