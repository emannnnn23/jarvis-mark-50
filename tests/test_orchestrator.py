"""The request lifecycle: agent loop, model calls, errors and helpers."""

from __future__ import annotations

import json
import unittest

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    AgentTestCase,
    FakeGroqClient,
    _request,
    make_connection_error,
    make_status_error,
    response,
    tool_call,
)
import groq  # noqa: E402
import jarvis  # noqa: E402
from core import orchestrator  # noqa: E402
from intelligence import groq_client  # noqa: E402
from tools import catalog  # noqa: E402


class AgentLoopTests(AgentTestCase):
    def test_plain_response_returns_directly(self):
        client = FakeGroqClient([response("Good evening, Sir.")])
        result = self.run_agent(client)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.text, "Good evening, Sir.")
        self.assertEqual(result.rounds, 1)
        self.assertEqual(result.tool_events, [])
        self.assertEqual(len(client.calls), 1)
        first = client.calls[0]
        self.assertEqual(first["messages"][0]["role"], "system")
        self.assertEqual(first["messages"][-1], {"role": "user", "content": "hello"})
        self.assertEqual(first["tool_choice"], "auto")
        self.assertEqual({t["function"]["name"] for t in first["tools"]}, set(self.registry.names()))

    def test_tool_call_executes_handler_and_result_message_follows(self):
        client = FakeGroqClient([
            response(None, [tool_call("call_abc", "remember_fact", {"fact": "I like green tea"})]),
            response("Noted, Sir."),
        ])
        result = self.run_agent(client, "remember I like green tea")
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.text, "Noted, Sir.")
        self.assertEqual(result.tool_events, [{"tool": "remember_fact", "status": "success"}])
        self.assertEqual([f["text"] for f in self.memory.list_facts()], ["I like green tea"])

        messages = client.calls[1]["messages"]
        assistant_index = next(i for i, m in enumerate(messages) if m.get("tool_calls"))
        tool_index = next(i for i, m in enumerate(messages) if m["role"] == "tool")
        self.assertLess(assistant_index, tool_index)
        self.assertEqual(tool_index, assistant_index + 1)
        assistant = messages[assistant_index]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_abc")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "remember_fact")
        tool_msg = messages[tool_index]
        self.assertEqual(tool_msg["tool_call_id"], "call_abc")
        self.assertEqual(tool_msg["name"], "remember_fact")
        self.assertEqual(json.loads(tool_msg["content"])["status"], "success")

    def test_multiple_tool_calls_each_get_own_result(self):
        client = FakeGroqClient([
            response("Checking.", [tool_call("call_1", "get_time", {}),
                                   tool_call("call_2", "search_memory", {"query": ""}),
                                   tool_call("call_3", "open_youtube", {"search": "lo-fi"})]),
            response("Done."),
        ])
        result = self.run_agent(client)
        self.assertEqual(result.status, "complete")
        tool_msgs = self.tool_messages(client.calls[1])
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["call_1", "call_2", "call_3"])
        self.assertEqual([m["name"] for m in tool_msgs], ["get_time", "search_memory", "open_youtube"])
        self.assertIn("iso8601", json.loads(tool_msgs[0]["content"])["result"])
        self.assertIn("facts", json.loads(tool_msgs[1]["content"])["result"])
        self.assertTrue(json.loads(tool_msgs[2]["content"])["result"]["browser_request_issued"])
        assistant = [m for m in client.calls[1]["messages"] if m.get("tool_calls")][0]
        self.assertEqual([c["id"] for c in assistant["tool_calls"]], ["call_1", "call_2", "call_3"])
        self.assertEqual(assistant["content"], "Checking.")

    def test_unknown_tool_is_fed_back_as_error(self):
        client = FakeGroqClient([
            response(None, [tool_call("call_x", "delete_everything", {"path": "/"})]),
            response("I can't do that."),
        ])
        result = self.run_agent(client)
        self.assertEqual(result.status, "complete")
        content = json.loads(self.tool_messages(client.calls[1])[0]["content"])
        self.assertEqual(content["status"], "error")
        self.assertIn("Unknown tool 'delete_everything'", content["error"])
        self.assertEqual([(e["tool"], e["status"]) for e in result.tool_events], [("delete_everything", "error")])

    def test_malformed_json_arguments_return_error(self):
        client = FakeGroqClient([
            response(None, [tool_call("call_bad", "remember_fact", '{"fact": "unterminated')]),
            response("Sorry."),
        ])
        self.run_agent(client)
        content = json.loads(self.tool_messages(client.calls[1])[0]["content"])
        self.assertEqual(content["status"], "error")
        self.assertIn("not valid JSON", content["error"])
        self.assertEqual(self.memory.list_facts(), [])

    def test_invalid_argument_types_unexpected_and_missing(self):
        cases = [
            ("remember_fact", {"fact": 123}, "must be a string"),
            ("remember_fact", {}, "Missing required argument(s): fact"),
            ("get_time", {"zone": "UTC"}, "Unexpected argument 'zone'"),
            ("search_web", {"query": "x", "max_results": "lots"}, "must be a number"),
            ("search_web", {"query": "x", "max_results": 50}, "at most 10"),
            ("remember_fact", {"fact": "ok", "priority": "urgent"}, "must be one of"),
            ("create_note", {"title": "t", "content": "c", "overwrite": "perhaps"}, "true or false"),
            ("remember_fact", '["a", "list"]', "must be a JSON object"),
        ]
        script = [response(None, [tool_call(f"c{i}", name, args)]) for i, (name, args, _) in enumerate(cases)]
        client = FakeGroqClient(script + [response("ok")])
        config = self.make_config(max_tool_rounds=len(cases) + 1)
        self.run_agent(client, registry=self.registry, config=config)
        tool_msgs = self.tool_messages(client.calls[-1])
        self.assertEqual(len(tool_msgs), len(cases))
        for msg, (name, _, expected) in zip(tool_msgs, cases):
            with self.subTest(tool=name, expected=expected):
                content = json.loads(msg["content"])
                self.assertEqual(content["status"], "error")
                self.assertIn(expected, content["error"])
        self.assertEqual(self.memory.list_facts(), [])
        self.assertFalse(self.notes_dir.exists())

    def test_handler_exception_becomes_error_result(self):
        def explode(**_):
            raise ValueError("disk on fire")

        registry = jarvis.ToolRegistry()
        registry.register(jarvis.Tool("explode", "Always fails", jarvis._object(), explode, jarvis.Permission.READ_ONLY))
        client = FakeGroqClient([response(None, [tool_call("call_e", "explode", {})]), response("It failed.")])
        result = self.run_agent(client, registry=registry)
        self.assertEqual(result.status, "complete")
        content = json.loads(self.tool_messages(client.calls[1])[0]["content"])
        self.assertEqual(content["status"], "error")
        self.assertIn("ValueError", content["error"])
        self.assertIn("disk on fire", content["error"])
        self.assertIn("explode", content["error"])

    def test_tool_error_message_is_returned(self):
        client = FakeGroqClient([
            response(None, [tool_call("call_r", "read_workspace_file", {"path": "missing.txt"})]),
            response("Not found."),
        ])
        self.run_agent(client)
        content = json.loads(self.tool_messages(client.calls[1])[0]["content"])
        self.assertEqual(content["status"], "error")
        self.assertIn("missing.txt", content["error"])

    def test_max_tool_rounds_enforced(self):
        counter = {"n": 0}

        def always_tool_call():
            counter["n"] += 1
            return response("thinking", [tool_call(f"call_{counter['n']}", "get_time", {})])

        client = FakeGroqClient(default=always_tool_call)
        config = self.make_config(max_tool_rounds=3)
        result = self.run_agent(client, config=config)
        self.assertEqual(len(client.calls), config.max_tool_rounds + 1)
        self.assertEqual(client.calls[-1]["tool_choice"], "none")
        for call in client.calls[:-1]:
            self.assertEqual(call["tool_choice"], "auto")
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.rounds, 3)
        self.assertEqual(len(result.tool_events), 3)
        final_messages = client.calls[-1]["messages"]
        self.assertEqual(final_messages[-1]["role"], "system")
        self.assertIn("tool-round limit", final_messages[-1]["content"])

    def test_max_tool_rounds_final_call_failure_has_fallback_text(self):
        auth = make_status_error(groq.AuthenticationError, 401, "invalid api key")
        script = [response(None, [tool_call("c1", "get_time", {})]), auth]
        client = FakeGroqClient(script)
        result = self.run_agent(client, config=self.make_config(max_tool_rounds=1))
        self.assertEqual(result.status, "incomplete")
        self.assertIn("limit of 1 tool rounds", result.text)
        self.assertIn("get_time: success", result.text)

    def test_identical_repeated_call_is_not_re_executed(self):
        calls = []

        def counted(**kwargs):
            calls.append(kwargs)
            return {"done": True}

        registry = jarvis.ToolRegistry()
        registry.register(jarvis.Tool("counted", "Counts", jarvis._object({"x": {"type": "integer"}}),
                                      counted, jarvis.Permission.CONTROLLED_ACTION))
        client = FakeGroqClient([
            response(None, [tool_call("a1", "counted", {"x": 1})]),
            response(None, [tool_call("a2", "counted", '{ "x" : 1 }'),
                            tool_call("a3", "counted", {"x": 2})]),
            response("Finished."),
        ])
        result = self.run_agent(client, registry=registry)
        self.assertEqual(result.status, "complete")
        self.assertEqual(calls, [{"x": 1}, {"x": 2}])
        tool_msgs = self.tool_messages(client.calls[2])
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["a1", "a2", "a3"])
        repeated = json.loads(tool_msgs[1]["content"])
        self.assertIn("not repeated", repeated["note"])
        self.assertEqual(repeated["status"], "success")

    def test_memory_context_injected_into_system_prompt(self):
        self.memory.remember_fact("My favourite coffee is a flat white")
        client = FakeGroqClient([response("A flat white, Sir.")])
        self.run_agent(client, "what coffee do I like")
        system = client.calls[0]["messages"][0]["content"]
        self.assertIn("flat white", system)
        self.assertIn(str(self.workspace), system)

    def test_api_error_propagates_from_run_agent(self):
        client = FakeGroqClient([make_status_error(groq.AuthenticationError, 401, "bad key")])
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            self.run_agent(client)
        self.assertEqual(ctx.exception.kind, "auth")


