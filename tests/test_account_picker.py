"""Email selection and the arrow-key picker. Fake accounts; no terminal or network."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import account_picker as picker
import claude_usage as app

EMAIL = "person@example.test"
ACCOUNTS = [{"id": "max-a", "name": EMAIL, "plan": "max_20x"},
            {"id": "team-a", "name": EMAIL, "plan": "team_premium"},
            {"id": "codex-a", "name": "Work Codex", "plan": "codex"}]


class MatchTests(unittest.TestCase):
    def test_unique_email_any_case(self):
        self.assertEqual(picker.match(ACCOUNTS[1:], {}, EMAIL.upper())["id"], "team-a")

    def test_saved_identity_email_matches(self):
        entries = {"codex-a": {"identity": {"email": "codex@example.test"}}}
        self.assertEqual(picker.match(ACCOUNTS, entries, "codex@example.test")["id"], "codex-a")

    def test_shared_email_is_ambiguous_with_candidates(self):
        with self.assertRaises(picker.Ambiguous) as caught:
            picker.match(ACCOUNTS, {}, EMAIL)
        self.assertEqual([a["id"] for a in caught.exception.candidates], ["max-a", "team-a"])
        self.assertIsInstance(caught.exception, app.UsageError)

    def test_unknown_email(self):
        with self.assertRaises(app.UsageError) as caught:
            picker.match(ACCOUNTS, {}, "nobody@example.test")
        self.assertNotIsInstance(caught.exception, picker.Ambiguous)

    def test_plan_narrows(self):
        self.assertEqual(picker.match(ACCOUNTS, {}, EMAIL, "max_20x")["id"], "max-a")


class PickerTests(unittest.TestCase):
    def choose(self, keys):
        frames = []
        return picker.choose_index("Which?", ["A", "B", "C"], iter(keys), frames.append), frames

    def test_arrows_move_and_enter_chooses(self):
        self.assertEqual(self.choose(["down", "down", "enter"])[0], 2)
        self.assertEqual(self.choose(["up", "enter"])[0], 2)  # wraps to the last
        self.assertEqual(self.choose(["down", "down", "down", "enter"])[0], 0)  # wraps to the first

    def test_cancel_and_end_of_input(self):
        self.assertIsNone(self.choose(["down", "cancel"])[0])
        self.assertIsNone(self.choose([])[0])

    def test_redraw_highlights_selection(self):
        _, frames = self.choose(["down", "enter"])
        self.assertEqual(len(frames), 2)
        self.assertIn("❯ B", frames[-1])
        self.assertNotIn("❯ A", frames[-1])

    def test_raw_key_decoding(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        for data in (b"\x1b[A", b"\x1b[B", b"j", b"k", b"\r", b"\x1b", b"q", b"\x03"):
            os.write(write, data)
            yield_ = next(picker._keys(read))
            self.assertIn(yield_, ("up", "down", "enter", "cancel"))
        os.close(write)
        self.assertEqual(next(picker._keys(read)), "cancel")

    def test_describe_disambiguates_same_plan(self):
        twins = [{"id": "team-a", "name": EMAIL, "plan": "team_premium"},
                 {"id": "team-b", "name": EMAIL, "plan": "team_premium"}]
        self.assertEqual(picker.describe(ACCOUNTS[0], ACCOUNTS), f"{EMAIL} · Max 20×")
        self.assertIn("(team-b)", picker.describe(twins[1], twins))


class RunTests(unittest.TestCase):
    def test_ambiguity_asks_then_retries_with_chosen_row(self):
        calls = []

        def action(selector, plan):
            calls.append((selector, plan))
            return picker.match(ACCOUNTS, {}, selector, plan)
        prompts = []
        result = picker.run(action, EMAIL, picker=lambda title, labels: (prompts.append(labels), 1)[1],
                            is_interactive=lambda: True)
        self.assertEqual(result["id"], "team-a")
        self.assertEqual(calls, [(EMAIL, None), ("team-a", "team_premium")])
        self.assertEqual(prompts, [[f"{EMAIL} · Max 20×", f"{EMAIL} · Team Premium"]])

    def test_plan_question(self):
        def action(selector, plan):
            if plan is None:
                raise picker.PlanNeeded(["max_20x", "team_premium"])
            return plan
        self.assertEqual(picker.run(action, EMAIL, picker=lambda t, labels: 0, is_interactive=lambda: True), "max_20x")

    def test_without_terminal_refuses_and_lists_choices(self):
        with self.assertRaises(app.UsageError) as caught:
            picker.run(lambda s, p: picker.match(ACCOUNTS, {}, s, p), EMAIL, is_interactive=lambda: False)
        self.assertIn("Max 20×", str(caught.exception))
        self.assertIn("Team Premium", str(caught.exception))
        self.assertIn("terminal", str(caught.exception))

    def test_cancel_changes_nothing(self):
        def cancel(title, labels):
            raise app.UsageError("Cancelled; nothing changed.")
        with self.assertRaises(app.UsageError):
            picker.run(lambda s, p: picker.match(ACCOUNTS, {}, s, p), EMAIL, picker=cancel, is_interactive=lambda: True)


class AddTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tool"
        self.store = app.Store(self.path, create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.store.write("accounts.json", {"accounts": ACCOUNTS})

    def accounts(self):
        return app.validate_accounts(self.store.read("accounts.json"))

    def test_add_derives_id_and_asks_for_plan(self):
        with self.assertRaises(picker.PlanNeeded):
            app.add_account(self.store, self.accounts(), "new@example.test", None)
        result = app.add_account(self.store, self.accounts(), "New@Example.test", "codex")
        added = next(a for a in self.accounts() if a["id"] == result["account_id"])
        self.assertEqual((added["name"], added["plan"]), ("new@example.test", "codex"))
        self.assertRegex(added["id"], r"^codex-[0-9a-f]{16}$")

    def test_add_rejects_duplicates_and_non_emails(self):
        before = (self.path / "accounts.json").read_bytes()
        for email, plan in ((EMAIL, "max_20x"), ("not-an-email", "codex")):
            with self.assertRaises(app.UsageError):
                app.add_account(self.store, self.accounts(), email, plan)
        self.assertEqual((self.path / "accounts.json").read_bytes(), before)

    def test_cli_add_with_picker(self):
        with patch.object(picker, "interactive", return_value=True), \
                patch.object(picker, "pick", return_value=list(app.PLANS).index("team_premium")), \
                patch("sys.stdout"):
            self.assertEqual(app.main(["--data-dir", str(self.path), "--add", "seat@example.test"]), 0)
        self.assertIn(("seat@example.test", "team_premium"), [(a["name"], a["plan"]) for a in self.accounts()])


if __name__ == "__main__":
    unittest.main()
