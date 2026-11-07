"""The JSON task store: durable multi-step tasks with checkpoints."""

from __future__ import annotations

import json
import os
import stat
import unittest
from unittest import mock

from helpers import FAKE_KEY, TempDirTestCase  # noqa: E402 - sets up sys.path for the project imports
from memory import task_store  # noqa: E402
from memory.task_store import (  # noqa: E402
    MAX_ACTIONS, MAX_TASKS, TaskStore, TaskStoreError,
)


def edit(target: str, status: str = "success") -> dict:
    return {"tool": "edit_workspace_file", "status": status, "target": target}


def project_tests(passed: bool, project: str = "proj", check: str | None = None) -> dict:
    event = {"tool": "run_project_tests", "status": "success", "target": project, "project": project, "passed": passed}
    if check is not None:
        event["check"] = check
    return event


class TaskStoreTestCase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "tasks" / "jarvis_tasks.json"
        self.store = self.make_store()

    def make_store(self) -> TaskStore:
        store = TaskStore(self.path)
        store.load()
        return store


class PersistenceTests(TaskStoreTestCase):
    def test_missing_file_is_empty_and_writable(self):
        self.assertEqual(self.store.data, {"tasks": []})
        self.assertIsNone(self.store.corruption_error)
        self.assertIsNone(self.store.active())
        self.assertEqual(self.store.unfinished(), [])
        self.assertFalse(self.path.exists())

    def test_task_record_shape_and_persistence_across_instances(self):
        task = self.store.start("Fix the parser", steps=["Read code", "Edit"], request="please fix it")
        self.assertRegex(task["id"], r"^t[0-9a-f]{6}$")
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["goal"], "Fix the parser")
        self.assertEqual(task["request"], "please fix it")
        self.assertIsNone(task["paused_reason"])
        self.assertIsNone(task["last_error"])
        self.assertIsNone(task["summary"])
        self.assertEqual(task["actions"], [])
        self.assertEqual(task["files_changed"], [])
        self.assertEqual([s["text"] for s in task["steps"]], ["Read code", "Edit"])
        self.assertEqual(task["steps"][0]["status"], "pending")
        self.assertIsNone(task["steps"][0]["evidence"])
        self.assertRegex(task["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

        fresh = self.make_store()
        self.assertEqual(fresh.get(task["id"]), task)
        self.assertEqual(fresh.active()["id"], task["id"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["tasks"][0]["id"], task["id"])

    def test_returned_dicts_are_copies(self):
        task = self.store.start("Goal")
        task["goal"] = "tampered"
        self.store.get(task["id"])["steps"].append("x")
        self.assertEqual(self.store.get(task["id"])["goal"], "Goal")
        self.assertEqual(self.store.get(task["id"])["steps"], [])

    def test_ids_are_unique(self):
        ids = {self.store.start(f"goal {i}")["id"] for i in range(30)}
        self.assertEqual(len(ids), 30)

    def test_atomic_write_leaves_no_temp_files_and_is_private(self):
        self.store.start("Goal")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["jarvis_tasks.json"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_failed_write_keeps_old_file_cleans_up_and_rolls_back_memory(self):
        task = self.store.start("Keep me")
        before = self.path.read_bytes()
        with mock.patch.object(task_store.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(TaskStoreError):
                self.store.set_plan(task["id"], ["a step"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["jarvis_tasks.json"])
        self.assertEqual(self.store.get(task["id"])["steps"], [])

    def test_empty_file_loads_as_fresh(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("  \n", encoding="utf-8")
        store = self.make_store()
        self.assertIsNone(store.corruption_error)
        self.assertEqual(store.unfinished(), [])

    def test_unusable_task_entries_are_dropped_and_gaps_filled(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"tasks": ["junk", {"goal": "no id"}, {"id": "tabc123", "goal": "G"}]}),
                             encoding="utf-8")
        store = self.make_store()
        task = store.get("tabc123")
        self.assertEqual([t["id"] for t in store.recent()], ["tabc123"])
        self.assertEqual(task["steps"], [])
        self.assertEqual(task["files_changed"], [])
        self.assertIsNone(task["last_error"])


class CorruptionTests(TaskStoreTestCase):
    def corrupt(self, content: bytes = b'{"tasks": [ {"id": "t1", "goal": "half written') -> bytes:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(content)
        return content

    def test_corrupted_file_is_untouched_and_save_is_refused(self):
        original = self.corrupt()
        store = self.make_store()
        self.assertIn("not valid JSON", store.corruption_error)
        self.assertFalse(store.writable)
        with self.assertRaises(TaskStoreError) as caught:
            store.start("New goal")
        self.assertIn("won't overwrite", str(caught.exception))
        self.assertIn("/tasks reset", str(caught.exception))
        with self.assertRaises(TaskStoreError):
            store.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(store.unfinished(), [])  # the failed start was rolled back in memory

    def test_wrong_shape_counts_as_corrupted(self):
        for content in (b"[1, 2]", b'{"tasks": "nope"}'):
            with self.subTest(content=content):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(content)
                store = self.make_store()
                self.assertIsNotNone(store.corruption_error)
                self.assertEqual(self.path.read_bytes(), content)

    def test_reset_corrupted_moves_file_aside_and_starts_fresh(self):
        original = self.corrupt()
        store = self.make_store()
        backup = store.reset_corrupted()
        self.assertRegex(backup.name, r"^jarvis_tasks\.corrupt-\d{8}-\d{6}\.json$")
        self.assertEqual(backup.read_bytes(), original)
        self.assertIsNone(store.corruption_error)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"tasks": []})
        task = store.start("Works again")
        self.assertEqual(self.make_store().get(task["id"])["goal"], "Works again")

    def test_two_resets_in_the_same_second_keep_both_backups(self):
        self.corrupt(b"{first")
        store = self.make_store()
        first = store.reset_corrupted()
        self.path.write_bytes(b"{second")
        store.load()
        second = store.reset_corrupted()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"{first")
        self.assertEqual(second.read_bytes(), b"{second")


    def test_a_non_utf8_file_is_treated_as_corrupted_and_left_untouched(self):
        original = self.corrupt(b'{"tasks": [\xff\xfe]}')
        store = self.make_store()
        self.assertIn("not valid UTF-8", store.corruption_error)
        self.assertFalse(store.writable)
        with self.assertRaises(TaskStoreError):
            store.start("New goal")
        self.assertEqual(self.path.read_bytes(), original)

    def test_an_unreadable_file_is_treated_as_corrupted_and_never_raises(self):
        original = self.corrupt(b'{"tasks": []}')
        with mock.patch.object(task_store.Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            store = self.make_store()
        self.assertIn("Cannot read task file", store.corruption_error)
        self.assertIn("Permission denied", store.corruption_error)
        with self.assertRaises(TaskStoreError):
            store.save()
        self.assertEqual(self.path.read_bytes(), original)

    def test_a_directory_in_place_of_the_file_is_treated_as_corrupted(self):
        self.path.mkdir(parents=True)
        store = self.make_store()
        self.assertIn("Cannot read task file", store.corruption_error)
        self.assertTrue(self.path.is_dir())

    def test_a_file_nested_too_deeply_is_treated_as_corrupted(self):
        original = self.corrupt(b'{"tasks": ' + b"[" * 200000 + b"]" * 200000 + b"}")
        store = self.make_store()
        self.assertIsNotNone(store.corruption_error)
        self.assertEqual(self.path.read_bytes(), original)

    def test_any_other_load_failure_is_treated_as_corrupted(self):
        original = self.corrupt(b'{"tasks": [{"id": "t1", "goal": "g"}]}')
        with mock.patch.object(task_store, "_normalize_task", side_effect=RuntimeError("surprise")):
            store = self.make_store()
        self.assertIn("could not be loaded (RuntimeError)", store.corruption_error)
        self.assertEqual(self.path.read_bytes(), original)


class RepairTests(TaskStoreTestCase):
    """A hand-edited or damaged task file with wrong types loads, and nothing that reads it fails."""

    def load_tasks(self, tasks: list) -> TaskStore:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
        return self.make_store()

    def assert_usable(self, store: TaskStore) -> None:
        for task in store.unfinished() + store.recent(50):
            task_store.count_done_steps(task)
            store.summary_for_prompt(task["id"])
            store.verification_gap(task["id"])
        store.active()

    def test_each_probe_case_loads_and_stays_usable(self):
        cases = {
            "step is a string": [{"id": "t1", "goal": "g", "status": "paused", "steps": ["a"]}],
            "updated_at is a number": [{"id": "t1", "goal": "g", "updated_at": 5}, {"id": "t2", "goal": "h"}],
            "goal is null": [{"id": "t1", "goal": None, "status": "paused"}],
        }
        for name, tasks in cases.items():
            with self.subTest(name):
                store = self.load_tasks(tasks)
                self.assertIsNone(store.corruption_error)
                self.assert_usable(store)

    def test_fields_are_repaired_to_their_types(self):
        store = self.load_tasks([{
            "id": "t1", "goal": 42, "status": "running", "created_at": [], "updated_at": 5,
            "paused_reason": {"x": 1}, "request": None, "last_error": 7, "summary": False,
            "untrusted_plan": "yes", "files_changed": ["a.py", 3, None],
            "steps": ["a", {"text": "Read", "status": "weird", "evidence": 5, "updated_at": 1}, {"status": "done"},
                     {"text": "Edit", "status": "done", "evidence": "edited"}],
            "actions": ["junk", {"tool": None, "status": 3, "target": ["x"], "passed": 1, "check": 5},
                       {"tool": "get_time", "status": "success", "at": "2026-11-01T10:00:00+00:00"}],
        }, "not a task", {"id": "t2", "goal": "fine", "steps": "not a list", "actions": {}, "files_changed": "a.py"}])
        task = store.get("t1")
        self.assertEqual((task["goal"], task["status"], task["request"]), ("42", "paused", ""))
        self.assertEqual((task["created_at"], task["updated_at"]), (None, None))
        self.assertEqual((task["paused_reason"], task["last_error"], task["summary"]), (None, "7", None))
        self.assertIs(task["untrusted_plan"], True)
        self.assertEqual(task["files_changed"], ["a.py"])
        self.assertEqual([(s["text"], s["status"], s["evidence"], s["updated_at"]) for s in task["steps"]],
                         [("Read", "pending", "5", None), ("Edit", "done", "edited", task["steps"][1]["updated_at"])])
        self.assertEqual(task["actions"][0], {"tool": "", "status": "3", "passed": True, "check": "5"})
        self.assertEqual(task["actions"][1]["tool"], "get_time")
        other = store.get("t2")
        self.assertEqual((other["steps"], other["actions"], other["files_changed"]), ([], [], []))
        self.assertEqual([t["id"] for t in store.recent()], ["t2", "t1"])  # a task with no time sorts last
        self.assert_usable(store)

    def test_tasks_without_a_usable_time_can_still_be_trimmed(self):
        tasks = [{"id": f"t{i}", "goal": "old", "status": "done", "updated_at": None} for i in range(MAX_TASKS)]
        store = self.load_tasks(tasks)
        store.start("one more")
        self.assertEqual(len(store.recent(1000)), MAX_TASKS)

    def test_the_original_bytes_are_untouched_by_loading(self):
        self.load_tasks([{"id": "t1", "goal": None, "steps": ["a"], "updated_at": 5}])
        before = self.path.read_bytes()
        self.make_store()
        self.assertEqual(self.path.read_bytes(), before)



class LifecycleTests(TaskStoreTestCase):
    def test_start_pauses_the_previous_active_task(self):
        first = self.store.start("First")
        second = self.store.start("Second")
        self.assertEqual(self.store.active()["id"], second["id"])
        paused = self.store.get(first["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["paused_reason"], "another task started")
        self.assertEqual({t["status"] for t in self.store.unfinished()}, {"active", "paused"})

    def test_start_requires_a_goal(self):
        for goal in ("", "   \n"):
            with self.assertRaises(TaskStoreError):
                self.store.start(goal)
        self.assertEqual(self.store.recent(), [])

    def test_pause_and_resume(self):
        first = self.store.start("First")
        paused = self.store.pause(first["id"], "rate limit")
        self.assertEqual((paused["status"], paused["paused_reason"]), ("paused", "rate limit"))
        self.assertIsNone(self.store.active())
        second = self.store.start("Second")
        resumed = self.store.resume(first["id"])
        self.assertEqual((resumed["status"], resumed["paused_reason"]), ("active", None))
        self.assertEqual(self.store.active()["id"], first["id"])
        self.assertEqual(self.store.get(second["id"])["status"], "paused")

    def test_finish_abandon_and_forget(self):
        done, gone, deleted = (self.store.start(g) for g in ("a", "b", "c"))
        finished = self.store.finish(done["id"], "All good")
        self.assertEqual((finished["status"], finished["summary"]), ("done", "All good"))
        self.assertEqual(self.store.abandon(gone["id"])["status"], "abandoned")
        self.assertEqual(self.store.forget(deleted["id"])["id"], deleted["id"])
        self.assertIsNone(self.store.get(deleted["id"]))
        self.assertEqual(self.store.unfinished(), [])
        self.assertEqual({t["id"] for t in self.make_store().recent()}, {done["id"], gone["id"]})

    def test_finished_tasks_cannot_be_resumed_paused_or_finished_again(self):
        task = self.store.start("Goal")
        self.store.finish(task["id"], "done")
        for call in (lambda: self.store.resume(task["id"]), lambda: self.store.pause(task["id"], "x"),
                     lambda: self.store.finish(task["id"], "again"), lambda: self.store.abandon(task["id"])):
            with self.assertRaises(TaskStoreError):
                call()
        self.assertEqual(self.store.get(task["id"])["status"], "done")

    def test_unknown_id_raises_for_every_task_method(self):
        calls = [
            lambda: self.store.set_plan("tnope00", ["x"]), lambda: self.store.set_step("tnope00", 1, "done", "e"),
            lambda: self.store.record_action("tnope00", edit("a.py")), lambda: self.store.record_error("tnope00", "e"),
            lambda: self.store.pause("tnope00", "r"), lambda: self.store.resume("tnope00"),
            lambda: self.store.finish("tnope00", "s"), lambda: self.store.abandon("tnope00"),
            lambda: self.store.forget("tnope00"), lambda: self.store.verification_gap("tnope00"),
            lambda: self.store.summary_for_prompt("tnope00"),
        ]
        for call in calls:
            with self.assertRaises(TaskStoreError):
                call()
        self.assertIsNone(self.store.get("tnope00"))

    def test_unfinished_and_recent_order_newest_first(self):
        times = iter(f"2026-11-06T10:00:{n:02d}+00:00" for n in range(1, 40))
        with mock.patch.object(task_store, "_now_iso", side_effect=lambda: next(times)):
            a, b, c = (self.store.start(g) for g in ("a", "b", "c"))  # starting b pauses a, c pauses b
            self.store.record_error(a["id"], "touched last")
            self.store.abandon(c["id"])
        self.assertEqual([t["id"] for t in self.store.unfinished()], [a["id"], b["id"]])
        self.assertEqual([t["id"] for t in self.store.recent()], [c["id"], a["id"], b["id"]])
        self.assertEqual([t["id"] for t in self.store.recent(limit=2)], [c["id"], a["id"]])


    def test_pause_can_keep_the_last_activity_time(self):
        task = self.store.start("Crashed job")
        self.store.data["tasks"][0]["updated_at"] = "2026-11-01T10:00:00+00:00"
        kept = self.store.pause(task["id"], "JARVIS was closed before it finished", keep_time=True)
        self.assertEqual((kept["status"], kept["updated_at"]), ("paused", "2026-11-01T10:00:00+00:00"))
        self.assertEqual(self.make_store().get(task["id"])["updated_at"], "2026-11-01T10:00:00+00:00")
        self.store.resume(task["id"])
        self.assertNotEqual(self.store.pause(task["id"], "later")["updated_at"], "2026-11-01T10:00:00+00:00")

    def test_an_abandoned_task_can_be_reopened_only_when_asked_and_a_done_one_never(self):
        dropped = self.store.start("Dropped")
        self.store.abandon(dropped["id"])
        with self.assertRaises(TaskStoreError):
            self.store.resume(dropped["id"])
        other = self.store.start("Other")
        reopened = self.store.resume(dropped["id"], reopen_abandoned=True)
        self.assertEqual(reopened["status"], "active")
        self.assertEqual(self.store.get(other["id"])["status"], "paused")
        self.store.finish(dropped["id"], "ok")
        with self.assertRaises(TaskStoreError) as caught:
            self.store.resume(dropped["id"], reopen_abandoned=True)
        self.assertIn("already done", str(caught.exception))

    def test_set_request_stores_the_message_redacted_and_capped(self):
        task = self.store.start("Goal")
        self.store.set_request(task["id"], f"please use {FAKE_KEY} " + "r" * 900)
        request = self.make_store().get(task["id"])["request"]
        self.assertEqual(len(request), 500)
        self.assertNotIn(FAKE_KEY, request)


class FinishRuleTests(TaskStoreTestCase):
    def test_finish_needs_a_summary(self):
        task = self.store.start("Goal")
        for summary in ("", "   ", "\n"):
            with self.subTest(summary=summary):
                with self.assertRaises(TaskStoreError) as caught:
                    self.store.finish(task["id"], summary)
                self.assertIn("summary", str(caught.exception))
        self.assertEqual(self.store.get(task["id"])["status"], "active")

    def test_finish_is_refused_while_a_step_is_pending_in_progress_or_failed(self):
        task = self.store.start("Goal", steps=["a", "b", "c", "d"])
        self.store.set_step(task["id"], 1, "done", "did a")
        self.store.set_step(task["id"], 3, "in_progress")
        self.store.set_step(task["id"], 4, "failed", "it broke")
        with self.assertRaises(TaskStoreError) as caught:
            self.store.finish(task["id"], "All good")
        message = str(caught.exception)
        self.assertIn("Step(s) 2 (pending), 3 (in progress), 4 (failed) are not done", message)
        self.assertIn("skipped", message)
        self.assertIn("update_task plan", message)
        self.assertEqual(self.store.get(task["id"])["status"], "active")

    def test_finish_succeeds_once_every_step_is_done_or_skipped(self):
        task = self.store.start("Goal", steps=["a", "b"])
        self.store.set_step(task["id"], 1, "done", "did a")
        self.store.set_step(task["id"], 2, "skipped", "b was already done upstream")
        self.assertEqual(self.store.finish(task["id"], "All good")["status"], "done")

    def test_a_task_without_steps_may_finish(self):
        task = self.store.start("Quick question")
        self.assertEqual(self.store.finish(task["id"], "Answered")["status"], "done")

    def test_revising_the_plan_lets_the_task_finish(self):
        task = self.store.start("Goal", steps=["a", "b"])
        self.store.set_step(task["id"], 1, "done", "did a")
        self.store.set_plan(task["id"], ["a"])
        self.assertEqual(self.store.finish(task["id"], "Done")["status"], "done")


class StepTests(TaskStoreTestCase):
    def setUp(self):
        super().setUp()
        self.task = self.store.start("Goal", steps=["one", "two", "three"])

    def test_set_step_is_one_based_and_checkpoints(self):
        updated = self.store.set_step(self.task["id"], 2, "in_progress")
        self.assertEqual([s["status"] for s in updated["steps"]], ["pending", "in_progress", "pending"])
        done = self.store.set_step(self.task["id"], 2, "done", "ran it; output ok")
        self.assertEqual(done["steps"][1]["evidence"], "ran it; output ok")
        self.assertEqual(self.make_store().get(self.task["id"])["steps"][1]["status"], "done")

    def test_set_step_bounds(self):
        for number in (0, 4, -1, True, "1", None):
            with self.subTest(number=number):
                with self.assertRaises(TaskStoreError):
                    self.store.set_step(self.task["id"], number, "failed")

    def test_unknown_step_status_is_refused(self):
        with self.assertRaises(TaskStoreError):
            self.store.set_step(self.task["id"], 1, "finished")

    def test_done_needs_non_empty_evidence(self):
        for evidence in (None, "", "   "):
            with self.subTest(evidence=evidence):
                with self.assertRaises(TaskStoreError):
                    self.store.set_step(self.task["id"], 1, "done", evidence)
        self.assertEqual(self.store.get(self.task["id"])["steps"][0]["status"], "pending")

    def test_other_statuses_need_no_evidence_and_keep_earlier_evidence(self):
        self.store.set_step(self.task["id"], 1, "done", "proof")
        reopened = self.store.set_step(self.task["id"], 1, "failed")
        self.assertEqual((reopened["steps"][0]["status"], reopened["steps"][0]["evidence"]), ("failed", "proof"))

    def test_skipped_needs_non_empty_evidence(self):
        for evidence in (None, "", "   "):
            with self.subTest(evidence=evidence):
                with self.assertRaises(TaskStoreError) as caught:
                    self.store.set_step(self.task["id"], 1, "skipped", evidence)
                self.assertIn("skipped with evidence", str(caught.exception))
        self.assertEqual(self.store.get(self.task["id"])["steps"][0]["status"], "pending")
        self.assertEqual(self.store.set_step(self.task["id"], 1, "skipped", "not needed")["steps"][0]["status"], "skipped")

    def test_set_plan_keeps_done_steps_by_text(self):
        self.store.set_step(self.task["id"], 1, "done", "proof")
        self.store.set_step(self.task["id"], 2, "in_progress")
        plan = self.store.set_plan(self.task["id"], ["one", "two", "four"])
        self.assertEqual([(s["text"], s["status"]) for s in plan["steps"]],
                         [("one", "done"), ("two", "pending"), ("four", "pending")])
        self.assertEqual(plan["steps"][0]["evidence"], "proof")

    def test_set_plan_replaces_everything_else(self):
        plan = self.store.set_plan(self.task["id"], ["only"])
        self.assertEqual([s["text"] for s in plan["steps"]], ["only"])

    def test_step_text_is_capped_and_blank_steps_dropped(self):
        plan = self.store.set_plan(self.task["id"], ["x" * 500, "  ", "ok"])
        self.assertEqual([len(s["text"]) for s in plan["steps"]], [200, 2])


class ActionTests(TaskStoreTestCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.store.start("Goal")["id"]

    def task(self) -> dict:
        return self.store.get(self.task_id)

    def test_record_action_stores_event_with_timestamp(self):
        self.store.record_action(self.task_id, project_tests(False))
        (action,) = self.task()["actions"]
        self.assertEqual((action["tool"], action["status"], action["target"], action["passed"]),
                         ("run_project_tests", "success", "proj", False))
        self.assertRegex(action["at"], r"^\d{4}-\d\d-\d\dT")

    def test_successful_changes_add_files_once(self):
        for event in (edit("a.py"), edit("a.py"), {"tool": "create_workspace_file", "status": "success",
                                                    "target": "b.py"}):
            self.store.record_action(self.task_id, event)
        self.assertEqual(self.task()["files_changed"], ["a.py", "b.py"])
        self.assertEqual(len(self.task()["actions"]), 3)

    def test_unsuccessful_or_non_changing_events_do_not_add_files(self):
        for status in ("error", "denied", "partial", "unverified"):
            self.store.record_action(self.task_id, edit("bad.py", status))
        self.store.record_action(self.task_id, {"tool": "read_workspace_file", "status": "success", "target": "r.py"})
        self.store.record_action(self.task_id, {"tool": "edit_workspace_file", "status": "success"})  # no target
        self.assertEqual(self.task()["files_changed"], [])

    def test_actions_are_capped_oldest_dropped(self):
        for i in range(MAX_ACTIONS + 5):
            self.store.record_action(self.task_id, {"tool": "read_workspace_file", "status": "success",
                                                    "target": f"f{i}.txt"})
        actions = self.task()["actions"]
        self.assertEqual(len(actions), MAX_ACTIONS)
        self.assertEqual(actions[0]["target"], "f5.txt")
        self.assertEqual(actions[-1]["target"], f"f{MAX_ACTIONS + 4}.txt")

    def test_record_error_keeps_latest_capped(self):
        self.store.record_error(self.task_id, "first")
        self.store.record_error(self.task_id, "e" * 600)
        self.assertEqual(self.task()["last_error"], "e" * 300)


class VerificationGapTests(TaskStoreTestCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.store.start("Goal")["id"]

    def record(self, *events):
        for event in events:
            self.store.record_action(self.task_id, event)

    def test_no_changes_means_no_gap(self):
        self.assertIsNone(self.store.verification_gap(self.task_id))
        self.record(project_tests(False))
        self.assertIsNone(self.store.verification_gap(self.task_id))

    def test_change_without_any_test_run_is_a_gap_with_exact_text(self):
        self.record(edit("proj/a.py"), edit("proj/b.py"))
        self.assertEqual(
            self.store.verification_gap(self.task_id),
            "Run the project's tests (run_project_tests) after the last change before finishing: "
            "2 file(s) changed since the last passing run.")

    def test_change_then_failing_test_is_a_gap(self):
        self.record(edit("proj/a.py"), project_tests(False))
        self.assertIsNotNone(self.store.verification_gap(self.task_id))

    def test_change_then_passing_test_is_clear(self):
        self.record(edit("proj/a.py"), project_tests(False), project_tests(True))
        self.assertIsNone(self.store.verification_gap(self.task_id))

    def test_passing_test_then_another_change_is_a_gap(self):
        self.record(edit("proj/a.py"), project_tests(True), edit("proj/b.py"))
        self.assertIn("1 file(s) changed", self.store.verification_gap(self.task_id))  # only b.py is untested

    def test_failed_edit_after_passing_test_does_not_reopen_the_gap(self):
        self.record(edit("proj/a.py"), project_tests(True), edit("proj/b.py", "error"))
        self.assertIsNone(self.store.verification_gap(self.task_id))

    def test_other_tools_with_passed_do_not_count_as_a_test_run(self):
        self.record(edit("proj/a.py"), {"tool": "something_else", "status": "success", "passed": True})
        self.assertIsNotNone(self.store.verification_gap(self.task_id))

    def test_finish_refused_on_a_gap_and_allowed_after_a_passing_run(self):
        self.record(edit("proj/a.py"), project_tests(False))
        with self.assertRaises(TaskStoreError) as caught:
            self.store.finish(self.task_id, "done")
        self.assertEqual(str(caught.exception), self.store.verification_gap(self.task_id))
        self.assertEqual(self.store.get(self.task_id)["status"], "active")
        self.record(project_tests(True))
        self.assertEqual(self.store.finish(self.task_id, "done")["status"], "done")

    def test_gap_survives_the_action_cap(self):
        self.record(edit("proj/a.py"))
        for i in range(MAX_ACTIONS):  # pushes the edit out of the action window
            self.record({"tool": "read_workspace_file", "status": "success", "target": f"{i}"})
        self.assertIsNotNone(self.store.verification_gap(self.task_id))
        self.record(project_tests(True))
        self.assertIsNone(self.store.verification_gap(self.task_id))


    def test_a_passing_lint_does_not_clear_the_gap(self):
        self.record(edit("projA/a.py"), project_tests(True, "projA", check="lint"))
        self.assertIsNotNone(self.store.verification_gap(self.task_id))
        self.record(project_tests(True, "projA", check="tests"))
        self.assertIsNone(self.store.verification_gap(self.task_id))

    def test_a_passing_run_on_another_project_does_not_clear_the_gap(self):
        self.record(edit("projA/a.py"), project_tests(True, "projB"))
        self.assertIsNotNone(self.store.verification_gap(self.task_id))
        self.record(project_tests(True, "projAB"))  # a name that only starts the same is another folder
        self.assertIsNotNone(self.store.verification_gap(self.task_id))
        self.record(project_tests(True, "projA"))
        self.assertIsNone(self.store.verification_gap(self.task_id))

    def test_paths_are_compared_in_one_spelling(self):
        self.record(edit("./projA//src/a.py"), project_tests(True, "projA/"))
        self.assertIsNone(self.store.verification_gap(self.task_id))
        self.record(edit("projA/b.py"), project_tests(True, "./projA/src"))
        self.assertIsNotNone(self.store.verification_gap(self.task_id))  # b.py is outside projA/src

    def test_the_workspace_root_contains_every_project(self):
        self.record(edit("projA/a.py"), edit("projB/b.py"), project_tests(True, "."))
        self.assertIsNone(self.store.verification_gap(self.task_id))

    def test_a_run_must_hold_every_file_changed_since_the_last_clearing_run(self):
        self.record(edit("projA/a.py"), edit("projB/b.py"), project_tests(True, "projA"), project_tests(True, "projB"))
        self.assertIn("2 file(s) changed", self.store.verification_gap(self.task_id))
        self.record(project_tests(True, "."))
        self.assertIsNone(self.store.verification_gap(self.task_id))

    def test_old_actions_without_check_or_project_count_as_test_runs_on_their_target(self):
        self.record(edit("proj/a.py"), {"tool": "run_project_tests", "status": "success", "target": "proj",
                                        "passed": True})
        self.assertIsNone(self.store.verification_gap(self.task_id))

    def test_a_passing_run_with_no_project_does_not_count(self):
        self.record(edit("proj/a.py"), {"tool": "run_project_tests", "status": "success", "passed": True})
        self.assertIsNotNone(self.store.verification_gap(self.task_id))

    def test_record_action_keeps_the_check_and_project(self):
        self.record(project_tests(True, "projA", check="lint"))
        action = self.store.get(self.task_id)["actions"][-1]
        self.assertEqual((action["check"], action["project"]), ("lint", "projA"))
        self.assertIn("run_project_tests on projA: success (lint passed)",
                      self.store.summary_for_prompt(self.task_id))

class CapTests(TaskStoreTestCase):
    def test_max_tasks_drops_oldest_finished_and_keeps_unfinished(self):
        keep = self.store.start("old but unfinished")
        self.store.pause(keep["id"], "waiting")
        finished = []
        for i in range(MAX_TASKS + 5):
            task = self.store.start(f"job {i}")
            self.store.finish(task["id"], "ok")
            finished.append(task["id"])
        tasks = self.store.recent(limit=1000)
        self.assertEqual(len(tasks), MAX_TASKS)
        self.assertIsNotNone(self.store.get(keep["id"]))
        self.assertIsNone(self.store.get(finished[0]))
        self.assertIsNotNone(self.store.get(finished[-1]))
        self.assertEqual(len(self.make_store().recent(limit=1000)), MAX_TASKS)

    def test_unfinished_tasks_are_never_dropped_even_past_the_cap(self):
        ids = [self.store.start(f"job {i}")["id"] for i in range(MAX_TASKS + 3)]
        self.assertEqual(len(self.store.unfinished()), MAX_TASKS + 3)
        self.assertTrue(all(self.store.get(i) for i in ids))

    def test_field_length_caps(self):
        task = self.store.start("g" * 900, steps=["s"], request="r" * 900)
        self.assertEqual((len(task["goal"]), len(task["request"])), (300, 500))
        self.store.set_step(task["id"], 1, "done", "e" * 900)
        self.store.finish(task["id"], "z" * 900)
        saved = self.make_store().get(task["id"])
        self.assertEqual(len(saved["steps"][0]["evidence"]), 300)
        self.assertEqual(len(saved["summary"]), 300)


class RedactionTests(TaskStoreTestCase):
    def test_secrets_never_reach_the_file(self):
        task = self.store.start(f"Use key {FAKE_KEY} for the API", steps=[f"call with {FAKE_KEY}"],
                                request=f"my key is {FAKE_KEY}")
        self.store.set_step(task["id"], 1, "done", f"worked with {FAKE_KEY}")
        self.store.record_error(task["id"], f"401 for {FAKE_KEY}")
        self.store.record_action(task["id"], {"tool": "edit_workspace_file", "status": "success",
                                              "target": f"{FAKE_KEY}.txt"})
        self.store.pause(task["id"], f"stopped near {FAKE_KEY}")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(FAKE_KEY, raw)
        self.assertNotIn("a" * 40, raw)
        saved = self.store.get(task["id"])
        self.assertIn("[REDACTED]", saved["goal"])
        self.assertIn("[REDACTED]", saved["steps"][0]["evidence"])
        self.assertIn("[REDACTED]", saved["last_error"])

    def test_lone_surrogates_are_saved_safely(self):
        task = self.store.start("bad \ud800 text")
        self.assertEqual(self.make_store().get(task["id"])["goal"], "bad ? text")

    def test_goal_is_one_line(self):
        task = self.store.start("line one\n\n  line two\t!")
        self.assertEqual(task["goal"], "line one line two !")


class PromptSummaryTests(TaskStoreTestCase):
    def test_summary_content(self):
        task = self.store.start("Fix the parser", steps=["Read", "Edit", "Test"])
        tid = task["id"]
        self.store.set_step(tid, 1, "done", "read it")
        self.store.record_action(tid, edit("a.py"))
        self.store.record_action(tid, edit("b.py"))
        self.store.record_action(tid, project_tests(False))
        self.store.record_error(tid, "tests failed: 2 errors")
        self.store.pause(tid, "rate limit")
        text = self.store.summary_for_prompt(tid)
        self.assertEqual(text.splitlines(), [
            f"Task {tid} (paused: rate limit): Fix the parser",
            "Plan:",
            "  1. [done] Read",
            "  2. [pending] Edit",
            "  3. [pending] Test",
            "Files changed: a.py, b.py",
            "Last error: tests failed: 2 errors",
            "Last actions: edit_workspace_file on a.py: success; edit_workspace_file on b.py: success; "
            "run_project_tests on proj: success (tests failed)",
        ])

    def test_active_task_without_details_is_short(self):
        task = self.store.start("Just a goal")
        self.assertEqual(self.store.summary_for_prompt(task["id"]), f"Task {task['id']} (active): Just a goal")

    def test_summary_is_length_capped(self):
        task = self.store.start("g" * 300, steps=[f"{i} " + "s" * 190 for i in range(40)])
        for i in range(30):
            self.store.record_action(task["id"], edit(f"{'d' * 100}{i}.py"))
        text = self.store.summary_for_prompt(task["id"])
        self.assertLessEqual(len(text), 1500)
        self.assertTrue(text.startswith(f"Task {task['id']} (active): "))

    def test_resume_facts_survive_the_cap_and_the_plan_is_truncated_first(self):
        task = self.store.start("g" * 300, steps=[f"{i} " + "s" * 190 for i in range(40)])
        tid = task["id"]
        for i in range(12):
            self.store.record_action(tid, edit(f"{'d' * 60}{i}.py"))
        self.store.record_error(tid, "e" * 300)
        text = self.store.summary_for_prompt(tid)
        self.assertLessEqual(len(text), 1500)
        self.assertIn("Files changed: ", text)
        self.assertRegex(text, r"\(\+\d+ more\)")
        self.assertIn("Last error: " + "e" * 100, text)
        self.assertIn("Last actions: ", text)
        self.assertIn("edit_workspace_file on ", text.splitlines()[-1])
        self.assertIn("more step", text)  # the omitted plan lines are counted, not silently lost

    def test_tail_lines_survive_at_the_maximum_sizes(self):
        task = self.store.start("g" * 300, steps=[f"step {i} " + "s" * 180 for i in range(30)])
        tid = task["id"]
        for i in range(10):
            self.store.record_action(tid, edit(f"{'p' * 115}{i:04d}.py"))
        self.store.record_error(tid, "e" * 300)
        self.store.mark_untrusted(tid)
        self.store.pause(tid, "r" * 300)
        text = self.store.summary_for_prompt(tid)
        self.assertLessEqual(len(text), 1500)
        for fragment in ("Files changed: ", "Last error: ", "Last actions: ", task_store.UNTRUSTED_PLAN_NOTE):
            self.assertIn(fragment, text)
        self.assertTrue(text.splitlines()[-1] == task_store.UNTRUSTED_PLAN_NOTE)
        self.assertTrue(text.startswith(f"Task {tid} (paused: rrr"))
        self.assertIn("Last actions: edit_workspace_file on ", text)
        self.assertNotIn("...\n...", text)

    def test_the_maximum_sizes_hold_with_no_plan_and_with_a_plan(self):
        for steps in ([], ["a step"] * 30):
            with self.subTest(steps=len(steps)):
                task = self.store.start("g" * 300, steps=steps)
                for i in range(10):
                    self.store.record_action(task["id"], edit(f"{'q' * 118}{i}x.py"))
                self.store.record_error(task["id"], "x" * 300)
                text = self.store.summary_for_prompt(task["id"])
                self.assertLessEqual(len(text), 1500)
                self.assertTrue(all(label in text for label in ("Files changed", "Last error", "Last actions")))

    def test_last_error_equal_to_the_pause_reason_is_not_repeated(self):
        task = self.store.start("Goal")
        self.store.record_error(task["id"], "the model could not be reached")
        self.store.pause(task["id"], "the model could not be reached")
        text = self.store.summary_for_prompt(task["id"])
        self.assertEqual(text.count("the model could not be reached"), 1)
        self.assertNotIn("Last error", text)
        self.store.record_error(task["id"], "something else")
        self.assertIn("Last error: something else", self.store.summary_for_prompt(task["id"]))

    def test_untrusted_plan_mark(self):
        plain = self.store.start("Plain", steps=["a"])
        self.assertIs(plain["untrusted_plan"], False)
        self.assertNotIn("untrusted", self.store.summary_for_prompt(plain["id"]))
        self.assertIs(self.store.set_plan(plain["id"], ["b"])["untrusted_plan"], False)
        self.assertIs(self.store.set_plan(plain["id"], ["b"], untrusted=True)["untrusted_plan"], True)
        self.assertIs(self.store.set_plan(plain["id"], ["c"])["untrusted_plan"], True)  # never clears
        self.assertIn(task_store.UNTRUSTED_PLAN_NOTE, self.store.summary_for_prompt(plain["id"]))
        started = self.store.start("Marked", untrusted=True)
        self.assertIs(self.store.get(started["id"])["untrusted_plan"], True)

    def test_old_records_load_with_the_mark_off(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"tasks": [{"id": "told001", "goal": "Old", "status": "paused"}]}))
        self.assertIs(self.make_store().get("told001")["untrusted_plan"], False)

    def test_unfinished_steps_are_kept_before_done_ones_when_the_plan_is_cut(self):
        task = self.store.start("Goal", steps=[f"step {i} " + "s" * 150 for i in range(1, 21)])
        tid = task["id"]
        for n in range(1, 16):
            self.store.set_step(tid, n, "done", "ok")
        text = self.store.summary_for_prompt(tid)
        self.assertLessEqual(len(text), 1500)
        self.assertIn("[pending] step 20 ", text)
        self.assertIn("[pending] step 16 ", text)

    def test_many_changed_files_are_abbreviated(self):
        task = self.store.start("Goal")
        for i in range(14):
            self.store.record_action(task["id"], edit(f"f{i}.py"))
        text = self.store.summary_for_prompt(task["id"])
        self.assertIn("(+4 more)", text)
        self.assertNotIn("f13.py", text.split("Last actions")[0])


if __name__ == "__main__":
    unittest.main()
