"""Sorting and removal use fake accounts; no real credentials or network."""
import contextlib
import copy
import datetime as dt
import fcntl
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import claude_usage as app
import live_usage as live

NOW = dt.datetime(2026, 10, 13, tzinfo=app.UTC)
KEY, SALT = b"k" * 32, b"s" * 16


def row(ident, provider, name, used, reset=None):
    plan = {"claude": "max_20x", "codex": "codex", "muse_code": "muse_code"}[provider]
    usage = app.parse_usage({"five_hour": {"utilization": used, "resets_at": reset},
                             "seven_day": {"utilization": used, "resets_at": reset}})
    return dict(id=ident, provider=provider, plan=plan, name=name, usage=usage,
                status="live", live=True, reported_plan=None, captured_at=NOW.isoformat())


class SortingTests(unittest.TestCase):
    def setUp(self):
        self.data = {"accounts": [
            row("meta", "muse_code", "Muse", 10),
            row("unknown", "claude", "Unknown", None),
            row("high", "claude", "Zebra", 90, "2026-10-13T03:00:00+02:00"),
            row("low", "claude", "alice", 9, "2026-10-13T00:30:00Z"),
            row("openai", "codex", "Codex", 50)]}

    def ids(self, column, reverse=False):
        return [r["id"] for r in app.sort_report(self.data, column, reverse)["accounts"]]

    def test_numeric_sort_both_directions_missing_last_and_groups_fixed(self):
        before = copy.deepcopy(self.data)
        for column in ("5h", "7d", "5h-reset", "7d-reset"):
            self.assertEqual(self.ids(column), ["openai", "low", "high", "unknown", "meta"])
            self.assertEqual(self.ids(column, True), ["openai", "high", "low", "unknown", "meta"])
        self.assertEqual(self.data, before)

    def test_name_plan_status_and_equal_values_are_stable(self):
        self.assertEqual(self.ids("name"), ["openai", "low", "unknown", "high", "meta"])
        self.assertEqual(self.ids("name", True), ["openai", "high", "unknown", "low", "meta"])
        for column in ("plan", "status"):
            self.assertEqual(self.ids(column), ["openai", "unknown", "high", "low", "meta"])

    def test_grouped_tables_share_widths_and_have_no_extra_text(self):
        output = app.render(app.sort_report(self.data), NOW, width=240)
        sections = output.split("\n\n")
        for section, title in zip(sections, ("OpenAI", "Anthropic", "Meta")):
            self.assertTrue(section.startswith("╭─ " + title + " "))
            self.assertTrue(section.endswith("╯"))
        self.assertEqual(len({s.splitlines()[1] for s in sections}), 1)
        self.assertEqual(output.count("Account"), 3)
        self.assertNotIn("Subscription usage", output)

    def test_colours_preserve_layout_meaning_and_unicode_alignment(self):
        self.data["accounts"][0]["name"] = "李 / Cafe\u0301"
        plain = app.render(self.data, NOW, width=240)
        coloured = app.render(self.data, NOW, color=True, width=240)
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", coloured), plain)
        widths = {app.display_width(line) for line in plain.splitlines() if line}
        self.assertEqual(len(widths), 1)
        self.assertRegex(coloured, r"\x1b\[31m *90%")
        self.assertRegex(coloured, r"\x1b\[33m *50%")
        self.assertRegex(coloured, r"\x1b\[32m *9%")
        self.assertIn("● LIVE", plain)
        self.assertIn("↻", plain)
        self.assertNotIn("\x1b", plain)

    def test_automatic_colour_respects_terminal_and_environment(self):
        with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True), patch("sys.stdout.isatty", return_value=True):
            self.assertTrue(app.color_enabled("auto"))
            self.assertFalse(app.color_enabled("never"))
            with patch.dict(os.environ, {"NO_COLOR": ""}):
                self.assertFalse(app.color_enabled("auto"))
                self.assertTrue(app.color_enabled("always"))
            with patch.dict(os.environ, {"TERM": "dumb"}):
                self.assertFalse(app.color_enabled("auto"))
            with patch("sys.stdout.isatty", return_value=False):
                self.assertFalse(app.color_enabled("auto"))