class FormatFinalResponseTests(unittest.TestCase):
    def test_strips_leaked_markup(self):
        text = ('<thinking>secret plan\nmore</thinking>Here you are, Sir. '
                '<function=search_web>{"query": "x"}</function> <|eot_id|>')
        self.assertEqual(jarvis.format_final_response(text), "Here you are, Sir.")

    def test_strips_unterminated_function_markup(self):
        self.assertEqual(jarvis.format_final_response('Sure. <function=get_time>{}'), "Sure.")

    def test_empty_reply_gets_fallback(self):
        for value in (None, "", "   ", "<thinking>only this</thinking>"):
            with self.subTest(value=value):
                self.assertIn("empty reply", jarvis.format_final_response(value))


class CallModelTests(unittest.TestCase):
    def test_retries_transient_errors_with_backoff(self):
        ok = response("ok")
        client = FakeGroqClient([
            make_status_error(groq.RateLimitError, 429, "Rate limit reached"),
            make_connection_error(),
            make_status_error(groq.InternalServerError, 500, "server error"),
            ok,
        ])
        sleeps: list[float] = []
        result = jarvis.call_model(client, "m", [{"role": "user", "content": "hi"}], sleep=sleeps.append)
        self.assertIs(result, ok)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(sleeps, [1, 2, 4])

    def test_retry_after_header_is_honoured(self):
        client = FakeGroqClient([
            make_status_error(groq.RateLimitError, 429, "slow down", headers={"retry-after": "3"}),
            response("ok"),
        ])
        sleeps: list[float] = []
        jarvis.call_model(client, "m", [], sleep=sleeps.append)
        self.assertEqual(sleeps, [3.0])

    def test_gives_up_after_max_retries(self):
        client = FakeGroqClient([make_connection_error() for _ in range(5)])
        sleeps: list[float] = []
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            jarvis.call_model(client, "m", [], max_retries=2, sleep=sleeps.append)
        self.assertEqual(ctx.exception.kind, "network")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(sleeps, [1, 2])

    def test_auth_error_not_retried(self):
        client = FakeGroqClient([make_status_error(groq.AuthenticationError, 401, "Invalid API Key"), response("x")])
        sleeps: list[float] = []
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            jarvis.call_model(client, "m", [], sleep=sleeps.append)
        self.assertEqual(ctx.exception.kind, "auth")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(sleeps, [])

    def test_quota_error_not_retried(self):
        body = {"error": {"message": "Rate limit reached on tokens per day (TPD): Limit 100000",
                          "code": "rate_limit_exceeded"}}
        client = FakeGroqClient([make_status_error(groq.RateLimitError, 429, "limit", body=body), response("x")])
        sleeps: list[float] = []
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            jarvis.call_model(client, "m", [], sleep=sleeps.append)
        self.assertEqual(ctx.exception.kind, "quota")
        self.assertEqual(len(client.calls), 1)

    def test_tool_use_failed_retried_once_at_lower_temperature(self):
        body = {"error": {"message": "Failed to call a function", "code": "tool_use_failed"}}
        client = FakeGroqClient([make_status_error(groq.BadRequestError, 400, "bad", body=body), response("ok")])
        sleeps: list[float] = []
        tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
        jarvis.call_model(client, "m", [], tools, sleep=sleeps.append)
        self.assertEqual([c["temperature"] for c in client.calls], [0.4, 0.1])
        self.assertEqual(sleeps, [])

    def test_tools_and_tool_choice_passed_only_with_tools(self):
        client = FakeGroqClient([response("a"), response("b")])
        jarvis.call_model(client, "m", [], None)
        self.assertNotIn("tools", client.calls[0])
        self.assertNotIn("tool_choice", client.calls[0])
        jarvis.call_model(client, "m", [], [{"type": "function"}], tool_choice="none")
        self.assertEqual(client.calls[1]["tool_choice"], "none")


