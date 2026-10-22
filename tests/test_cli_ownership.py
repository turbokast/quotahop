"""Single-owner hand-back between Claude Code and QuotaHop. Fake files only."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import auth_renewal as renewal
import claude_switch as switch
import claude_usage as app
import cli_ownership as ownership
import live_usage as live

KEY, SALT = b"k" * 32, b"s" * 16
IDS = {"a": ("00000000-0000-4000-8000-00000000000a", "a@example.test"),
       "b": ("00000000-0000-4000-8000-00000000000b", "b@example.test")}
ORG = "00000000-0000-4000-8000-0000000000ff"


def binding(name):
    return {"account_uuid": IDS[name][0], "organization_uuid": ORG}


def profile_for(token):
    name = "a" if "-A-" in token else "b" if "-B-" in token else None
    if name is None:
        raise live.LiveError("authentication_error", "unknown")
    return {"account": {"uuid": IDS[name][0], "email": IDS[name][1], "display_name": name},
            "organization": {"uuid": ORG, "organization_type": "claude_max", "rate_limit_tier": "tier"}}


def oauth(name, serial):
    return {"accessToken": f"FAKE-{name.upper()}-ACCESS-{serial}", "refreshToken": f"FAKE-{name.upper()}-REFRESH-{serial}",
            "expiresAt": int((time.time() + 8 * 3600) * 1000), "refreshTokenExpiresAt": int((time.time() + 30 * 86400) * 1000),
            "scopes": ["user:inference", "user:profile"], "subscriptionType": "max", "rateLimitTier": "tier"}


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = app.Store(root / "tool", create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.claude = root / "home" / ".claude"
        self.claude.mkdir(parents=True, mode=0o700)
        self.creds, self.meta = self.claude / ".credentials.json", root / "home" / ".claude.json"
        self.meta.write_text(json.dumps({"theme": "dark", "oauthAccount": {"emailAddress": IDS["a"][1]}}))
        self.accounts = [{"id": "max-a", "name": IDS["a"][1], "plan": "max_20x"},
                         {"id": "max-b", "name": IDS["b"][1], "plan": "max_20x"}]
        self.store.write("accounts.json", {"accounts": self.accounts})
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        stack.enter_context(patch.object(switch, "paths", return_value=(self.creds, self.meta)))
        stack.enter_context(patch.object(live.vault_cache, "get", return_value=KEY))
        self.profile = stack.enter_context(patch.object(switch.ProfileClient, "request",
                                                        side_effect=lambda method, url, token: profile_for(token)))
        self.renew = stack.enter_context(patch("owned_claude.perform", side_effect=AssertionError("unexpected renewal")))
        ownership._seen.clear()

    def write_cli(self, value):
        self.creds.write_text(json.dumps({"designOauth": {"accessToken": "FAKE-DESIGN"}, "claudeAiOauth": value}))
        self.creds.chmod(0o600)

    def cli(self):
        return json.loads(self.creds.read_text())["claudeAiOauth"]

    def install(self, a_state, a_oauth, b_state="ready", b_oauth=None):
        """Registry/vault with A and B grants in the given ownership states."""
        b_oauth = b_oauth or oauth("b", 1)
        saved, entries = {}, {}
        for ident, name, state, value in (("max-a", "a", a_state, a_oauth), ("max-b", "b", b_state, b_oauth)):
            entry = {**ownership.entry_for({"claudeAiOauth": value}), "claude_identity": binding(name)}
            saved[ident] = {"origin": renewal.ORIGINS["claude"], "auth": {"claudeAiOauth": value}, "entry": entry,
                            "state": "ready" if state == "legacy" else state, "next_at": entry["expires_at"] - 300,
                            "published": renewal.digest(entry), "updated_at": time.time()}
            if state != "legacy":
                saved[ident]["ownership"] = "cli" if state == "cli_owned" else "private"
            entries[ident] = entry
        self.store.write(renewal.REGISTRY, renewal.seal(saved, KEY), replace=True)
        self.store.write("vault.json", live.encrypt_vault(entries, KEY, SALT), replace=True)

    def records(self):
        return renewal.records(self.store, KEY)

    def sync(self):
        with renewal.vault_lock(self.store):
            return ownership.sync(self.store, KEY, force=True)

    def test_mirror_follows_claude_code_renewal_without_refreshing(self):
        self.install("cli_owned", oauth("a", 1))
        self.write_cli(oauth("a", 2))  # Claude Code rotated A's grant itself.
        self.assertEqual(self.sync(), "max-a")
        item = self.records()["max-a"]
        self.assertEqual(item["state"], "cli_owned")
        self.assertEqual(item["auth"]["claudeAiOauth"]["refreshToken"], "FAKE-A-REFRESH-2")
        self.assertEqual(live.load_vault(self.store)["max-a"]["access_token"], "FAKE-A-ACCESS-2")
        self.renew.assert_not_called()

    def test_switch_returns_outgoing_grant_to_quotahop_renewal(self):
        self.install("cli_owned", oauth("a", 1))
        self.write_cli(oauth("a", 2))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = app.main(["--data-dir", str(Path(self.tmp.name) / "tool"), "--json", "--switch", "max-b"])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertIn("returned to QuotaHop", json.loads(out.getvalue())["message"])
        self.assertNotIn("FAKE-", out.getvalue() + err.getvalue())
        self.assertEqual(self.cli()["refreshToken"], "FAKE-B-REFRESH-1")  # full grant: Claude Code renews B
        saved = self.records()
        self.assertEqual(saved["max-b"]["state"], "cli_owned")
        # A comes back with Claude Code's latest copy, not the stale handed-over one.
        self.assertEqual((saved["max-a"]["state"], saved["max-a"]["auth"]["claudeAiOauth"]["refreshToken"]),
                         ("ready", "FAKE-A-REFRESH-2"))
        self.assertEqual(json.loads(self.creds.read_text())["designOauth"], {"accessToken": "FAKE-DESIGN"})
        self.assertFalse((self.claude / ".oauth_refresh.lock").exists())
        # The worker renews A when due and never touches B, which Claude Code holds.
        fresh = oauth("a", 3)
        self.renew.side_effect = None
        self.renew.return_value = ({"claudeAiOauth": fresh},
                                   {**ownership.entry_for({"claudeAiOauth": fresh}), "claude_identity": binding("a")})
        renewal.cycle(self.store, KEY, now=time.time() + 9 * 3600)
        self.renew.assert_called_once()
        self.assertEqual(self.renew.call_args.args[0]["claudeAiOauth"]["refreshToken"], "FAKE-A-REFRESH-2")
        self.assertEqual(self.records()["max-b"]["state"], "cli_owned")

    def test_manual_login_elsewhere_takes_back_previous_account(self):
        self.install("cli_owned", oauth("a", 1), "suspended", oauth("b", 1))
        self.write_cli(oauth("b", 7))  # User ran /login as B in Claude Code.
        self.assertEqual(self.sync(), "max-b")
        saved = self.records()
        self.assertEqual(saved["max-a"]["state"], "ready")
        self.assertEqual((saved["max-b"]["state"], saved["max-b"]["auth"]["claudeAiOauth"]["refreshToken"]),
                         ("cli_owned", "FAKE-B-REFRESH-7"))

    def test_unattributable_cli_login_never_creates_a_second_owner(self):
        self.install("cli_owned", oauth("a", 1))
        self.write_cli({**oauth("a", 2), "accessToken": "FAKE-UNKNOWN-ACCESS"})
        self.assertIsNone(self.sync())
        self.assertEqual(self.records()["max-a"]["state"], "cli_owned")

    def test_same_refresh_token_in_cli_is_never_renewed_by_quotahop(self):
        shared = oauth("a", 1)
        self.install("ready", shared)
        self.write_cli(shared)
        self.assertEqual(self.sync(), "max-a")
        self.assertEqual(self.records()["max-a"]["state"], "cli_owned")

    def test_legacy_hold_released_only_when_cli_holds_another_account(self):
        self.install("legacy", oauth("a", 1), "legacy", oauth("b", 1))
        self.write_cli(oauth("b", 1))
        self.assertEqual(self.sync(), "max-b")
        saved = self.records()
        self.assertEqual(saved["max-a"]["state"], "ready")
        self.assertEqual(saved["max-b"]["state"], "cli_owned")

    def test_renewal_keeps_binding_without_a_post_refresh_profile_request(self):
        self.install("ready", oauth("a", 1), "cli_owned", oauth("b", 1))
        self.write_cli(oauth("b", 1))
        fresh = oauth("a", 2)
        self.renew.side_effect = None
        self.renew.return_value = ({"claudeAiOauth": fresh}, ownership.entry_for({"claudeAiOauth": fresh}))
        renewal.cycle(self.store, KEY, now=time.time() + 9 * 3600)
        item = self.records()["max-a"]
        self.assertEqual((item["state"], item["entry"]["claude_identity"]), ("ready", binding("a")))
        self.assertEqual(live.load_vault(self.store)["max-a"]["access_token"], "FAKE-A-ACCESS-2")

    def test_unlock_restarts_stopped_worker_only_when_grants_need_it(self):
        with patch.object(renewal, "start") as start:
            self.assertFalse(renewal.ensure_running(self.store, KEY))  # nothing enrolled
            self.install("ready", oauth("a", 1))
            self.assertTrue(renewal.ensure_running(self.store, KEY))
            start.assert_called_once_with(self.store, self.store.path, key=KEY)
            with renewal.worker_lock(self.store):  # already running
                self.assertFalse(renewal.ensure_running(self.store, KEY))
            start.side_effect = live.LiveError("worker_start", "fake")
            self.assertFalse(renewal.ensure_running(self.store, KEY))

    def test_refresh_lock_waits_for_claude_and_never_breaks_its_lock(self):
        (self.claude / ".oauth_refresh.lock").mkdir()
        with patch.object(ownership, "LOCK_WAIT", 0.3), self.assertRaises(live.LiveError) as caught:
            with ownership.refresh_locks(self.claude):
                pass
        self.assertEqual(caught.exception.code, "claude_refreshing")
        self.assertTrue((self.claude / ".oauth_refresh.lock").is_dir())
        (self.claude / ".oauth_refresh.lock").rmdir()
        with ownership.refresh_locks(self.claude):
            self.assertTrue((self.claude / ".oauth_refresh.lock").is_dir())
            self.assertTrue((self.claude.parent / ".claude.lock").is_dir())
        self.assertFalse((self.claude / ".oauth_refresh.lock").exists())
        self.assertFalse((self.claude.parent / ".claude.lock").exists())


if __name__ == "__main__":
    unittest.main()