class ResponsiveTests(unittest.TestCase):
    def setUp(self):
        self.data = {'selection_checked': True, 'accounts': []}
        for provider, name in [('codex','codex-user@example.test'), ('claude','team-seat-3@example.test'),
                               ('muse_code','very-long-subscription-email@example.test'),
                               ('claude','李 / Cafe\u0301 ' * 8)]:
            item = row(name, provider, name, 42, '2026-10-14T02:00:00Z')
            item.update(selected=provider=='muse_code', switch_readiness='expired' if provider=='claude' else 'not_switchable')
            self.data['accounts'].append(item)
        self.data['accounts'][1].update(status='not_configured', live=False, usage=None)

    def test_every_default_output_line_fits_terminal_and_colours_align(self):
        before = copy.deepcopy(self.data)
        for width in (12, 20, 40, 60, 75, 76, 80, 100, 120, 160, 240):
            with self.subTest(width=width):
                plain = app.render(self.data, NOW, width=width)
                colour = app.render(self.data, NOW, width=width, color=True)
                self.assertEqual(re.sub(r'\x1b\[[0-9;]*m', '', colour), plain)
                self.assertLessEqual(max(map(app.display_width, plain.splitlines())), width)
                self.assertIn('42%', plain)
                self.assertIn('▶', plain)
        self.assertEqual(before, self.data)

    def test_terminal_width_is_detected_and_names_are_wrapped_not_discarded(self):
        with patch.object(app.shutil, 'get_terminal_size', return_value=os.terminal_size((80,24))):
            output = app.render(self.data, NOW)
        self.assertLessEqual(max(map(app.display_width, output.splitlines())),80)
        self.assertIn('Sign in', output)
        self.assertIn('example.test', output)
        self.assertIn('↻', output)

    def test_verbose_retains_exact_local_reset_timestamps(self):
        output = app.render(self.data, NOW, width=80, verbose=True)
        self.assertIn(app.reset_text('2026-10-14T02:00:00Z', NOW), output)
        table_lines = [line for line in output.splitlines() if line.startswith(('╭','│','├','╰'))]
        self.assertLessEqual(max(map(app.display_width,table_lines)),80)

    def test_empty_narrow_report_fits(self):
        text = app.render({'accounts':[]}, NOW, width=20)
        self.assertIn('No accounts', text)
        self.assertLessEqual(max(map(app.display_width,text.splitlines())),20)