class ClassifyApiErrorTests(unittest.TestCase):
    def test_auth(self):
        error = jarvis.classify_api_error(make_status_error(groq.AuthenticationError, 401, "Invalid API Key"))
        self.assertEqual(error.kind, "auth")
        self.assertFalse(error.retryable)
        self.assertIn("API key", str(error))

    def test_rate_limit(self):
        error = jarvis.classify_api_error(make_status_error(groq.RateLimitError, 429, "Rate limit reached",
                                                           body={"error": {"message": "requests per minute"}}))
        self.assertEqual(error.kind, "rate_limit")
        self.assertTrue(error.retryable)

    def test_quota_from_body(self):
        body = {"error": {"message": "Rate limit reached for model on tokens per day (TPD)",
                          "type": "tokens", "code": "rate_limit_exceeded"}}
        error = jarvis.classify_api_error(make_status_error(groq.RateLimitError, 429, "429", body=body))
        self.assertEqual(error.kind, "quota")
        self.assertFalse(error.retryable)

    def test_model_not_found(self):
        error = jarvis.classify_api_error(make_status_error(groq.NotFoundError, 404, "not found"), "llama-9")
        self.assertEqual(error.kind, "model")
        self.assertIn("llama-9", str(error))
        body = {"error": {"message": "The model `x` does not exist", "code": "model_not_found"}}
        error = jarvis.classify_api_error(make_status_error(groq.BadRequestError, 400, "bad", body=body), "x")
        self.assertEqual(error.kind, "model")
        body = {"error": {"message": "The model has been decommissioned", "code": "model_decommissioned"}}
        error = jarvis.classify_api_error(make_status_error(groq.BadRequestError, 400, "bad", body=body), "x")
        self.assertEqual(error.kind, "model")

    def test_network_timeout_server_and_unknown(self):
        self.assertEqual(jarvis.classify_api_error(make_connection_error()).kind, "network")
        self.assertEqual(jarvis.classify_api_error(groq.APITimeoutError(request=_request())).kind, "timeout")
        server = jarvis.classify_api_error(make_status_error(groq.InternalServerError, 503, "unavailable"))
        self.assertEqual(server.kind, "server")
        self.assertTrue(server.retryable)
        unknown = jarvis.classify_api_error(RuntimeError("weird " + "gsk_" + "x" * 30))
        self.assertEqual(unknown.kind, "unknown")
        self.assertNotIn("gsk_" + "x" * 30, str(unknown))

    def test_verify_model(self):
        client = FakeGroqClient(models=("llama-3.3-70b-versatile", "qwen/qwen3-32b"))
        jarvis.verify_model(client, "llama-3.3-70b-versatile")
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            jarvis.verify_model(client, "missing-model")
        self.assertEqual(ctx.exception.kind, "model")
        self.assertIn("qwen/qwen3-32b", str(ctx.exception))


