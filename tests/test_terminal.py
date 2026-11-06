"""The terminal UI: confirmation details, developer-mode detection, /dev and /status."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    AgentTestCase,
    FakeGroqClient,
    printed_text,
)
import jarvis  # noqa: E402
from core.orchestrator import AgentResult  # noqa: E402
from tools import developer  # noqa: E402
from ui import terminal  # noqa: E402


def tagged_style(text: str, colour: str) -> str:
    return f"<{colour}>{text}"


class PrintDetailsTests(unittest.TestCase):
    def printed_lines(self, details: str) -> list[str]:
        with mock.patch("ui.terminal.style", side_effect=tagged_style), mock.patch("builtins.print") as printed:
            terminal.print_details(details)
        return [" ".join(map(str, call.args)) for call in printed.call_args_list]

    def test_diff_lines_are_coloured_and_indented(self):
        lines = self.printed_lines("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-old\n+new\n context")
        self.assertEqual(lines, ["   --- a/x.py", "   +++ b/x.py", "   <cyan>@@ -1,2 +1,2 @@", "   <red>-old",
                                 "   <green>+new", "    context"])

    def test_real_colour_codes_on_a_terminal(self):
        tty = mock.Mock(isatty=mock.Mock(return_value=True))
        with mock.patch.dict(os.environ), mock.patch("sys.stdout", tty), mock.patch("builtins.print") as printed:
            os.environ.pop("NO_COLOR", None)
            terminal.print_details("+added\n-removed")
        self.assertEqual([call.args[0] for call in printed.call_args_list],
                         ["   \033[32m+added\033[0m", "   \033[31m-removed\033[0m"])

    def test_long_details_are_capped_with_a_note(self):
        lines = self.printed_lines("\n".join(f"+line {i}" for i in range(terminal.MAX_DETAIL_LINES + 7)))
        # Every change that passes the review limit is shown in full.
        self.assertGreaterEqual(terminal.MAX_DETAIL_LINES, developer.MAX_REVIEW_LINES)
        self.assertEqual(len(lines), terminal.MAX_DETAIL_LINES + 1)
        self.assertEqual(lines[terminal.MAX_DETAIL_LINES - 1], f"   <green>+line {terminal.MAX_DETAIL_LINES - 1}")
        self.assertEqual(lines[-1], "   <dim>... 7 more lines not shown")
        exactly = self.printed_lines("\n".join(" same" for _ in range(terminal.MAX_DETAIL_LINES)))
        self.assertEqual(len(exactly), terminal.MAX_DETAIL_LINES)
        self.assertNotIn("not shown", exactly[-1])

    def test_invisible_characters_are_revealed_with_a_warning(self):
        lines = self.printed_lines("+access = 'user\u202e \u2066// admin'\n+x = 1\u2028y = 2\n same")
        self.assertEqual(lines, ["   <yellow>!! 3 invisible or text-direction character(s), shown as \\u{...}",
                                 "   <green>+access = 'user\\u{202e} \\u{2066}// admin'",
                                 "   <green>+x = 1\\u{2028}y = 2",  # a line separator does not split the line
                                 "    same"])
        self.assertTrue(all("<yellow>" not in line for line in self.printed_lines("+plain\n-text\t tab")))

    def test_control_characters_from_file_content_are_shown_not_run(self):
        # Escape sequences in a diff must never reach the terminal raw, and must not vanish silently
        # either: they are shown as \u{...} with a warning, so the user sees what will be written.
        lines = self.printed_lines("+print('hi')\x1b[2J\x1b]0;owned\x07\n-x\r")
        self.assertTrue(all("\x1b" not in line and "\x07" not in line and "\r" not in line for line in lines))
        self.assertEqual(len(lines), 3)
        self.assertIn("invisible", lines[0])
        self.assertIn("\\u{001b}", lines[1])


class TerminalConfirmTests(unittest.TestCase):
    REQUEST = jarvis.ConfirmationRequest("edit_workspace_file", jarvis.Permission.SENSITIVE_ACTION,
                                         "Edit app.py: 1 line(s) added, 1 removed.", ["/w/app.py"], "",
                                         "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2")

    def ask(self, request, answer="yes", tty=True):
        stdin = mock.Mock(isatty=mock.Mock(return_value=tty))
        with mock.patch("sys.stdin", stdin), mock.patch("ui.terminal.style", side_effect=tagged_style), \
                mock.patch("builtins.input", return_value=answer) as typed, \
                mock.patch("builtins.print") as printed:
            approved = terminal.terminal_confirm(request)
        return approved, printed_text(printed), typed

    def test_details_are_shown_before_the_question(self):
        approved, text, typed = self.ask(self.REQUEST)
        self.assertTrue(approved)
        typed.assert_called_once()
        self.assertIn("[SENSITIVE ACTION: edit_workspace_file]", text)
        self.assertIn("Operation: Edit app.py", text)
        self.assertIn("Affects:   /w/app.py", text)
        self.assertIn("<red>-x = 1", text)
        self.assertIn("<green>+x = 2", text)
        self.assertLess(text.index("Affects:"), text.index("+x = 2"))

    def test_answers(self):
        for answer, expected in (("yes", True), (" Y ", True), ("no", False), ("", False), ("yes please", False)):
            with self.subTest(answer=answer):
                self.assertIs(self.ask(self.REQUEST, answer)[0], expected)

    def test_no_details_prints_no_details_block(self):
        request = jarvis.ConfirmationRequest("open_url", jarvis.Permission.CONTROLLED_ACTION, "Open x", [])
        with mock.patch("ui.terminal.print_details") as details:
            self.ask(request)
        details.assert_not_called()

    def test_without_a_terminal_nothing_is_asked(self):
        approved, text, typed = self.ask(self.REQUEST, tty=False)
        self.assertFalse(approved)
        typed.assert_not_called()
        self.assertEqual(text, "")

    def test_end_of_input_declines(self):
        stdin = mock.Mock(isatty=mock.Mock(return_value=True))
        with mock.patch("sys.stdin", stdin), mock.patch("builtins.input", side_effect=EOFError), \
                mock.patch("builtins.print"):
            self.assertFalse(terminal.terminal_confirm(self.REQUEST))


class LooksLikeDeveloperRequestTests(unittest.TestCase):
    def test_code_words(self):
        for text in ("fix the failing tests", "What changed in my repo today?", "run the linter", "show me the git diff",
                     "refactor this function", "TESTS please", "there's a stack trace", "why this Traceback",
                     "implement the README plan", "npm install fails", "commit message ideas"):
            with self.subTest(text=text):
                self.assertTrue(terminal.looks_like_developer_request(text))

    def test_everyday_requests(self):
        for text in ("what time is it", "play some classical music", "the latest news", "tell me a joke",
                     "remind me to buy coffee", "", "contest results", "testament"):
            with self.subTest(text=text):
                self.assertFalse(terminal.looks_like_developer_request(text))

    def test_project_names(self):
        names = {"shopfront", "my-app", "jarvis-mark-50"}
        for text in ("how is shopfront doing", "Open My-App please", "status of jarvis-mark-50?",
                     "what about shopfront, anything new"):
            with self.subTest(text=text):
                self.assertTrue(terminal.looks_like_developer_request(text, names))
        self.assertFalse(terminal.looks_like_developer_request("how is the shop doing", names))
        self.assertFalse(terminal.looks_like_developer_request("how is shopfront doing"))

    def test_project_name_at_the_end_of_a_sentence(self):
        # Typed or transcribed sentences often end with a full stop.
        self.assertTrue(terminal.looks_like_developer_request("Let's look at shopfront.", {"shopfront"}))


class DeveloperModeCLITests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.voice = jarvis.VoiceEngine(enabled=False, player=["fake-player"])
        self.addCleanup(self.voice.shutdown)
        self.cli = self.make_cli()

    def make_cli(self, config=None):
        ctx = jarvis.AgentContext(FakeGroqClient(), config or self.config, self.registry, self.policy, self.memory)
        with mock.patch("builtins.print"):
            return jarvis.JarvisCLI(ctx, self.voice, self.memory)

    def command(self, line: str) -> str:
        with mock.patch("builtins.print") as printed:
            self.cli.handle_command(line)
        return printed_text(printed)

    def send(self, text: str, cli=None) -> tuple[set[str], str]:
        cli = cli or self.cli
        with mock.patch("ui.terminal.run_agent", return_value=AgentResult("Done, Sir.", "complete", [], 1)) as agent, \
                mock.patch("builtins.print") as printed:
            cli.handle_message(text)
        agent.assert_called_once()
        return agent.call_args.kwargs["tool_groups"], printed_text(printed)

    def test_dev_command(self):
        self.assertFalse(self.cli.developer_mode)
        self.assertEqual(self.command("/dev").strip(), "Developer tools are off, switching on when you ask about code. "
                                                       "Use /dev on, /dev off or /dev auto.")
        self.assertEqual(self.command("/dev on").strip(), "Developer tools are on.")
        self.assertTrue(self.cli.developer_mode)
        self.assertEqual(self.command("/dev status").strip(),
                         "Developer tools are on. Use /dev on, /dev off or /dev auto.")
        self.assertTrue(self.cli.developer_mode)
        self.assertEqual(self.command("/dev off").strip(), "Developer tools are off until you type /dev on or /dev auto.")
        self.assertFalse(self.cli.developer_mode)
        self.assertEqual(self.command("/dev auto").strip(), "Developer tools are off, switching on when you ask about code.")
        self.assertFalse(self.cli.developer_mode)
        self.assertIn("/dev on|off", jarvis.HELP_TEXT)

    def test_dev_off_stays_off_until_dev_on_or_dev_auto(self):
        self.command("/dev off")
        groups, text = self.send("please fix the failing test in my repo")
        self.assertEqual(groups, {"core"})
        self.assertFalse(self.cli.developer_mode)
        self.assertNotIn("developer tools on", text)
        self.command("/dev auto")  # automatic switching is back
        self.assertEqual(self.send("please fix the failing test in my repo")[0], {"core", "developer"})
        self.assertTrue(self.cli.developer_mode)
        self.command("/dev off")
        self.assertEqual(self.send("what time is it")[0], {"core"})
        self.command("/dev on")
        self.assertEqual(self.send("what time is it")[0], {"core", "developer"})

    def test_status_after_dev_off_does_not_promise_automatic_switching(self):
        self.command("/dev off")
        text = self.command("/status")
        self.assertIn("Developer tools  off", text)
        self.assertNotIn("on when you ask about code", text)
        self.command("/dev auto")
        self.assertIn("Developer tools  off (on when you ask about code)", self.command("/status"))

    def test_incomplete_task_suffix(self):
        result = AgentResult("I edited app.py but could not run the tests.", "incomplete", [], 3)
        with mock.patch("ui.terminal.run_agent", return_value=result), mock.patch("builtins.print") as printed:
            self.cli.handle_message("fix my tests")
        text = printed_text(printed)
        self.assertIn("I edited app.py but could not run the tests.\n\n(The task is incomplete.)", text)
        self.assertNotIn("tool rounds", text)
        self.assertEqual(self.cli.conversation.turns, [("fix my tests", result.text)])  # the suffix is not stored

    def test_everyday_message_offers_core_tools_only(self):
        groups, text = self.send("what time is it")
        self.assertEqual(groups, {"core"})
        self.assertFalse(self.cli.developer_mode)
        self.assertNotIn("developer tools on", text)

    def test_dev_on_offers_developer_tools(self):
        self.command("/dev on")
        self.assertEqual(self.send("what time is it")[0], {"core", "developer"})
        self.command("/dev off")
        self.assertEqual(self.send("what time is it")[0], {"core"})

    def test_code_message_turns_developer_mode_on_and_says_so_once(self):
        groups, text = self.send("please fix the failing test")
        self.assertEqual(groups, {"core", "developer"})
        self.assertTrue(self.cli.developer_mode)
        self.assertEqual(text.count("developer tools on for this session"), 1)
        groups, text = self.send("and check the git diff")
        self.assertEqual(groups, {"core", "developer"})
        self.assertNotIn("developer tools on for this session", text)
        self.assertEqual(self.send("thanks, what time is it")[0], {"core", "developer"})  # stays on

    def test_workspace_project_name_turns_developer_mode_on(self):
        (self.workspace / "shopfront").mkdir()
        (self.workspace / ".hidden").mkdir()
        (self.workspace / "groceries").write_text("milk")
        self.assertEqual(self.send("show .hidden")[0], {"core"})
        self.assertEqual(self.send("groceries please")[0], {"core"})
        groups, text = self.send("how is Shopfront doing")
        self.assertEqual(groups, {"core", "developer"})
        self.assertIn("developer tools on for this session", text)

    def test_missing_workspace_is_harmless(self):
        cli = self.make_cli(self.make_config(workspace=self.tmp / "does-not-exist"))
        self.assertEqual(self.send("hello there", cli=cli)[0], {"core"})

    def test_status_shows_developer_tools_and_round_limits(self):
        text = self.command("/status")
        self.assertIn("Developer tools  off (on when you ask about code)", text)
        self.assertIn("Max tool rounds  8 (14 with developer tools)", text)
        self.command("/dev on")
        self.assertIn("Developer tools  on\n", self.command("/status") + "\n")


class MicrophoneHintTests(AgentTestCase):
    """With no voice input on, JARVIS says so instead of silently ignoring an empty Enter."""

    make_cli = DeveloperModeCLITests.make_cli

    def setUp(self):
        super().setUp()
        self.voice = jarvis.VoiceEngine(enabled=False, player=["fake-player"])
        self.addCleanup(self.voice.shutdown)
        self.cli = self.make_cli()

    def test_banner_says_the_microphone_is_off(self):
        with mock.patch("builtins.print") as printed:
            self.cli.banner()
        output = printed_text(printed)
        self.assertIn("Microphone off", output)
        self.assertIn("/wake on", output)
        self.assertIn("replies in text only", output)

    def test_empty_enter_explains_how_to_talk_once(self):
        with mock.patch.object(self.cli, "_read_line", side_effect=["", "", ""]), \
                mock.patch("builtins.print") as printed:
            for _ in range(3):
                self.assertFalse(self.cli._step())
        self.assertEqual(printed_text(printed).count("The microphone is off"), 1)

    def test_no_hint_when_push_to_talk_is_on(self):
        self.cli.listen_mode = True
        with mock.patch.object(self.cli, "_read_line", return_value=""), \
                mock.patch.object(self.cli, "voice_turn", return_value="") as voice_turn, \
                mock.patch("builtins.print") as printed:
            self.cli._step()
        voice_turn.assert_called_once()
        self.assertNotIn("The microphone is off", printed_text(printed))


if __name__ == "__main__":
    unittest.main()
