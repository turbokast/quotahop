"""Account labels and selection with fake identities and credentials only."""
import base64
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import account_identity as identity
import claude_usage as app
import live_usage as live
import muse_cli_usage as muse_cli

PASSWORD = b"fake-identity-test-password"
EMAIL = "alice@example.test"
TOKEN = "FAKE-CLAUDE-ACCESS-TOKEN"


def codex_token(email=EMAIL):
    claims = {"https://api.openai.com/profile": {"email": email, "name": "Alice Example"}, "exp": 2000000000}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + payload + ".FAKE"


class IdentityTests(unittest.TestCase):
    def test_jwt_profile_is_display_only_not_a_retained_id_token(self):
        self.assertEqual(identity.codex(codex_token()), {"email": EMAIL, "name": "Alice Example"})
        for token in ("not-a-jwt", "a.bad.c", "a.e30.c", None):
            self.assertEqual(identity.codex(token), {})

    def test_labels_reject_control_characters_secret_shapes_and_reflections(self):
        for name in ("bad\nname", "\x1b[31mbad", "Bearer secret", "dca:example", TOKEN):
            self.assertEqual(identity.extract(name=name, secrets=(TOKEN,)), {})
        self.assertEqual(identity.extract(email="bad email@example.test", name="Alice"), {"name": "Alice"})
        self.assertEqual(identity.extract(email=EMAIL, name="Alice"), {"email": EMAIL, "name": "Alice"})

    def test_legacy_claude_identity_only_attaches_to_matching_token(self):
        entries = {"team-1": {"access_token": TOKEN}, "team-2": {"access_token": TOKEN+"2"}}
        current = {"access_token": TOKEN, "identity": {"email": EMAIL}}
        with patch.object(live, "read_access_token", return_value=current) as read:
            enriched = identity.for_report(entries)
        self.assertEqual(enriched["team-1"]["identity"], {"email": EMAIL})
        self.assertNotIn("identity", enriched["team-2"])
        self.assertNotIn("identity", entries["team-1"])
        read.assert_called_once()
        with patch.object(live, "read_access_token") as read:
            identity.for_report({"team-1": current})
            identity.for_report({"codex-1": {"provider": "codex", "access_token": codex_token()}})
        read.assert_not_called()

    def test_saved_identities_label_both_success_and_auth_errors(self):
        accounts = [{"id": "team-1", "name": "Team 1", "plan": "team_premium"},
                    {"id": "codex-1", "name": "Codex 1", "plan": "codex"}]
        entries = {"team-1": {"access_token": TOKEN, "identity": {"email": EMAIL}},
                   "codex-1": {"provider": "codex", "access_token": codex_token("bob@example.test")}}
        with patch.object(live, "fetch", side_effect=[live.LiveError("authentication_error", "Expired."), {"rate_limit": None}]):
            data = app.report(accounts, MagicMock(), dt.datetime.now(app.UTC), entries)
        self.assertEqual([row["name"] for row in data["accounts"]], [EMAIL, "bob@example.test"])
        self.assertEqual(data["accounts"][0]["status"], "error")
        rendered = app.render(data, dt.datetime.now(app.UTC))
        self.assertNotIn("Team 1", rendered)
        self.assertNotIn("Codex 1", rendered)

    def test_muse_report_uses_response_identity_in_combined_report(self):
        raw = {"subs_usage": {"weekly": {"used_percent": 22}}, "subs_tier_name": "High Usage", "identity": {"email": EMAIL}}
        with patch.object(muse_cli, "read_credentials", return_value=("dca:FAKE-TOKEN", "FAKE-API-KEY")), patch.object(muse_cli.MuseCliClient, "request", return_value=raw):
            data = app.report([{"id": "muse-1", "name": "Muse Code 1", "plan": "muse_code", "source": "cli_current"}], MagicMock(), dt.datetime.now(app.UTC))
        self.assertEqual(data["accounts"][0]["name"], EMAIL)
        self.assertEqual(data["accounts"][0]["id"], "muse-1")


class CaptureIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = app.Store(self.root / "data", create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.accounts = app.default_accounts() + [{"id": "codex-1", "name": "Codex 1", "plan": "codex"}]
        self.store.write("accounts.json", {"accounts": self.accounts})
        self.source = self.root / ".credentials.json"
        self.source.write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "refreshToken": "FAKE-NEVER-COPY"}}))
        self.source.chmod(0o600)
        self.metadata = self.root / ".claude.json"
        self.metadata.write_text(json.dumps({"oauthAccount": {"emailAddress": EMAIL, "displayName": "Alice Example"}}))
        self.metadata.chmod(0o664)
        for target, result in (("vault_cache.get", None), ("vault_cache.put", True),
                               ("live_usage.passphrase", PASSWORD)):
            mocked = patch(target, return_value=result)
            mocked.start()
            self.addCleanup(mocked.stop)

    def cli(self, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()), patch("credential_state.current", return_value={}):
            code = app.main(["--jobs", "1", "--data-dir", str(self.root / "data"), *args])
        return code, stdout.getvalue()

    def test_claude_identity_reads_are_explicitly_read_only_and_do_not_save_refresh(self):
        before = [(p.stat().st_atime_ns, p.stat().st_mtime_ns, p.stat().st_mode) for p in (self.source, self.metadata)]
        with patch("socket.socket", side_effect=AssertionError("No network")), patch("subprocess.Popen", side_effect=AssertionError("No Claude CLI")):
            entry = live.read_access_token(self.source)
        self.assertEqual(entry["identity"], {"email": EMAIL, "name": "Alice Example"})
        after = [(p.stat().st_atime_ns, p.stat().st_mtime_ns, p.stat().st_mode) for p in (self.source, self.metadata)]
        self.assertEqual(before, after)
        self.assertNotIn("FAKE-NEVER-COPY", json.dumps(entry))

    def test_account_switch_during_metadata_read_is_refused(self):
        with patch.object(live, "read_access_token", return_value={"access_token": "DIFFERENT-LOGIN"}):
            with self.assertRaises(live.LiveError) as caught:
                identity.claude(self.source, TOKEN)
        self.assertEqual(caught.exception.code, "account_changed")

    def test_capture_by_provider_reuses_template_and_recaptures_by_email(self):
        code, out = self.cli("--capture", "claude", "--plan", "team_premium", "--credential-file", str(self.source), "--json")
        self.assertEqual(code, 0, out)
        result = json.loads(out)
        self.assertEqual(result["account_name"], EMAIL)
        self.assertEqual(result["account_id"], "team-1")
        accounts = self.store.read("accounts.json")["accounts"]
        self.assertEqual(len(accounts), 8)
        self.assertEqual(next(a for a in accounts if a["id"] == "team-1")["name"], EMAIL)
        code, out = self.cli("--capture", EMAIL.upper(), "--credential-file", str(self.source))
        self.assertEqual(code, 0, out)
        self.assertIn(EMAIL, out)
        entries, _, _ = live.unlock_vault(self.store.read("vault.json"), PASSWORD)
        self.assertEqual(set(entries), {"team-1"})
        self.assertEqual(entries["team-1"]["identity"]["email"], EMAIL)

    def test_manual_email_when_metadata_missing_and_name_fallback(self):
        self.metadata.unlink()
        code, out = self.cli("--capture", EMAIL, "--plan", "max_20x", "--credential-file", str(self.source))
        self.assertEqual(code, 0, out)
        self.assertIn(EMAIL, out)
        entries, _, _ = live.unlock_vault(self.store.read("vault.json"), PASSWORD)
        self.assertEqual(entries["max-1"]["identity"], {"email": EMAIL})

    def test_wrong_email_refused_before_writing_config_or_vault(self):
        before = self.store.read("accounts.json")
        code, out = self.cli("--capture", "other@example.test", "--plan", "team_premium", "--credential-file", str(self.source), "--json")
        self.assertEqual(code, 2)
        self.assertIn("differs", out)
        self.assertFalse((self.root / "data/vault.json").exists())
        self.assertEqual(self.store.read("accounts.json"), before)

    def test_legacy_capture_renames_without_changing_storage_id(self):
        code, out = self.cli("--capture", "team-1", "--credential-file", str(self.source), "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["account_name"], EMAIL)
        self.assertEqual(json.loads(out)["account_id"], "team-1")

    def test_different_account_cannot_overwrite_named_capture(self):
        self.assertEqual(self.cli("--capture", "team-1", "--credential-file", str(self.source))[0], 0)
        self.metadata.write_text(json.dumps({"oauthAccount": {"emailAddress": "bob@example.test"}}))
        self.source.write_text(json.dumps({"accessToken": "FAKE-BOB-ACCESS-TOKEN"}))
        before = self.store.read("vault.json")
        code, out = self.cli("--capture", "team-1", "--credential-file", str(self.source), "--json")
        self.assertEqual(code, 2)
        self.assertEqual(before, self.store.read("vault.json"))
        self.assertEqual(self.cli("--capture", "claude", "--plan", "team_premium", "--credential-file", str(self.source))[0], 0)
        accounts = self.store.read("accounts.json")["accounts"]
        self.assertEqual([a["name"] for a in accounts if a["id"] in ("team-1", "team-2")], [EMAIL, "bob@example.test"])

    def test_codex_capture_auto_identity_and_old_token_display_no_id_token_saved(self):
        self.source.write_text(json.dumps({"tokens": {"access_token": codex_token(), "account_id": "workspace-a", "id_token": "FAKE-NEVER-COPY-ID", "refresh_token": "FAKE-NEVER-COPY-REFRESH"}}))
        code, out = self.cli("--capture", "codex", "--credential-file", str(self.source), "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["account_name"], EMAIL)
        entries, _, _ = live.unlock_vault(self.store.read("vault.json"), PASSWORD)
        self.assertEqual(entries["codex-1"]["identity"]["email"], EMAIL)
        self.assertNotIn("FAKE-NEVER-COPY", json.dumps(entries))

    def test_recapture_legacy_config_by_email_shown_in_report(self):
        self.source.write_text(json.dumps({"tokens": {"access_token": codex_token(), "account_id": "workspace-a"}}))
        live.capture(self.store, "codex-1", self.source, "codex")
        self.assertEqual(self.store.read("accounts.json")["accounts"][-1]["name"], "Codex 1")
        code, out = self.cli("--capture", EMAIL, "--credential-file", str(self.source), "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["account_name"], EMAIL)
        self.assertEqual(json.loads(out)["account_id"], "codex-1")

    def test_ambiguous_name_requires_provider_and_same_email_different_workspace_not_merged(self):
        entry = {"provider": "codex", "access_token": codex_token(), "account_id": "a", "identity": {"email": EMAIL}}
        accounts = [{"id": "codex-1", "name": EMAIL, "plan": "codex"}, {"id": "codex-2", "name": EMAIL, "plan": "codex"}]
        entries = {"codex-1": entry, "codex-2": {**entry, "account_id": "b"}}
        with self.assertRaises(app.UsageError):
            identity.choose(accounts, entries, EMAIL, "codex", entry)
        selected, _ = identity.choose(accounts, entries, "codex", "codex", {**entry, "account_id": "b"})
        self.assertEqual(selected["id"], "codex-2")

    def test_missing_identity_does_not_guess_new_account_name(self):
        self.metadata.unlink()
        code, out = self.cli("--capture", "claude", "--plan", "team_premium", "--credential-file", str(self.source), "--json")
        self.assertEqual(code, 2)
        self.assertIn("No account name/email", out)
        code, out = self.cli("--capture", "claude", "--plan", "team_premium", "--name", "Alice Work", "--credential-file", str(self.source))
        self.assertEqual(code, 0, out)
        self.assertIn("Alice Work", out)

    def test_failed_vault_write_cannot_relabel_existing_account(self):
        original = self.store.read("accounts.json")
        original_write = self.store.write
        def write(name, *args, **kwargs):
            if name == "vault.json":
                raise OSError("Simulated disk failure")
            return original_write(name, *args, **kwargs)
        with patch.object(self.store, "write", side_effect=write), self.assertRaises(OSError):
            live.capture(self.store, "team-1", self.source, named=True)
        self.assertEqual(self.store.read("accounts.json"), original)

    def test_same_email_max_and_team_are_independent_and_recapture_is_scoped(self):
        with patch("socket.socket", side_effect=AssertionError("No network")):
            for plan, token in (("max_20x", TOKEN), ("team_premium", TOKEN + "-TEAM")):
                self.source.write_text(json.dumps({"accessToken": token}))
                self.assertEqual(self.cli("--capture", EMAIL, "--plan", plan,
                                          "--credential-file", str(self.source))[0], 0)
            before, _, _ = live.unlock_vault(self.store.read("vault.json"), PASSWORD)
            self.assertEqual(set(before), {"max-1", "team-1"})
            self.assertEqual(before["max-1"]["access_token"], TOKEN)
            self.assertEqual(before["team-1"]["access_token"], TOKEN + "-TEAM")
            for selector, plan, ident in (("claude", "max_20x", "max-1"),
                                          (EMAIL, "team_premium", "team-1"),
                                          ("max-1", None, "max-1")):
                self.source.write_text(json.dumps({"accessToken": TOKEN + "-NEW-" + ident}))
                args = ["--capture", selector, "--credential-file", str(self.source)]
                if plan:
                    args.extend(["--plan", plan])
                self.assertEqual(self.cli(*args)[0], 0)
                after, _, _ = live.unlock_vault(self.store.read("vault.json"), PASSWORD)
                other = "team-1" if ident == "max-1" else "max-1"
                self.assertEqual(after[other], before[other])
                self.assertEqual(after[ident]["access_token"], TOKEN + "-NEW-" + ident)
                before = after
        accounts = self.store.read("accounts.json")["accounts"]
        self.assertEqual([(a["id"], a["plan"]) for a in accounts if a["name"] == EMAIL],
                         [("max-1", "max_20x"), ("team-1", "team_premium")])

    def test_generated_ids_include_subscription_type_without_template_slots(self):
        self.store.write("accounts.json", {"accounts": []}, replace=True)
        ids = []
        for plan in ("max_20x", "team_premium"):
            self.source.write_text(json.dumps({"accessToken": TOKEN + plan}))
            code, out = self.cli("--capture", "claude", "--plan", plan,
                                 "--credential-file", str(self.source), "--json")
            self.assertEqual(code, 0, out)
            ids.append(json.loads(out)["account_id"])
        self.assertEqual(len(set(ids)), 2)
        before = self.store.read("vault.json")
        for selector in (EMAIL, "claude"):
            self.assertEqual(self.cli("--capture", selector, "--credential-file", str(self.source))[0], 2)
            self.assertEqual(self.store.read("vault.json"), before)

    def test_same_token_cannot_masquerade_as_two_subscriptions(self):
        self.assertEqual(self.cli("--capture", "claude", "--plan", "max_20x", "--credential-file", str(self.source))[0], 0)
        before = (self.store.read("vault.json"), self.store.read("accounts.json"))
        self.assertEqual(self.cli("--capture", "claude", "--plan", "team_premium", "--credential-file", str(self.source))[0], 2)
        self.assertEqual((self.store.read("vault.json"), self.store.read("accounts.json")), before)

    def test_explicit_id_cannot_change_saved_subscription_type(self):
        self.assertEqual(self.cli("--capture", "max-1", "--credential-file", str(self.source))[0], 0)
        before = (self.store.read("vault.json"), self.store.read("accounts.json"))
        self.source.write_text(json.dumps({"accessToken": TOKEN + "-TEAM"}))
        self.assertEqual(self.cli("--capture", "max-1", "--plan", "team_premium", "--credential-file", str(self.source))[0], 2)
        self.assertEqual((self.store.read("vault.json"), self.store.read("accounts.json")), before)