class HelperTests(unittest.TestCase):
    def test_serialize_tool_result_truncates(self):
        big = {"status": "success", "result": "x" * 10_000}
        text = jarvis.serialize_tool_result(big, max_chars=1000)
        self.assertLessEqual(len(text), 1100)
        decoded = json.loads(text)
        self.assertTrue(decoded["truncated"])
        self.assertEqual(decoded["status"], "success")

    def test_parse_arguments(self):
        self.assertEqual(jarvis.parse_arguments(None), {})
        self.assertEqual(jarvis.parse_arguments(""), {})
        self.assertEqual(jarvis.parse_arguments("null"), {})
        self.assertEqual(jarvis.parse_arguments({"a": 1}), {"a": 1})
        with self.assertRaises(jarvis.ToolValidationError):
            jarvis.parse_arguments("[1]")
        with self.assertRaises(jarvis.ToolValidationError):
            jarvis.parse_arguments(42)

    def test_conversation_trims_and_summarises(self):
        conversation = jarvis.Conversation(max_turns=2)
        for i in range(4):
            conversation.add(f"q{i}", f"a{i}")
        self.assertEqual([m["content"] for m in conversation.messages()], ["q2", "a2", "q3", "a3"])
        self.assertIn("q0", conversation.summary)


class DeveloperModeAgentTests(AgentTestCase):
    """tool_groups, the developer round limit, the result cache around state changes, and progress reports."""

    def make_config(self, **overrides):
        overrides.setdefault("backup_dir", self.tmp / "backups")
        return super().make_config(**overrides)

    def setUp(self):
        super().setUp()
        self.notes = self.workspace / "notes.txt"
        self.notes.write_text("alpha\n")

    def run_groups(self, client, groups, config=None, on_tool=None):
        ctx = jarvis.AgentContext(client, config or self.config, self.registry, self.policy, self.memory)
        return jarvis.run_agent(ctx, "work on my project", on_tool=on_tool, tool_groups=groups)

    @staticmethod
    def offered(call_kwargs) -> set[str]:
        return {schema["function"]["name"] for schema in call_kwargs.get("tools", [])}

    def tool_contents(self, call_kwargs) -> list[dict]:
        return [json.loads(m["content"]) for m in self.tool_messages(call_kwargs)]

    def test_tool_groups_limit_the_schemas_sent(self):
        everything = set(self.registry.names())
        for groups, expected in (({"core"}, everything - catalog.DEVELOPER_TOOLS),
                                 ({"core", "developer"}, everything), (None, everything),
                                 ({"developer"}, set(catalog.DEVELOPER_TOOLS))):
            with self.subTest(groups=groups):
                client = FakeGroqClient([response("ok")])
                self.run_groups(client, groups)
                self.assertEqual(self.offered(client.calls[0]), expected)

    def test_registered_tool_that_was_not_offered_is_refused_without_running(self):
        seen = []
        client = FakeGroqClient([
            response(None, [tool_call("e1", "edit_workspace_file",
                                      {"path": "notes.txt", "old_text": "alpha", "new_text": "beta"})]),
            response("Done."),
        ])
        result = self.run_groups(client, {"core"}, on_tool=seen.append)
        [content] = self.tool_contents(client.calls[1])
        self.assertEqual(content["status"], "error")
        self.assertIn("edit_workspace_file is not available right now", content["error"])
        self.assertIn("/dev on", content["error"])
        self.assertEqual(self.notes.read_text(), "alpha\n")
        self.assertEqual(self.confirm.calls, [])
        self.assertEqual(seen, [])
        self.assertEqual([(e["tool"], e["status"]) for e in result.tool_events], [("edit_workspace_file", "error")])

    def test_developer_round_limit_only_with_the_developer_group(self):
        config = self.make_config(max_tool_rounds=1, max_dev_tool_rounds=3)
        for groups, rounds in (({"core", "developer"}, 3), ({"core"}, 1), (None, 1)):
            with self.subTest(groups=groups):
                client = FakeGroqClient(default=lambda: response(None, [tool_call("t", "get_time", {})]))
                result = self.run_groups(client, groups, config=config)
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.rounds, rounds)
                self.assertEqual(len(client.calls), rounds + 1)  # plus the closing summary with tools off
                self.assertIn(f"tool-round limit ({rounds})", client.calls[-1]["messages"][-1]["content"])

    def test_identical_read_is_cached_until_something_changes(self):
        read = tool_call("r", "read_workspace_file", {"path": "notes.txt"})
        edit = tool_call("e", "edit_workspace_file", {"path": "notes.txt", "old_text": "alpha", "new_text": "beta"})
        client = FakeGroqClient([response(None, [read]), response(None, [read]), response(None, [edit]),
                                 response(None, [read]), response("Done.")])
        result = self.run_groups(client, {"core", "developer"})
        first, cached, edited, fresh = self.tool_contents(client.calls[4])
        self.assertNotIn("note", first)
        self.assertIn("not repeated", cached["note"])
        self.assertEqual(edited["status"], "success")
        self.assertNotIn("note", fresh)  # re-run after the edit, not served from the cache
        self.assertIn("beta", json.dumps(fresh))
        self.assertNotIn("alpha", json.dumps(fresh))
        self.assertEqual(result.status, "complete")

    def test_identical_edit_is_never_applied_twice(self):
        edit = tool_call("e", "edit_workspace_file",
                         {"path": "notes.txt", "old_text": "alpha", "new_text": "alpha beta"})
        client = FakeGroqClient([response(None, [edit]), response(None, [edit]), response("Done.")])
        self.run_groups(client, {"core", "developer"})
        self.assertEqual(self.notes.read_text(), "alpha beta\n")
        self.assertEqual(len(self.confirm.calls), 1)
        second = self.tool_contents(client.calls[2])[1]
        self.assertIn("not repeated", second["note"])

    def test_declined_change_does_not_clear_the_cache(self):
        self.confirm.answer = False
        read = tool_call("r", "read_workspace_file", {"path": "notes.txt"})
        edit = tool_call("e", "edit_workspace_file", {"path": "notes.txt", "old_text": "alpha", "new_text": "beta"})
        client = FakeGroqClient([response(None, [read, edit, read]), response("Done.")])
        self.run_groups(client, {"core", "developer"})
        _, declined, again = self.tool_contents(client.calls[1])
        self.assertEqual(declined["status"], "denied")
        self.assertIn("not repeated", again["note"])

    def test_tests_are_re_run_after_an_edit(self):
        runs = []
        registry = jarvis.ToolRegistry()
        registry.register(jarvis.Tool("run_tests", "Runs tests", jarvis._object({}),
                                      lambda: runs.append(1) or {"passed": len(runs) > 1},
                                      jarvis.Permission.CONTROLLED_ACTION))
        registry.register(jarvis.Tool("change", "Changes a file", jarvis._object({}), lambda: {"ok": True},
                                      jarvis.Permission.CONTROLLED_ACTION))
        client = FakeGroqClient([response(None, [tool_call("t1", "run_tests", {})]),
                                 response(None, [tool_call("c", "change", {})]),
                                 response(None, [tool_call("t2", "run_tests", {})]),
                                 response("Fixed.")])
        result = self.run_agent(client, registry=registry)
        self.assertEqual(len(runs), 2)
        self.assertEqual([e.get("passed") for e in result.tool_events], [False, None, True])

    def test_changes_state(self):
        get = self.registry.get
        self.assertFalse(orchestrator._changes_state(None, "{}"))
        self.assertFalse(orchestrator._changes_state(get("read_workspace_file"), {"path": "x"}))
        self.assertTrue(orchestrator._changes_state(get("edit_workspace_file"), "{}"))
        self.assertTrue(orchestrator._changes_state(get("create_note"), {"title": "t", "content": "c"}))

        def unclassifiable(args):
            raise jarvis.ToolError("cannot tell")

        tool = jarvis.Tool("odd", "d", jarvis._object({}), lambda: None, unclassifiable)
        self.assertTrue(orchestrator._changes_state(tool, "{}"))
        self.assertTrue(orchestrator._changes_state(get("read_workspace_file"), "[not json"))

    def test_event_records_target_and_test_verdict(self):
        event = orchestrator._event
        self.assertEqual(event("edit_workspace_file", '{"path": "a/b.py", "old_text": "x"}', {"status": "success"}),
                         {"tool": "edit_workspace_file", "status": "success", "target": "a/b.py"})
        self.assertEqual(event("run_project_tests", {"project": "shop"},
                               {"status": "success", "result": {"passed": False, "exit_code": 1}}),
                         {"tool": "run_project_tests", "status": "success", "target": "shop", "passed": False})
        self.assertEqual(event("open_url", {"url": "https://x.example/" + "p" * 300}, {"status": "denied"})["target"],
                         ("https://x.example/" + "p" * 300)[:120])
        self.assertEqual(event("remember", {"name": "Ann"}, {"status": "success", "result": "ok"})["target"], "Ann")
        self.assertEqual(event("get_time", "not json", {"status": "error", "error": "bad"}),
                         {"tool": "get_time", "status": "error"})
        self.assertEqual(event("x", {"path": 7}, {"status": "error", "passed": True}),
                         {"tool": "x", "status": "error", "passed": True})

    def test_progress_report(self):
        report = orchestrator.progress_report
        self.assertEqual(report([]), "Nothing had been done yet.")
        text = report([{"tool": "read_workspace_file", "status": "success", "target": "a.py"},
                       {"tool": "run_project_tests", "status": "success", "target": "shop", "passed": False},
                       {"tool": "get_time", "status": "error"}])
        self.assertEqual(text, "Steps already done: read_workspace_file on a.py: success; "
                               "run_project_tests on shop: success (tests failed); get_time: error.")
        changed = report([{"tool": "edit_workspace_file", "status": "success", "target": "a.py"},
                          {"tool": "run_project_tests", "status": "success", "passed": True}])
        self.assertIn("run_project_tests: success (tests passed).", changed)
        self.assertIn("Files were changed", changed)
        for tool in ("create_workspace_file", "create_note"):
            self.assertIn("Files were changed", report([{"tool": tool, "status": "success"}]))
        self.assertNotIn("Files were changed", report([{"tool": "edit_workspace_file", "status": "denied"}]))

    def test_api_failure_after_tools_ran_returns_an_incomplete_result(self):
        edit = tool_call("e", "edit_workspace_file", {"path": "notes.txt", "old_text": "alpha", "new_text": "beta"})
        client = FakeGroqClient([response(None, [edit]),
                                 make_status_error(groq.AuthenticationError, 401, "bad key")])
        result = self.run_groups(client, {"core", "developer"})
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.rounds, 2)
        self.assertTrue(result.text.startswith("I couldn't finish: Groq rejected the API key"))
        self.assertIn("edit_workspace_file on notes.txt: success", result.text)
        self.assertIn("Files were changed", result.text)
        self.assertEqual(self.notes.read_text(), "beta\n")

    def test_api_failure_before_any_tool_ran_still_raises(self):
        for script in ([make_status_error(groq.AuthenticationError, 401, "bad key")],):
            client = FakeGroqClient(script)
            with self.assertRaises(jarvis.JarvisAPIError):
                self.run_groups(client, {"core", "developer"})


