"""The JSON memory store."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    TempDirTestCase,
)
import jarvis  # noqa: E402


class MemoryStoreTests(TempDirTestCase):
    def test_missing_file_initializes_empty_schema(self):
        store = self.make_memory()
        self.assertEqual(store.data, jarvis.empty_memory())
        self.assertTrue(store.writable)
        self.assertIsNone(store.corruption_error)
        self.assertFalse(self.memory_path.exists())

    def test_valid_file_loads(self):
        self.memory_path.parent.mkdir(parents=True)
        payload = {"user_name": "Emanuel", "location": "Almeria",
                   "facts": [{"id": "fabc123", "text": "Likes espresso", "created_at": None,
                              "updated_at": None, "source": "user", "priority": "high"}],
                   "conversations": [], "custom": {"keep": True}}
        self.memory_path.write_text(json.dumps(payload), encoding="utf-8")
        store = self.make_memory()
        self.assertTrue(store.writable)
        self.assertEqual(store.profile(), {"user_name": "Emanuel", "location": "Almeria"})
        self.assertEqual(store.find_fact("fabc123")["text"], "Likes espresso")
        self.assertEqual(store.data["custom"], {"keep": True})

    def test_malformed_json_is_never_overwritten(self):
        self.memory_path.parent.mkdir(parents=True)
        original = b'{"facts": [ {"text": "half written'
        self.memory_path.write_bytes(original)
        store = self.make_memory()
        self.assertIsNotNone(store.corruption_error)
        self.assertFalse(store.writable)
        self.assertIn("not valid JSON", store.corruption_error)

        with self.assertRaises(jarvis.MemoryStoreError):
            store.save()
        self.assertEqual(self.memory_path.read_bytes(), original)

        with self.assertRaises(jarvis.MemoryStoreError):
            store.remember_fact("I like green tea")
        self.assertEqual(store.list_facts(), [])  # rolled back in memory too
        self.assertEqual(self.memory_path.read_bytes(), original)

        store.add_conversation("hello", "hi there")  # silently skipped
        self.assertEqual(self.memory_path.read_bytes(), original)

        with self.assertRaises(jarvis.MemoryStoreError):
            store.set_profile(user_name="Tony")
        self.assertEqual(self.memory_path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.memory_path.parent.iterdir()), [self.memory_path.name])

    def test_non_object_json_is_treated_as_corrupt(self):
        self.memory_path.parent.mkdir(parents=True)
        self.memory_path.write_text("[1, 2, 3]", encoding="utf-8")
        store = self.make_memory()
        self.assertFalse(store.writable)

    def test_reset_corrupted_backs_up_file(self):
        self.memory_path.parent.mkdir(parents=True)
        original = b"{not json at all"
        self.memory_path.write_bytes(original)
        store = self.make_memory()
        backup = store.reset_corrupted()
        self.assertTrue(backup.exists())
        self.assertNotEqual(backup, self.memory_path)
        self.assertEqual(backup.read_bytes(), original)
        self.assertTrue(store.writable)
        self.assertEqual(json.loads(self.memory_path.read_text(encoding="utf-8")), jarvis.empty_memory())
        store.remember_fact("I prefer concise answers")
        self.assertEqual(len(self.make_memory().list_facts()), 1)

    def test_facts_persist_across_restart(self):
        store = self.make_memory()
        outcome = store.remember_fact("My favourite colour is red", priority="high")
        self.assertEqual(outcome["status"], "saved")
        fact_id = outcome["fact"]["id"]
        restarted = self.make_memory()
        fact = restarted.find_fact(fact_id)
        self.assertIsNotNone(fact)
        self.assertEqual(fact["text"], "My favourite colour is red")
        self.assertEqual(fact["priority"], "high")
        self.assertEqual(fact["source"], "user")
        self.assertIsNotNone(fact["created_at"])

    def test_duplicates_detected(self):
        store = self.make_memory()
        first = store.remember_fact("I like green tea")
        for variant in ("i LIKE green tea", "  I like   green\ttea  ", "I like green tea.", "I like green tea!"):
            with self.subTest(variant=variant):
                outcome = store.remember_fact(variant)
                self.assertEqual(outcome["status"], "duplicate")
                self.assertEqual(outcome["fact"]["id"], first["fact"]["id"])
        self.assertEqual(len(store.list_facts()), 1)

    def test_update_fact(self):
        store = self.make_memory()
        fact_id = store.remember_fact("I live in Leeds")["fact"]["id"]
        outcome = store.update_fact(fact_id, "I live in York")
        self.assertEqual(outcome["previous_text"], "I live in Leeds")
        self.assertEqual(outcome["fact"]["text"], "I live in York")
        self.assertIsNotNone(outcome["fact"]["updated_at"])
        self.assertEqual(self.make_memory().find_fact(fact_id)["text"], "I live in York")

    def test_update_fact_unknown_id_and_duplicate_text(self):
        store = self.make_memory()
        a = store.remember_fact("I live in Leeds")["fact"]["id"]
        store.remember_fact("I drive a Tesla")
        with self.assertRaises(jarvis.MemoryStoreError):
            store.update_fact("fnope00", "anything")
        with self.assertRaises(jarvis.MemoryStoreError):
            store.update_fact(a, "i drive a tesla.")
        self.assertEqual(store.find_fact(a)["text"], "I live in Leeds")

    def test_forget_fact(self):
        store = self.make_memory()
        fact_id = store.remember_fact("I have a cat called Dummy")["fact"]["id"]
        removed = store.forget_fact(fact_id)
        self.assertEqual(removed["id"], fact_id)
        self.assertIsNone(store.find_fact(fact_id))
        self.assertIsNone(self.make_memory().find_fact(fact_id))
        with self.assertRaises(jarvis.MemoryStoreError):
            store.forget_fact(fact_id)

    def test_conversation_history_bounded(self):
        store = self.make_memory(max_conversations=3)
        for i in range(6):
            store.add_conversation(f"question {i}", f"answer {i}")
        self.assertEqual(len(store.data["conversations"]), 3)
        self.assertEqual([c["user"] for c in store.data["conversations"]], ["question 3", "question 4", "question 5"])
        on_disk = json.loads(self.memory_path.read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["conversations"]), 3)

    def test_conversation_text_is_redacted(self):
        store = self.make_memory()
        key = "gsk_" + "k" * 30
        store.add_conversation(f"my key is {key}", "noted")
        self.assertNotIn(key, self.memory_path.read_text(encoding="utf-8"))

    def test_secrets_refused(self):
        store = self.make_memory()
        secrets = [
            "My password is hunter2",
            "password: correcthorse",
            "my groq key is gsk_" + "abcDEF123" * 4,
            "my card is 4111 1111 1111 1111",
            "card 4111-1111-1111-1111 exp 12/29",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        ]
        for secret in secrets:
            with self.subTest(secret=secret[:30]):
                with self.assertRaises(jarvis.MemoryStoreError) as ctx:
                    store.remember_fact(secret)
                self.assertIn("secret", str(ctx.exception).lower())
        self.assertEqual(store.list_facts(), [])
        self.assertFalse(self.memory_path.exists())

    def test_empty_and_too_long_facts_refused(self):
        store = self.make_memory()
        with self.assertRaises(jarvis.MemoryStoreError):
            store.remember_fact("   ")
        with self.assertRaises(jarvis.MemoryStoreError):
            store.remember_fact("word " * 200)

    def test_get_relevant_memories_bounded_and_ranked(self):
        store = self.make_memory()
        for text in ("I work as a nurse", "My dog is named Rex", "I live in Leeds",
                     "I enjoy hiking at weekends", "My sister is called Ana", "I play the piano",
                     "My favourite coffee is a flat white", "I am allergic to peanuts"):
            store.remember_fact(text)
        results = store.get_relevant_memories("What coffee do I like?", limit=5)
        self.assertLessEqual(len(results), 5)
        self.assertEqual(results[0]["text"], "My favourite coffee is a flat white")
        self.assertLessEqual(len(store.get_relevant_memories("anything at all")), 5)

    def test_get_relevant_memories_high_priority_beats_irrelevant(self):
        store = self.make_memory()
        store.remember_fact("Call me Sir", priority="high")
        for i in range(6):
            store.remember_fact(f"Unrelated fact number {i} about trains")
        results = store.get_relevant_memories("hello", limit=5)
        self.assertEqual(results[0]["text"], "Call me Sir")

    def test_legacy_string_facts_normalized(self):
        self.memory_path.parent.mkdir(parents=True)
        self.memory_path.write_text(json.dumps({"facts": ["Likes tea", {"text": "Lives in Leeds"}, "", 42]}),
                                    encoding="utf-8")
        store = self.make_memory()
        facts = store.list_facts()
        self.assertEqual([f["text"] for f in facts], ["Likes tea", "Lives in Leeds"])
        for fact in facts:
            for key in ("id", "created_at", "updated_at", "source", "priority"):
                self.assertIn(key, fact)
            self.assertTrue(fact["id"].startswith("f"))
        self.assertEqual(store.data["conversations"], [])
        self.assertIsNone(store.data["user_name"])

    def test_atomic_save_leaves_no_temp_files(self):
        store = self.make_memory()
        for i in range(5):
            store.remember_fact(f"Fact number {i} about apples")
        self.assertEqual([p.name for p in self.memory_path.parent.iterdir()], [self.memory_path.name])
        self.assertEqual(oct(self.memory_path.stat().st_mode & 0o777), oct(0o600))

    def test_failed_save_rolls_back_and_cleans_up(self):
        store = self.make_memory()
        store.remember_fact("I like apples")
        before = self.memory_path.read_bytes()
        with mock.patch("jarvis.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(jarvis.MemoryStoreError):
                store.remember_fact("I like pears")
        self.assertEqual(self.memory_path.read_bytes(), before)
        self.assertEqual([f["text"] for f in store.list_facts()], ["I like apples"])
        self.assertEqual([p.name for p in self.memory_path.parent.iterdir()], [self.memory_path.name])


if __name__ == "__main__":
    unittest.main()
