"""Web search and Linux integration tools."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    AgentTestCase,
    FakeBrowser,
    Recorder,
    TempDirTestCase,
    make_ddgs_factory,
)
import jarvis  # noqa: E402


class WebSearchTests(TempDirTestCase):
    def make_tools(self, factory, **config_overrides):
        config = self.make_config(**config_overrides)
        memory = jarvis.MemoryStore(self.memory_path)
        return jarvis.JarvisTools(config, memory, browser_open=FakeBrowser(), ddgs_factory=factory)

    @staticmethod
    def raw_results(n, body="snippet"):
        return [{"title": f"Title {i}", "href": f"https://example.com/{i}", "body": f"{body} {i}"} for i in range(n)]

    def test_results_formatted(self):
        calls: list = []
        tools = self.make_tools(make_ddgs_factory(self.raw_results(2), calls=calls))
        result = tools.search_web("  python release  ")
        self.assertEqual(result["query"], "python release")
        self.assertEqual(calls[0]["query"], "python release")
        self.assertEqual(result["results"][0], {"title": "Title 0", "url": "https://example.com/0",
                                                "snippet": "snippet 0"})
        self.assertEqual(result["notice"], jarvis.UNTRUSTED_NOTICE)
        self.assertIn("snippets", result["note"])

    def test_alternative_keys_and_non_dict_items(self):
        raw = [{"title": "A", "url": "https://a.example", "snippet": "alt"}, "garbage", None]
        tools = self.make_tools(make_ddgs_factory(raw))
        result = tools.search_web("q")
        self.assertEqual(result["results"], [{"title": "A", "url": "https://a.example", "snippet": "alt"}])

    def test_snippet_and_title_length_bounded(self):
        raw = [{"title": "T" * 1000, "href": "https://example.com", "body": "S" * 5000}]
        tools = self.make_tools(make_ddgs_factory(raw))
        item = tools.search_web("q")["results"][0]
        self.assertLessEqual(len(item["snippet"]), 300)
        self.assertLessEqual(len(item["title"]), 150)

    def test_result_count_bounded_by_search_limit(self):
        calls: list = []
        tools = self.make_tools(make_ddgs_factory(self.raw_results(20), calls=calls), search_limit=3)
        self.assertEqual(len(tools.search_web("q")["results"]), 3)
        self.assertEqual(len(tools.search_web("q", max_results=10)["results"]), 3)
        self.assertEqual(len(tools.search_web("q", max_results=2)["results"]), 2)
        self.assertEqual([c["max_results"] for c in calls], [3, 3, 2])

    def test_empty_results_message(self):
        tools = self.make_tools(make_ddgs_factory([]))
        result = tools.search_web("zzzz")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["results"], [])
        self.assertIn("No results", result["message"])

    def test_no_results_exception_treated_as_empty(self):
        class DDGSException(Exception):
            pass

        tools = self.make_tools(make_ddgs_factory(errors=[DDGSException("No results found.")]))
        result = tools.search_web("zzzz")
        self.assertEqual(result["results"], [])
        self.assertIn("No results", result["message"])

    def test_exception_surfaces_as_error_via_execute_tool(self):
        tools = self.make_tools(make_ddgs_factory(errors=[RuntimeError("backend exploded")]))
        registry = jarvis.build_registry(tools)
        policy = jarvis.PermissionPolicy("standard", confirm=Recorder(True))
        with mock.patch("jarvis.time.sleep") as sleep:
            result = jarvis.execute_tool(registry, policy, "search_web", json.dumps({"query": "x"}))
        self.assertEqual(result["status"], "error")
        self.assertIn("Web search failed", result["error"])
        self.assertIn("backend exploded", result["error"])
        sleep.assert_not_called()

    def test_rate_limit_exception_message(self):
        class RatelimitException(Exception):
            pass

        tools = self.make_tools(make_ddgs_factory(errors=[RatelimitException("202 Ratelimit")]))
        with self.assertRaises(jarvis.ToolError) as ctx:
            tools.search_web("q")
        self.assertIn("rate-limiting", str(ctx.exception))
        tools = self.make_tools(make_ddgs_factory(errors=[RuntimeError("HTTP 429 Too Many Requests")]))
        with self.assertRaises(jarvis.ToolError) as ctx:
            tools.search_web("q")
        self.assertIn("rate-limiting", str(ctx.exception))

    def test_timeout_is_retried_once(self):
        calls: list = []
        tools = self.make_tools(make_ddgs_factory(self.raw_results(1), errors=[TimeoutError("timed out")],
                                                  calls=calls))
        with mock.patch("jarvis.time.sleep") as sleep:
            result = tools.search_web("q")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(1.0)

    def test_repeated_timeout_gives_error(self):
        tools = self.make_tools(make_ddgs_factory(errors=[TimeoutError("timed out"), TimeoutError("timed out")]))
        with mock.patch("jarvis.time.sleep"):
            with self.assertRaises(jarvis.ToolError) as ctx:
                tools.search_web("q")
        self.assertIn("TimeoutError", str(ctx.exception))


class LinuxIntegrationTests(AgentTestCase):
    def test_launch_app_missing_executable(self):
        with mock.patch("jarvis.shutil.which", return_value=None), \
                mock.patch("jarvis.subprocess.Popen") as popen:
            with self.assertRaises(jarvis.ToolError) as ctx:
                self.tools.open_application("vscode")
        self.assertIn("not installed", str(ctx.exception))
        self.assertIn("code", str(ctx.exception))
        popen.assert_not_called()

    def test_launch_app_uses_argv_list_and_new_session(self):
        process = mock.Mock(pid=4242)
        process.poll.return_value = None
        which = lambda name: f"/usr/bin/{name}" if name == "firefox" else None  # noqa: E731
        with mock.patch("jarvis.shutil.which", side_effect=which), \
                mock.patch("jarvis.subprocess.Popen", return_value=process) as popen, \
                mock.patch("jarvis.time.sleep") as sleep:
            result = self.tools.open_application("Firefox")
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0], ["/usr/bin/firefox"])
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        sleep.assert_called_once()
        self.assertEqual(result["app"], "firefox")
        self.assertEqual(result["pid"], 4242)
        self.assertTrue(result["still_running"])

    def test_launch_app_alias_and_fallback_candidate(self):
        process = mock.Mock(pid=1)
        process.poll.return_value = None
        which = lambda name: "/usr/bin/codium" if name == "codium" else None  # noqa: E731
        with mock.patch("jarvis.shutil.which", side_effect=which), \
                mock.patch("jarvis.subprocess.Popen", return_value=process) as popen, \
                mock.patch("jarvis.time.sleep"):
            result = self.tools.open_application("VS Code")
        self.assertEqual(popen.call_args[0][0], ["/usr/bin/codium"])
        self.assertEqual(result["app"], "vscode")

    def test_unapproved_app_rejected(self):
        with mock.patch("jarvis.subprocess.Popen") as popen, \
                mock.patch("jarvis.shutil.which", return_value="/usr/bin/rm"):
            for name in ("rm", "bash -c 'rm -rf ~'", "../../bin/sh"):
                with self.subTest(name=name):
                    with self.assertRaises(jarvis.ToolError) as ctx:
                        self.tools.open_application(name)
                    self.assertIn("not an approved application", str(ctx.exception))
        popen.assert_not_called()

    def test_launch_detached_immediate_failure(self):
        process = mock.Mock(pid=7)
        process.poll.return_value = 3
        with mock.patch("jarvis.shutil.which", return_value="/usr/bin/x"), \
                mock.patch("jarvis.subprocess.Popen", return_value=process):
            with self.assertRaises(jarvis.ToolError) as ctx:
                jarvis.launch_detached(["x"], settle_seconds=0)
        self.assertIn("code 3", str(ctx.exception))

    def test_launch_detached_oserror(self):
        with mock.patch("jarvis.shutil.which", return_value="/usr/bin/x"), \
                mock.patch("jarvis.subprocess.Popen", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(jarvis.ToolError) as ctx:
                jarvis.launch_detached(["x"], settle_seconds=0)
        self.assertIn("Permission denied", str(ctx.exception))

    def test_run_command_missing_utility(self):
        with mock.patch("jarvis.shutil.which", return_value=None), \
                mock.patch("jarvis.subprocess.run") as run:
            with self.assertRaises(jarvis.ToolError) as ctx:
                jarvis.run_command(["nonexistent-tool", "--version"])
        self.assertIn("not installed", str(ctx.exception))
        run.assert_not_called()

    def test_run_command_timeout(self):
        with mock.patch("jarvis.shutil.which", return_value="/usr/bin/slow"), \
                mock.patch("jarvis.subprocess.run",
                           side_effect=subprocess.TimeoutExpired(cmd=["slow"], timeout=2)):
            with self.assertRaises(jarvis.ToolError) as ctx:
                jarvis.run_command(["slow"], timeout=2)
        self.assertIn("timed out", str(ctx.exception))

    def test_run_command_oserror(self):
        with mock.patch("jarvis.shutil.which", return_value="/usr/bin/x"), \
                mock.patch("jarvis.subprocess.run", side_effect=OSError(8, "Exec format error")):
            with self.assertRaises(jarvis.ToolError):
                jarvis.run_command(["x"])

    # inspect_git_status runs the real git binary in temporary repositories: the point of these tests is
    # to prove that git itself honours the hardening, which a mocked subprocess cannot show.
    def _git(self, repo, *args):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    def _make_repo(self, name="repo"):
        repo = self.workspace / name
        repo.mkdir()
        self._git(repo, "init", "-q")
        (repo / "file.py").write_text("x = 1\n")
        self._git(repo, "add", "file.py")
        self._git(repo, "commit", "-q", "-m", "init")
        return repo

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_status_reports_changes(self):
        repo = self._make_repo()
        (repo / "file.py").write_text("x = 2\n")
        result = self.tools.inspect_git_status("repo")
        self.assertEqual(result["status"], "success")
        self.assertIn("M file.py", result["git_status"])
        self.assertIn("file.py", result["diff_stat"])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_status_does_not_run_repo_filter_drivers_or_hooks(self):
        repo = self._make_repo()
        marker = self.tmp / "PWNED"
        # Hostile repository config: a clean filter, an fsmonitor hook and a hooks path.
        self._git(repo, "config", "filter.x.clean", f"touch {marker} && cat")
        self._git(repo, "config", "filter.x.required", "true")
        self._git(repo, "config", "core.fsmonitor", f"touch {marker}; true")
        hooks = repo / "evil-hooks"
        hooks.mkdir()
        hook = hooks / "post-index-change"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        self._git(repo, "config", "core.hooksPath", str(hooks))
        (repo / ".gitattributes").write_text("* filter=x\n")
        (repo / "file.py").write_text("x = 3\n")
        result = self.tools.inspect_git_status("repo")
        self.assertEqual(result["status"], "success")
        self.assertFalse(marker.exists(), "inspect_git_status executed code from the repository config")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_status_ignores_repository_above_workspace(self):
        # e.g. a dotfiles repository in $HOME that contains the workspace
        self._git(self.tmp, "init", "-q")
        (self.tmp / "outside_secret_plan.txt").write_text("secret")
        (self.workspace / "plain").mkdir()
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.tmp / ".git")}):
            with self.assertRaises(jarvis.ToolError) as ctx:
                self.tools.inspect_git_status("plain")
        self.assertIn("not inside a git repository", str(ctx.exception))

    def test_git_status_outside_workspace_rejected(self):
        with mock.patch("jarvis.subprocess.run") as run:
            with self.assertRaises(jarvis.ToolError):
                self.tools.inspect_git_status("../")
        run.assert_not_called()

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_status_failure(self):
        (self.workspace / "notrepo").mkdir()
        with self.assertRaises(jarvis.ToolError) as ctx:
            self.tools.inspect_git_status("notrepo")
        self.assertIn("not inside a git repository", str(ctx.exception))

    def test_open_youtube_encodes_query(self):
        result = self.tools.open_youtube("  lo-fi music & chill/beats?  ")
        expected = "https://www.youtube.com/results?search_query=lo-fi+music+%26+chill%2Fbeats%3F"
        self.assertEqual(self.browser.urls, [expected])
        self.assertEqual(result["url"], expected)
        self.assertIs(result["browser_request_issued"], True)

    def test_open_youtube_no_browser(self):
        tools = jarvis.JarvisTools(self.config, self.memory, browser_open=FakeBrowser(result=False))
        with self.assertRaises(jarvis.ToolError):
            tools.open_youtube("music")

    def test_open_url_accepts_http_and_bare_domains(self):
        self.assertEqual(self.tools.open_url("https://example.com/a?b=1")["url"], "https://example.com/a?b=1")
        self.assertEqual(self.tools.open_url("example.com")["url"], "https://example.com")
        self.assertEqual(self.browser.urls, ["https://example.com/a?b=1", "https://example.com"])

    def test_open_url_rejects_file_scheme(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "chrome://settings"):
            with self.subTest(url=url):
                with self.assertRaises(jarvis.ToolError):
                    self.tools.open_url(url)
        self.assertEqual(self.browser.urls, [])

    def test_open_url_rejects_javascript_and_data_schemes(self):
        for url in ("javascript:alert(1)", "JavaScript:alert(document.cookie)", "data:text/html,<script>x</script>"):
            with self.subTest(url=url):
                with self.assertRaises(jarvis.ToolError):
                    self.tools.open_url(url)
        self.assertEqual(self.browser.urls, [])

    def test_get_time_iso8601_with_offset(self):
        result = self.tools.get_time()
        parsed = dt.datetime.fromisoformat(result["iso8601"])
        self.assertIsNotNone(parsed.utcoffset())
        self.assertLess(abs((parsed - dt.datetime.now().astimezone()).total_seconds()), 5)
        self.assertEqual(result["weekday"], parsed.strftime("%A"))

    def test_create_note_refuses_overwrite_without_flag(self):
        first = self.tools.create_note("Shopping list", "eggs")
        path = Path(first["path"])
        self.assertEqual(path.parent, self.notes_dir.resolve())
        self.assertEqual(path.name, "Shopping-list.md")
        self.assertEqual(path.read_text(encoding="utf-8"), "# Shopping list\n\neggs\n")
        self.assertFalse(first["overwritten"])
        with self.assertRaises(jarvis.ToolError) as ctx:
            self.tools.create_note("Shopping list", "milk")
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "# Shopping list\n\neggs\n")
        # Through execute_tool in standard mode: error, no confirmation, file unchanged.
        result = jarvis.execute_tool(self.registry, self.policy, "create_note",
                                     json.dumps({"title": "Shopping list", "content": "milk"}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(path.read_text(encoding="utf-8"), "# Shopping list\n\neggs\n")
        self.assertEqual([p.name for p in path.parent.iterdir()], ["Shopping-list.md"])

    def test_create_note_title_cannot_escape_notes_dir(self):
        result = self.tools.create_note("../../etc/evil", "x")
        path = Path(result["path"])
        self.assertEqual(path.parent, self.notes_dir.resolve())
        self.assertFalse((self.tmp / "etc").exists())

    def test_open_project_launches_editor_on_workspace_dir(self):
        (self.workspace / "proj").mkdir()
        process = mock.Mock(pid=9)
        process.poll.return_value = None
        with mock.patch("jarvis.shutil.which", return_value="/usr/bin/code"), \
                mock.patch("jarvis.subprocess.Popen", return_value=process) as popen, \
                mock.patch("jarvis.time.sleep"):
            self.tools.open_project("proj")
            with self.assertRaises(jarvis.ToolError):
                self.tools.open_project("../")
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], ["/usr/bin/code", str(self.workspace / "proj")])
        self.assertIs(popen.call_args.kwargs["shell"], False)


if __name__ == "__main__":
    unittest.main()
