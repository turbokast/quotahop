"""Switch only fake Claude files. No real credentials, CLI invocation, or network."""
import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import claude_switch as switch
import claude_usage as app
import live_usage as live
import saved_refresh

KEY, SALT = b"k" * 32, b"s" * 16
TOKEN = "FAKE-SELECTED-CLAUDE-ACCESS"
EMAIL = "person@example.test"


def profile(plan="claude_max"):
    return {"account": {"uuid": "00000000-0000-4000-8000-000000000001", "email": EMAIL,
                        "display_name": "Example", "created_at": "2025-01-01T00:00:00Z"},
            "organization": {"uuid": "00000000-0000-4000-8000-000000000002", "organization_type": plan,
                             "rate_limit_tier": "default_claude_max_20x", "name": "Example org",
                             "has_extra_usage_enabled": False, "billing_type": "stripe_subscription"}}


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = app.Store(self.root / "tool", create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.directory = self.root / "fake-claude"
        self.directory.mkdir(mode=0o700)
        self.creds = self.directory / ".credentials.json"
        self.meta = self.root / ".claude.json"
        self.original_oauth = {"accessToken": "FAKE-OLD-ACCESS", "refreshToken": "FAKE-OLD-REFRESH",
                               "expiresAt": int((time.time() + 3600) * 1000), "refreshTokenExpiresAt": 9999999999999,
                               "scopes": ["user:inference"], "subscriptionType": "team", "clientId": "original-client"}
        self.old_account = {"accountUuid": "old-user", "emailAddress": "old@example.test", "organizationUuid": "old-org"}
        self.original_creds = (b'{\n "designOauth" : { "accessToken":"FAKE-DESIGN-ACCESS", "refreshToken":"FAKE-DESIGN-REFRESH" },\n'
                               b' "claudeAiOauth" : ' + json.dumps(self.original_oauth, indent=2).encode() +
                               b',\n "mcpOAuth": {"server": {"token":"FAKE-MCP-TOKEN"}}, "futureField": [1,  2]\n}\n')
        self.original_meta = (b'{\n "theme":"dark", "oauthAccount": ' + json.dumps(self.old_account).encode() +
                              b', "projects" : {"/fake/project": {"hasTrustDialogAccepted":true}}, "settings":{ "leave" : "alone" }\n}\n')
        self.creds.write_bytes(self.original_creds)
        self.creds.chmod(0o600)
        self.meta.write_bytes(self.original_meta)
        self.meta.chmod(0o664)
        self.canary = self.directory / "settings.json"
        self.canary.write_text('{"permissions":{"defaultMode":"plan"}}')
        self.canary_before = self.canary.stat()
        self.accounts = [{"id": "max-1", "name": EMAIL, "plan": "max_20x"},
                         {"id": "team-1", "name": EMAIL, "plan": "team_premium"},
                         {"id": "codex-1", "name": "Codex", "plan": "codex"}]
        self.entry = {"access_token": TOKEN, "expires_at": time.time() + 3600, "identity": {"email": EMAIL}}
        self.entries = {"max-1": self.entry, "team-1": {**self.entry, "access_token": TOKEN + "-TEAM"}}
        self.store.write("accounts.json", {"accounts": self.accounts})
        self.save_vault()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {}, clear=True))
        stack.enter_context(patch.object(switch, "paths", return_value=(self.creds, self.meta)))
        stack.enter_context(patch.object(live.vault_cache, "get", return_value=KEY))
        self.network = stack.enter_context(patch.object(switch.ProfileClient, "request", return_value=profile()))
        for target in ("subprocess.Popen", "os.system", "os.fork", "live_usage.read_access_token", "live_usage.passphrase"):
            stack.enter_context(patch(target, side_effect=AssertionError(target)))

    def save_vault(self):
        self.store.write("vault.json", live.encrypt_vault(self.entries, KEY, SALT), replace=True)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        before_env = dict(os.environ)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = app.main(["--data-dir", str(self.root / "tool"), "--json", *args])
        self.assertEqual(dict(os.environ), before_env)
        self.assertNotIn("FAKE-", out.getvalue() + err.getvalue())
        return code, json.loads(out.getvalue())

    def assert_original(self):
        self.assertEqual(self.creds.read_bytes(), self.original_creds)
        self.assertEqual(self.meta.read_bytes(), self.original_meta)

    def assert_unrelated_unchanged(self):
        self.assertEqual(switch.replace_value(self.creds.read_bytes(), "claudeAiOauth", None),
                         switch.replace_value(self.original_creds, "claudeAiOauth", None))
        self.assertEqual(switch.replace_value(self.meta.read_bytes(), "oauthAccount", None),
                         switch.replace_value(self.original_meta, "oauthAccount", None))
        self.assertEqual(self.creds.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.meta.stat().st_mode & 0o777, 0o664)
        self.assertEqual(self.canary.stat(), self.canary_before)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), [".credentials.json", "settings.json"])
        self.assertFalse(Path(str(self.meta) + ".lock").exists())

    def usage(self, method, url, token, *args):
        self.assertEqual((method, url), ("GET", live.URL))
        remaining, hours = (70, 1) if token == TOKEN else (90, 4)
        now = dt.datetime.now(app.UTC)
        return {"five_hour": {"utilization": 100 - remaining,
                              "resets_at": (now + dt.timedelta(hours=hours)).isoformat()},
                "seven_day": {"utilization": 10, "resets_at": (now + dt.timedelta(days=3)).isoformat()}}

    def test_automatic_switch_uses_live_table_ranking_and_checks_each_claude_once(self):
        with patch.object(live.UsageClient, "request", side_effect=self.usage) as usage:
            code, result = self.cli("--switch-next", "--jobs", "1")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["account_id"], "max-1")
        self.assertEqual(result["selection"], "recommended")
        self.assertTrue(result["changed"])
        self.assertEqual(usage.call_count, 2)
        self.assertEqual({c.args[2] for c in usage.call_args_list}, {TOKEN, TOKEN + "-TEAM"})
        self.network.assert_called_once_with("GET", switch.PROFILE_URL, TOKEN)
        self.assert_unrelated_unchanged()

    def test_automatic_switch_seven_accounts_all_are_checked_once(self):
        self.accounts = [{"id": f"sub-{i}", "name": f"Account {i}",
                          "plan": "max_20x" if i < 2 else "team_premium"} for i in range(7)]
        self.entries = {a["id"]: {**self.entry, "access_token": TOKEN if i == 0 else TOKEN + str(i)}
                        for i, a in enumerate(self.accounts)}
        self.store.write("accounts.json", {"accounts": self.accounts}, replace=True)
        self.save_vault()
        with patch.object(live.UsageClient, "request", side_effect=self.usage) as usage:
            code, result = self.cli("--switch-next", "--jobs", "1")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["account_id"], "sub-0")
        self.assertEqual(usage.call_count, 7)
        self.assertEqual(len({c.args[2] for c in usage.call_args_list}), 7)
        self.assert_unrelated_unchanged()

    def test_automatic_plan_filter_disambiguates_same_email(self):
        self.network.return_value = profile("claude_team")
        with patch.object(live.UsageClient, "request", side_effect=self.usage) as usage:
            code, result = self.cli("--switch-next", "--plan", "team_premium", "--jobs", "1")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["account_id"], "team-1")
        usage.assert_called_once_with("GET", live.URL, TOKEN + "-TEAM")
        self.assert_unrelated_unchanged()

    def test_automatic_preview_and_current_winner_do_not_change_any_file(self):
        before_vault = self.store.read("vault.json")
        with patch.object(live.UsageClient, "request", side_effect=self.usage):
            code, result = self.cli("--switch-next", "--dry-run", "--jobs", "1")
        self.assertEqual(code, 0, result)
        self.assertFalse(result["changed"])
        self.assert_original()
        self.assertEqual(self.store.read("vault.json"), before_vault)
        self.entry["access_token"] = self.original_oauth["accessToken"]
        self.save_vault()
        with patch.object(live.UsageClient, "request", return_value=self.usage("GET", live.URL, TOKEN)):
            code, result = self.cli("--switch-next", "--plan", "max_20x", "--jobs", "1")
        self.assertEqual(code, 0, result)
        self.assertFalse(result["changed"])
        self.assert_original()

    def test_automatic_expired_unknown_near_expiry_or_wrong_scope_not_fetched(self):
        for change in ({"expires_at": None}, {"expires_at": time.time() - 1},
                       {"expires_at": time.time() + 30}, {"scopes": ["user:profile"]}):
            self.entries = {"max-1": {**self.entry, **change}}
            self.save_vault()
            with patch.object(live.UsageClient, "request") as usage:
                self.assertEqual(self.cli("--switch-next", "--jobs", "1")[0], 2)
            usage.assert_not_called()
            self.network.assert_not_called()
            self.assert_original()

    def test_automatic_all_exhausted_unknown_or_errors_never_switch_or_retry(self):
        for raw in ({"five_hour": {"utilization": 100}}, {"seven_day": {"utilization": 100}}, {},
                    live.LiveError("timeout", "Check timed out."), live.LiveError("authentication_error", "Rejected.")):
            with patch.object(live.UsageClient, "request") as usage:
                if isinstance(raw, Exception):
                    usage.side_effect = raw
                else:
                    usage.return_value = raw
                code, result = self.cli("--switch-next", "--jobs", "1")
            self.assertEqual(code, 2, result)
            self.assertEqual(usage.call_count, 2)
            self.network.assert_not_called()
            self.assert_original()
            self.assertFalse((self.root / "tool" / switch.BACKUP).exists())

    def test_automatic_winner_profile_rejection_leaves_auth_untouched(self):
        self.network.side_effect = live.LiveError("authentication_error", "Rejected.")
        with patch.object(live.UsageClient, "request", side_effect=self.usage):
            self.assertEqual(self.cli("--switch-next", "--jobs", "1")[0], 2)
        self.network.assert_called_once()
        self.assert_original()

    def test_automatic_sort_overrides_and_nonclaude_filter_are_refused(self):
        self.bound_entry()
        with patch.object(live.UsageClient, "request") as usage:
            for flags in (("--reverse",), ("--sort", "name"), ("--plan", "codex")):
                self.assertEqual(self.cli("--switch-next", *flags)[0], 2)
        usage.assert_not_called()
        self.network.assert_not_called()
        self.assert_original()

    def bound_entry(self):
        self.entry["claude_identity"] = {"account_uuid": profile()["account"]["uuid"],
                                         "organization_uuid": profile()["organization"]["uuid"]}
        self.save_vault()

    def fresh_entry(self):
        return {"access_token": "FAKE-ALREADY-RENEWED-ACCESS", "expires_at": time.time() + 7200,
                "scopes": ["user:inference", "user:profile"]}

    def test_refresh_syncs_only_server_bound_account_and_leaves_all_cli_files_unchanged(self):
        self.bound_entry()
        fresh = self.fresh_entry()
        before_team = copy.deepcopy(self.entries["team-1"])
        with patch.object(live, "read_access_token", return_value=dict(fresh)) as read:
            entries, notes = saved_refresh.refresh(self.store, self.accounts)
        self.assertEqual(entries["max-1"]["access_token"], fresh["access_token"])
        # Team's profile is deliberately mismatched: it must retain its token.
        self.assertEqual(entries["team-1"], before_team)
        self.assertIn("synchronized", [n["state"] for n in notes])
        self.assertEqual(read.call_count, 2)
        for call in read.call_args_list:
            self.assertEqual(call.kwargs, {"include_identity": False})
        loaded, _, _ = live.unlock_vault(self.store.read("vault.json"), None, key=KEY)
        self.assertEqual(loaded, entries)
        self.assertNotIn("FAKE-", (self.root / "tool/vault.json").read_text())
        self.assertNotIn("refresh_token", str(entries))
        self.assertNotIn("refreshToken", str(entries))
        self.assert_original()
        self.assert_unrelated_unchanged()

    def test_refresh_establishes_binding_once_without_redundant_profile_calls(self):
        self.accounts = self.accounts[:1]
        with patch.object(live, "read_access_token", return_value={k: v for k, v in self.entry.items() if k != "identity"}):
            entries, notes = saved_refresh.refresh(self.store, self.accounts)
            self.assertIn("claude_identity", entries["max-1"])
            self.network.assert_called_once_with("GET", switch.PROFILE_URL, TOKEN)
            envelope = self.store.read("vault.json")
            self.network.reset_mock()
            saved_refresh.refresh(self.store, self.accounts)
            self.network.assert_not_called()
            self.assertEqual(self.store.read("vault.json"), envelope)
        self.assert_original()

    def test_refresh_rejects_same_email_other_organization_and_concurrent_login(self):
        self.bound_entry()
        self.accounts = self.accounts[:1]
        envelope = self.store.read("vault.json")
        bad = profile()
        bad["organization"]["uuid"] = "00000000-0000-4000-8000-000000000099"
        self.network.return_value = bad
        with patch.object(live, "read_access_token", return_value=self.fresh_entry()):
            entries, notes = saved_refresh.refresh(self.store, self.accounts)
        self.assertEqual(entries, self.entries)
        self.assertEqual(self.store.read("vault.json"), envelope)
        self.network.return_value = profile()
        fresh = self.fresh_entry()
        with patch.object(live, "read_access_token", side_effect=[fresh, {**fresh, "access_token": "FAKE-OTHER-LOGIN"}]):
            entries, notes = saved_refresh.refresh(self.store, self.accounts)
        self.assertEqual(self.store.read("vault.json"), envelope)
        self.assert_original()

    def test_refresh_cannot_renew_expired_inactive_unbound_credentials(self):
        self.entries = {"max-1": {**self.entry, "expires_at": time.time() - 1}}
        self.save_vault()
        before = self.store.read("vault.json")
        entries, notes = saved_refresh.refresh(self.store, self.accounts)
        self.assertEqual(entries, self.entries)
        self.assertEqual(self.store.read("vault.json"), before)
        self.network.assert_not_called()
        self.assertEqual(notes[0]["state"], "binding_unavailable")
        self.assert_original()

    def test_refresh_bound_expired_account_can_copy_fresh_matching_cli_token(self):
        self.entry["expires_at"] = time.time() - 1
        self.bound_entry()
        fresh = self.fresh_entry()
        with patch.object(live, "read_access_token", return_value=fresh):
            entries, _ = saved_refresh.refresh(self.store, self.accounts[:1])
        self.assertGreater(entries["max-1"]["expires_at"], time.time())
        self.network.assert_called_once_with("GET", switch.PROFILE_URL, fresh["access_token"])
        self.assert_original()

    def test_refresh_old_current_token_or_profile_failure_retains_saved_copy(self):
        self.bound_entry()
        before = self.store.read("vault.json")
        fresh = {**self.fresh_entry(), "expires_at": time.time() + 120}
        with patch.object(live, "read_access_token", return_value=fresh):
            saved_refresh.refresh(self.store, self.accounts[:1])
            self.assertEqual(self.store.read("vault.json"), before)
            self.network.side_effect = live.LiveError("timeout", "Profile check timed out.")
            saved_refresh.refresh(self.store, self.accounts[:1])
            self.assertEqual(self.store.read("vault.json"), before)
        self.assert_original()

    def test_refresh_cli_checks_usage_once_and_redacts_sync_details(self):
        self.bound_entry()
        fresh = self.fresh_entry()
        with patch.object(live, "read_access_token", return_value=fresh), patch("credential_state.current", return_value={}), \
                patch.object(live.UsageClient, "request", side_effect=self.usage) as usage:
            code, result = self.cli("--refresh", "--jobs", "1")
        self.assertEqual(code, 3, result)  # Uncaptured Codex row remains visible.
        self.assertEqual(usage.call_count, 2)
        self.assertFalse(result["credential_sync"]["oauth_renewal"])
        self.assertEqual(sum(n["state"] == "synchronized" for n in result["credential_sync"]["notes"]), 1)
        self.assertEqual(next(r for r in result["accounts"] if r["id"] == "team-1")["switch_readiness"], "identity_unverified")
        self.assert_original()

    def test_automatic_switch_syncs_bound_fresh_access_before_ranking(self):
        self.bound_entry()
        fresh = self.fresh_entry()
        with patch.object(live, "read_access_token", return_value=fresh), \
                patch.object(live.UsageClient, "request", side_effect=self.usage) as usage:
            code, result = self.cli("--switch-next", "--plan", "max_20x", "--jobs", "1")
        self.assertEqual(code, 0, result)
        usage.assert_called_once_with("GET", live.URL, fresh["access_token"])
        self.assertEqual(json.loads(self.creds.read_bytes())["claudeAiOauth"]["accessToken"], fresh["access_token"])
        self.assert_unrelated_unchanged()

    def test_bound_identity_is_validated_on_vault_unlock_and_manual_switch(self):
        self.bound_entry()
        wrong = profile()
        wrong["organization"]["uuid"] = "00000000-0000-4000-8000-000000000099"
        self.network.return_value = wrong
        self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        self.assert_original()
        self.entry["claude_identity"]["organization_uuid"] = "invalid"
        self.save_vault()
        with self.assertRaises(live.LiveError):
            live.unlock_vault(self.store.read("vault.json"), None, key=KEY)

    def test_switch_preserves_design_mcp_config_bytes_permissions_and_environment(self):
        code, result = self.cli("--switch", EMAIL, "--plan", "max_20x")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["changed"])
        self.assert_unrelated_unchanged()
        oauth = json.loads(self.creds.read_bytes())["claudeAiOauth"]
        self.assertEqual(oauth["accessToken"], TOKEN)
        self.assertIsNone(oauth["refreshToken"])
        self.assertNotIn("refreshTokenExpiresAt", oauth)
        self.assertNotIn("clientId", oauth)
        self.assertEqual(json.loads(self.meta.read_bytes())["oauthAccount"]["organizationUuid"], profile()["organization"]["uuid"])
        self.network.assert_called_once_with("GET", switch.PROFILE_URL, TOKEN)
        backup_path = self.root / "tool" / switch.BACKUP
        self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b"FAKE-", backup_path.read_bytes())

    def test_undo_restores_previous_auth_without_touching_design_or_profile_request(self):
        self.assertEqual(self.cli("--switch", "max-1")[0], 0)
        self.network.reset_mock()
        code, result = self.cli("--undo-switch")
        self.assertEqual(code, 0, result)
        self.assertEqual(json.loads(self.creds.read_bytes())["claudeAiOauth"], self.original_oauth)
        self.assertEqual(json.loads(self.meta.read_bytes())["oauthAccount"], self.old_account)
        self.assert_unrelated_unchanged()
        self.network.assert_not_called()
        self.assertFalse((self.root / "tool" / switch.BACKUP).exists())

    def test_shared_email_requires_plan_and_team_switch_uses_team_profile(self):
        self.assertEqual(self.cli("--switch", EMAIL)[0], 2)
        self.network.assert_not_called()
        self.assert_original()
        self.network.return_value = profile("claude_team")
        code, result = self.cli("--switch", EMAIL, "--plan", "team_premium")
        self.assertEqual(code, 0, result)
        self.assertEqual(json.loads(self.creds.read_bytes())["claudeAiOauth"]["accessToken"], TOKEN + "-TEAM")
        self.assert_unrelated_unchanged()

    def test_wrong_profile_email_or_plan_is_refused_before_any_auth_write(self):
        for raw in (profile("claude_team"), {**profile(), "account": {**profile()["account"], "email": "other@example.test"}},
                    {"account": {}, "organization": {}}, {**profile(), "reflected": TOKEN}):
            self.network.return_value = raw
            self.assertEqual(self.cli("--switch", "max-1")[0], 2)
            self.assert_original()
            self.assertFalse((self.root / "tool" / switch.BACKUP).exists())

    def test_captured_scope_metadata_is_preserved_without_refresh_credentials(self):
        scopes = ["user:inference", "user:profile", "user:mcp_servers", "user:file_upload"]
        self.entry["scopes"] = scopes
        self.save_vault()
        self.assertEqual(self.cli("--switch", "max-1")[0], 0)
        self.assertEqual(json.loads(self.creds.read_bytes())["claudeAiOauth"]["scopes"], scopes)
        self.assert_unrelated_unchanged()

    def test_expired_unknown_and_nonclaude_selections_do_not_contact_network(self):
        for expiry in (time.time() - 1, time.time() + 10, None):
            self.entry["expires_at"] = expiry
            self.save_vault()
            self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        for selector in ("codex-1", "missing", "../vault"):
            self.assertEqual(self.cli("--switch", selector)[0], 2)
        self.network.assert_not_called()
        self.assert_original()

    def test_read_only_profile_failure_preserves_live_auth(self):
        self.network.side_effect = live.LiveError("authentication_error", "Expired or revoked.")
        self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        self.assert_original()
        self.assert_unrelated_unchanged()

    def test_dry_run_and_current_token_noop_never_replace_or_back_up_auth(self):
        self.assertEqual(self.cli("--switch", "max-1", "--dry-run")[0], 0)
        self.assert_original()
        self.assert_unrelated_unchanged()
        self.assertFalse((self.root / "tool" / switch.BACKUP).exists())
        self.entry["access_token"] = self.original_oauth["accessToken"]
        self.save_vault()
        self.assertFalse(self.cli("--switch", "max-1")[1]["changed"])
        self.assert_original()

    def test_locks_owned_by_other_processes_are_never_removed(self):
        for lock in (self.directory / ".storage-write.lock", Path(str(self.meta) + ".lock")):
            lock.mkdir()
            self.assertEqual(self.cli("--switch", "max-1")[0], 2)
            self.assertTrue(lock.exists())
            self.assert_original()
            lock.rmdir()

    def test_environment_override_refuses_without_changing_environment(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "FAKE-OVERRIDE"}):
            self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        self.network.assert_not_called()
        self.assert_original()

    def test_missing_fields_symlinks_and_insecure_credentials_fail_closed(self):
        self.creds.chmod(0o644)
        self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        self.creds.chmod(0o600)
        saved = self.directory / "original"
        self.creds.rename(saved)
        self.creds.symlink_to(saved)
        self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        self.assertEqual(saved.read_bytes(), self.original_creds)
        self.creds.unlink()
        saved.rename(self.creds)
        self.meta.write_text('{"theme":"dark"}')
        self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        self.assertEqual(self.creds.read_bytes(), self.original_creds)

    def test_failed_second_write_rolls_back_first_exactly(self):
        original = switch.JsonFile.write
        def fail_metadata(file, *args, **kwargs):
            if file.path == self.meta:
                raise OSError("fake metadata write failure")
            return original(file, *args, **kwargs)
        with patch.object(switch.JsonFile, "write", fail_metadata):
            self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        self.assert_original()
        self.assert_unrelated_unchanged()

    def test_backup_failure_leaves_original_auth_untouched(self):
        with patch.object(app.Store, "write", side_effect=OSError("fake disk full")):
            self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        self.assert_original()
        self.assert_unrelated_unchanged()

    def test_concurrent_design_write_is_preserved_and_switch_refused(self):
        original = switch.JsonFile.write
        updated = self.original_creds.replace(b"FAKE-DESIGN-ACCESS", b"FAKE-DESIGN-UPDATED")
        def change_design(file, *args, **kwargs):
            if file.path == self.creds:
                self.creds.write_bytes(updated)
            return original(file, *args, **kwargs)
        with patch.object(switch.JsonFile, "write", change_design):
            self.assertEqual(self.cli("--switch", "max-1")[0], 2)
        self.assertEqual(self.creds.read_bytes(), updated)
        self.assertEqual(self.meta.read_bytes(), self.original_meta)

    def test_undo_refuses_to_overwrite_a_subsequent_login(self):
        self.assertEqual(self.cli("--switch", "max-1")[0], 0)
        changed = self.creds.read_bytes().replace(TOKEN.encode(), b"FAKE-THIRD-LOGIN")
        self.creds.write_bytes(changed)
        self.assertEqual(self.cli("--undo-switch")[0], 2)
        self.assertEqual(self.creds.read_bytes(), changed)