class RecommendationTests(unittest.TestCase):
    def account(self, ident, remaining, hours, weekly_remaining=100, weekly_hours=100):
        reset = (NOW + dt.timedelta(hours=hours)).isoformat() if hours is not None else None
        result = row(ident, "claude", ident, 100 - remaining, reset)
        result["usage"]["seven_day"] = {
            "utilization": 100 - weekly_remaining,
            "resets_at": (NOW + dt.timedelta(hours=weekly_hours)).isoformat() if weekly_hours is not None else None}
        return result

    def ordered(self, *accounts, reverse=False):
        data = {"accounts": list(accounts), "generated_at": NOW.isoformat()}
        before = copy.deepcopy(data)
        result = app.sort_report(data, reverse=reverse)
        self.assertEqual(data, before)
        return [a["id"] for a in result["accounts"]]

    def test_balances_capacity_and_reset_instead_of_just_one_column(self):
        soon = self.account("soon", 70, 1)
        later = self.account("later", 90, 4)
        almost_empty = self.account("almost-empty", 1, 1 / 60)
        self.assertEqual(self.ordered(almost_empty, later, soon), ["soon", "later", "almost-empty"])
        larger = self.account("larger", 90, 1)
        self.assertEqual(self.ordered(soon, larger), ["larger", "soon"])

    def test_weekly_headroom_limits_priority_and_either_limit_can_block(self):
        weekly_tight = self.account("weekly-tight", 95, 1, weekly_remaining=5)
        usable = self.account("usable", 70, 3, weekly_remaining=80)
        weekly_blocked = self.account("weekly-blocked", 100, 1, weekly_remaining=0)
        five_blocked = self.account("five-blocked", 0, 1)
        overage = self.account("overage", 0, 2)
        overage["usage"]["five_hour"]["utilization"] = 101
        self.assertEqual(self.ordered(weekly_blocked, five_blocked, weekly_tight, usable, overage),
                         ["usable", "weekly-tight", "five-blocked", "overage", "weekly-blocked"])

    def test_weekly_reset_can_drive_urgency_and_all_blocked_windows_must_reset(self):
        weekly_soon = self.account("weekly-soon", 70, 4, weekly_remaining=60, weekly_hours=0.5)
        five_soon = self.account("five-soon", 70, 1, weekly_remaining=60)
        self.assertEqual(self.ordered(five_soon, weekly_soon), ["weekly-soon", "five-soon"])
        both_blocked = self.account("both-blocked", 0, 1, weekly_remaining=0, weekly_hours=10)
        one_blocked = self.account("one-blocked", 0, 2)
        self.assertEqual(self.ordered(both_blocked, one_blocked), ["one-blocked", "both-blocked"])

    def test_weekly_only_accounts_are_eligible_without_inventing_missing_limit(self):
        weekly_only = self.account("weekly-only", 80, 1, weekly_remaining=80, weekly_hours=2)
        weekly_only["usage"]["five_hour"] = app.window(None)
        later = self.account("later", 80, 4)
        self.assertEqual(self.ordered(later, weekly_only), ["weekly-only", "later"])

    def test_missing_elapsed_and_historical_data_never_imply_fresh_capacity(self):
        good = self.account("good", 10, 1)
        missing = self.account("missing-reset", 100, None)
        elapsed = self.account("elapsed-reset", 90, -1)
        blocked = self.account("blocked", 0, -1)
        snapshot = self.account("snapshot", 100, 0.01)
        snapshot.update(live=False, status="snapshot")
        error = self.account("error", 100, 0.01)
        error.update(live=False, status="error", usage=None)
        unknown = self.account("unknown", 100, 0.01)
        unknown["usage"] = app.parse_usage({})
        self.assertEqual(self.ordered(snapshot, error, blocked, unknown, elapsed, missing, good),
                         ["good", "missing-reset", "elapsed-reset", "blocked", "error", "snapshot", "unknown"])

    def test_reverse_preserves_availability_tiers(self):
        good = self.account("good", 90, 1)
        less = self.account("less", 10, 1)
        blocked = self.account("blocked", 0, 1)
        self.assertEqual(self.ordered(blocked, good, less, reverse=True), ["less", "good", "blocked"])


class RemovalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name) / "data"
        self.store = app.Store(self.directory, create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.accounts = [dict(id="max-1", name="Old label", plan="max_20x"),
                         dict(id="team-1", name="Team", plan="team_premium"),
                         dict(id="muse-1", name="Muse", plan="muse_code", source="cli_current")]
        self.entries = {
            "max-1": {"access_token": "FAKE-ACCESS-MAX", "expires_at": None,
                      "identity": {"email": "person@example.com", "name": "Person"}},
            "team-1": {"access_token": "FAKE-ACCESS-TEAM", "expires_at": None,
                       "identity": {"email": "team@example.com"}}}
        self.save()
        self.store.write("max-1.json", {"captured_at": NOW.isoformat(), "usage": {}})
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(live.vault_cache, "get", return_value=KEY))
        for target in ("live_usage.passphrase", "live_usage.read_access_token", "muse_cli_usage.read_credentials",
                       "http.client.HTTPSConnection", "subprocess.Popen", "os.fork", "threading.Thread.start"):
            self.stack.enter_context(patch(target, side_effect=AssertionError(target)))

    def save(self):
        self.store.write("accounts.json", {"accounts": self.accounts}, replace=True)
        self.store.write("vault.json", live.encrypt_vault(self.entries, KEY, SALT), replace=True)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("credential_state.current", return_value={}):
            code = app.main(["--jobs", "1", "--data-dir", str(self.directory), *args])
        return code, out.getvalue(), err.getvalue()

    def contents(self):
        return {p.name: p.read_bytes() for p in self.directory.iterdir()}

    def test_remove_email_purges_only_selected_copy_and_snapshot(self):
        code, out, err = self.run_cli("--remove", "PERSON@example.com", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["account_id"], "max-1")
        self.assertNotIn("FAKE-ACCESS", out)
        self.assertEqual(live.load_vault(self.store), {"team-1": self.entries["team-1"]})
        self.assertEqual(self.store.read("accounts.json")["accounts"], self.accounts[1:])
        self.assertFalse((self.directory / "max-1.json").exists())
        self.assertEqual((self.directory / "vault.json").stat().st_mode & 0o777, 0o600)

    def test_unknown_and_ambiguous_selectors_do_not_modify_data(self):
        self.entries["team-1"]["identity"] = {"email": "person@example.com"}
        self.save()
        before = self.contents()
        for selector in ("nobody@example.com", "person@example.com", "../../.claude"):
            self.assertEqual(self.run_cli("--remove", selector)[0], 2)
            self.assertEqual(self.contents(), before)
        self.assertEqual(self.run_cli("--remove", "person@example.com", "--plan", "team_premium")[0], 0)
        self.assertEqual(live.load_vault(self.store), {"max-1": self.entries["max-1"]})

    def test_remove_names_ids_muse_and_last_account(self):
        for selector in ("Old label", "team-1", "Muse"):
            self.assertEqual(self.run_cli("--remove", selector)[0], 0)
        self.assertEqual(live.load_vault(self.store), {})
        self.assertEqual(app.validate_accounts(self.store.read("accounts.json")), [])
        code, out, err = self.run_cli("--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["accounts"], [])
        self.assertFalse(json.loads(out)["live"])
        self.assertEqual(self.run_cli("--add", "new", "--plan", "codex")[0], 0)

    def test_remove_without_vault_needs_no_authentication(self):
        self.store.delete("vault.json")
        self.assertEqual(self.run_cli("--remove", "muse-1")[0], 0)
        self.assertFalse((self.directory / "vault.json").exists())

    def test_bad_unlock_does_not_delete_anything(self):
        before = self.contents()
        with patch.object(live, "unlock_cached", side_effect=live.LiveError("vault_locked", "Cannot unlock vault.")):
            self.assertEqual(self.run_cli("--remove", "max-1")[0], 2)
        self.assertEqual(self.contents(), before)

    def test_locked_store_rejects_removal(self):
        before = self.contents()
        fcntl.flock(self.store.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertEqual(self.run_cli("--remove", "max-1")[0], 2)
        finally:
            fcntl.flock(self.store.fd, fcntl.LOCK_UN)
        self.assertEqual(self.contents(), before)

    def test_vault_write_failure_preserves_config_and_snapshot(self):
        before = self.contents()
        with patch.object(app.Store, "write", side_effect=OSError("fake write failure")):
            self.assertEqual(self.run_cli("--remove", "max-1")[0], 2)
        self.assertEqual(self.contents(), before)

    def test_snapshot_symlink_never_changes_credential_target(self):
        target = self.directory.parent / "fake-provider-credentials"
        target.write_bytes(b"FAKE-PROVIDER-CREDENTIALS")
        self.store.delete("max-1.json")
        (self.directory / "max-1.json").symlink_to(target)
        before = target.stat()
        self.assertEqual(self.run_cli("--remove", "max-1")[0], 0)
        self.assertEqual(target.stat(), before)
        self.assertEqual(target.read_bytes(), b"FAKE-PROVIDER-CREDENTIALS")

    def test_cli_sort_applies_to_json_with_one_retrieval_cycle(self):
        report = {"accounts": [row("high", "claude", "High", 90), row("low", "claude", "Low", 9)],
                  "live": True, "live_accounts": 2}
        with patch.object(app, "report", return_value=report) as retrieve:
            code, out, err = self.run_cli("--sort", "5h", "--reverse", "--json", "--color", "always")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([r["id"] for r in json.loads(out)["accounts"]], ["high", "low"])
        self.assertNotIn("\x1b", out)
        retrieve.assert_called_once()

    def test_cli_defaults_to_recommended_with_no_additional_fetch(self):
        report = {"accounts": [row("almost-empty", "claude", "A", 99, (NOW + dt.timedelta(minutes=1)).isoformat()),
                               row("usable", "claude", "Z", 20, (NOW + dt.timedelta(hours=2)).isoformat())],
                  "generated_at": NOW.isoformat(), "live": True, "live_accounts": 2}
        with patch.object(app, "report", return_value=report) as retrieve:
            code, out, err = self.run_cli("--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([r["id"] for r in json.loads(out)["accounts"]], ["usable", "almost-empty"])
        retrieve.assert_called_once()