class ToolValidationFlagTests(unittest.TestCase):
    TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]

    def setUp(self):
        groq_client._tool_validation_flag_supported = True
        self.addCleanup(setattr, groq_client, "_tool_validation_flag_supported", True)

    @staticmethod
    def flag_error(message="property 'disable_tool_validation' is unsupported"):
        return make_status_error(groq.BadRequestError, 400, "bad", body={"error": {"message": message}})

    def test_flag_sent_only_with_tools(self):
        client = FakeGroqClient([response("a"), response("b")])
        jarvis.call_model(client, "m", [], self.TOOLS)
        jarvis.call_model(client, "m", [], None)
        self.assertIs(client.calls[0]["disable_tool_validation"], True)
        self.assertNotIn("disable_tool_validation", client.calls[1])

    def test_rejected_flag_is_dropped_and_remembered(self):
        sleeps = []
        client = FakeGroqClient([self.flag_error(), response("ok"), response("later")])
        self.assertEqual(jarvis.call_model(client, "m", [], self.TOOLS, sleep=sleeps.append).choices[0].message.content,
                         "ok")
        self.assertIn("disable_tool_validation", client.calls[0])
        self.assertNotIn("disable_tool_validation", client.calls[1])
        self.assertEqual(sleeps, [])
        self.assertFalse(groq_client._tool_validation_flag_supported)
        jarvis.call_model(client, "m", [], self.TOOLS)
        self.assertNotIn("disable_tool_validation", client.calls[2])

    def test_other_bad_requests_are_not_retried(self):
        client = FakeGroqClient([self.flag_error("messages: field required"), response("x")])
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            jarvis.call_model(client, "m", [], self.TOOLS, sleep=lambda s: None)
        self.assertEqual(ctx.exception.kind, "bad_request")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(groq_client._tool_validation_flag_supported)

    def test_flag_error_without_the_flag_is_raised_not_looped(self):
        groq_client._tool_validation_flag_supported = False
        client = FakeGroqClient([self.flag_error(), response("x")])
        with self.assertRaises(jarvis.JarvisAPIError):
            jarvis.call_model(client, "m", [], self.TOOLS, sleep=lambda s: None)
        self.assertEqual(len(client.calls), 1)

    def test_retry_waits_up_to_max_retry_wait(self):
        self.assertEqual(groq_client.MAX_RETRY_WAIT, 60)
        for seconds, expected in (("45", [45.0]), ("60", [60.0])):
            with self.subTest(retry_after=seconds):
                client = FakeGroqClient([make_status_error(groq.RateLimitError, 429, "slow down",
                                                           headers={"retry-after": seconds}), response("ok")])
                sleeps = []
                jarvis.call_model(client, "m", [], sleep=sleeps.append)
                self.assertEqual(sleeps, expected)
        client = FakeGroqClient([make_status_error(groq.RateLimitError, 429, "slow down",
                                                   headers={"retry-after": "61"}), response("ok")])
        sleeps = []
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            jarvis.call_model(client, "m", [], sleep=sleeps.append)
        self.assertEqual(ctx.exception.kind, "rate_limit")
        self.assertEqual(sleeps, [])
        self.assertEqual(len(client.calls), 1)


class ToolGroupRegistryTests(unittest.TestCase):
    def test_group_defaults_to_core_and_schemas_filter_by_group(self):
        registry = jarvis.ToolRegistry()
        plain = jarvis.Tool("plain", "d", jarvis._object({}), lambda: None, jarvis.Permission.READ_ONLY)
        dev = jarvis.Tool("dev", "d", jarvis._object({}), lambda: None, jarvis.Permission.READ_ONLY, group="developer")
        self.assertEqual(plain.group, "core")
        registry.register(plain)
        registry.register(dev)
        names = lambda schemas: [s["function"]["name"] for s in schemas]  # noqa: E731
        self.assertEqual(names(registry.schemas()), ["plain", "dev"])
        self.assertEqual(names(registry.schemas(None)), ["plain", "dev"])
        self.assertEqual(names(registry.schemas({"core"})), ["plain"])
        self.assertEqual(names(registry.schemas({"developer"})), ["dev"])
        self.assertEqual(names(registry.schemas({"core", "developer"})), ["plain", "dev"])
        self.assertEqual(registry.schemas(set()), [])


if __name__ == "__main__":
    unittest.main()