class ProfileBoundaryTests(unittest.TestCase):
    def test_readonly_capture_copies_scopes_but_never_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake-credentials.json"
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN,
                "refreshToken": "FAKE-NEVER-COPY-REFRESH", "scopes": ["user:inference", "user:profile"]}}))
            path.chmod(0o600)
            before = path.stat()
            captured = live.read_access_token(path, include_identity=False)
            self.assertEqual(captured["scopes"], ["user:inference", "user:profile"])
            self.assertNotIn("FAKE-NEVER-COPY-REFRESH", json.dumps(captured))
            self.assertEqual(path.stat(), before)

    def test_profile_get_allowlist_is_separate_and_cannot_refresh_or_infer(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps(profile()).encode()
        with patch("live_usage.http.client.HTTPSConnection", return_value=connection) as factory:
            for method, url in (("POST", switch.PROFILE_URL), ("GET", live.URL),
                                ("POST", "https://api.anthropic.com/v1/messages"),
                                ("POST", "https://platform.claude.com/v1/oauth/token"),
                                ("GET", switch.PROFILE_URL + "?token=anything")):
                with self.assertRaises(live.LiveError):
                    switch.ProfileClient().request(method, url, TOKEN)
            with self.assertRaises(live.LiveError):
                live.UsageClient().request("GET", switch.PROFILE_URL, TOKEN)
            factory.assert_not_called()
            self.assertEqual(switch.ProfileClient().request("GET", switch.PROFILE_URL, TOKEN), profile())
        self.assertEqual(connection.request.call_args.args, ("GET", "/api/oauth/profile"))
        connection.close.assert_called_once()
        response.close.assert_called_once()

    def test_json_splice_handles_escapes_nested_keys_and_preserves_surrounding_text(self):
        raw = b'{"other": {"oauthAccount":"nested"}, "oauthAccount" : {"escaped":"} \\\""}, "keep" : [ 1, 2 ]}\n'
        updated = switch.replace_value(raw, "oauthAccount", {"new": True})
        self.assertEqual(updated, b'{"other": {"oauthAccount":"nested"}, "oauthAccount" : {"new": true}, "keep" : [ 1, 2 ]}\n')
        for raw in (b'{"oauthAccount":{},"oauthAccount":{}}', b'{}', b'not json'):
            with self.assertRaises(live.LiveError):
                switch.replace_value(raw, "oauthAccount", {})
