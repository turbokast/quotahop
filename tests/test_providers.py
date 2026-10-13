"""Provider isolation and quota tests. All credentials and HTTP are fake."""
import base64
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import claude_usage as app
import live_usage as live

TOKEN = "FAKE-CODEX-ACCESS-ONLY-12345"
REFRESH = "FAKE-REFRESH-NEVER-COPY"
PASSWORD = b"fake-private-vault-passphrase"


def codex_payload():
    return {"plan_type": "pro", "rate_limit": {
        "primary_window": {"used_percent": 23, "limit_window_seconds": 18000, "reset_at": 1900000000},
        "secondary_window": {"used_percent": 64, "limit_window_seconds": 604800, "reset_at": 1900600000}},
        "credits": {"has_credits": True, "unlimited": False, "balance": "12.50"}}


def entry():
    return {"provider": "codex", "access_token": TOKEN, "expires_at": None, "account_id": "fake-workspace"}


class ProviderTests(unittest.TestCase):
    def test_codex_windows_credits_and_model_allowances(self):
        raw = codex_payload()
        raw["additional_rate_limits"] = [{"limit_name": "model allowance", "rate_limit": raw["rate_limit"]}]
        raw["code_review_rate_limit"] = raw["rate_limit"]
        parsed = app.parse_codex(raw)
        self.assertEqual(parsed["five_hour"]["utilization"], 23)
        self.assertEqual(parsed["seven_day"]["utilization"], 64)
        self.assertEqual(parsed["credits"]["balance"], "12.50")
        self.assertEqual(len(parsed["model_windows"]), 4)
        self.assertEqual(app.timestamp(parsed["five_hour"]["resets_at"]).timestamp(), 1900000000)

    def test_arbitrary_duration_is_not_mislabeled_five_hours(self):
        raw = codex_payload()
        raw["rate_limit"]["primary_window"]["limit_window_seconds"] = 900
        parsed = app.parse_codex(raw)
        self.assertIsNone(parsed["five_hour"]["utilization"])
        self.assertEqual(next(iter(parsed["model_windows"])), "Codex primary_window (15m)")

    def test_missing_data_and_invalid_shapes(self):
        self.assertIsNone(app.parse_codex({"rate_limit": None})["five_hour"]["utilization"])
        self.assertEqual(app.parse_codex({"rate_limit": {"primary_window": {"used_percent": 0, "limit_window_seconds": 18000}}})["five_hour"]["utilization"], 0)
        for raw in ({}, {"total_tokens": 1234}, {"rate_limit": []}, {"rate_limit": {"primary_window": 1}},
                    {"additional_rate_limits": "wrong"}, {"credits": {"balance": "Bearer secret"}},
                    {"credits": {"has_credits": 1}}, {"rate_limit": {"primary_window": {"used_percent": 101}}}):
            with self.subTest(raw=raw), self.assertRaises(app.UsageError):
                app.parse_codex(raw)

    def test_provider_mismatch_and_muse_blocked_before_network(self):
        with patch.object(live.UsageClient, "request") as network:
            for provider, saved in (("claude", entry()), ("codex", {"access_token": TOKEN, "expires_at": None}), ("muse_code", entry())):
                with self.assertRaises(live.LiveError):
                    live.fetch(saved, set(), provider)
            network.assert_not_called()

    def test_codex_exact_method_path_routing_and_connection_close(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps(codex_payload()).encode()
        with patch("live_usage.http.client.HTTPSConnection", return_value=connection) as factory:
            live.fetch(entry(), set(), "codex")
        self.assertEqual(factory.call_args.args, ("chatgpt.com",))
        connection.request.assert_called_once()
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("GET", "/backend-api/wham/usage"))
        self.assertEqual(kwargs["headers"]["ChatGPT-Account-Id"], "fake-workspace")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn("anthropic-beta", kwargs["headers"])
        self.assertNotIn("body", kwargs)
        connection.close.assert_called_once()
        response.close.assert_called_once()

    def test_inference_mint_refresh_redirect_variants_and_bad_headers_blocked(self):
        with patch("live_usage.http.client.HTTPSConnection") as network:
            for method, url in (("POST", live.CODEX_URL), ("POST", "https://api.meta.ai/muse-code/key"),
                                ("GET", "https://api.meta.ai/muse-code/usage"),
                                ("POST", "https://api.openai.com/v1/responses"),
                                ("GET", "https://chatgpt.com/backend-api/codex/responses"),
                                ("POST", "https://auth.openai.com/oauth/token"),
                                ("GET", live.CODEX_URL + "?token=secret"),
                                ("GET", live.CODEX_URL + "/")):
                with self.assertRaises(live.LiveError):
                    live.UsageClient().request(method, url, TOKEN, "fake-workspace")
            for ident in (None, "a\r\nAuthorization:secret"):
                with self.assertRaises(live.LiveError):
                    live.UsageClient().request("GET", live.CODEX_URL, TOKEN, ident)
            network.assert_not_called()

    def test_codex_error_responses_no_refresh_or_retry(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        with patch("live_usage.http.client.HTTPSConnection", return_value=connection):
            for status in (401, 403, 429, 302, 500):
                connection.reset_mock()
                response.status = status
                response.read.return_value = TOKEN.encode()
                with self.assertRaises(live.LiveError) as caught:
                    live.fetch(entry(), set(), "codex")
                self.assertNotIn(TOKEN, str(caught.exception))
                connection.request.assert_called_once()
                response.read.assert_not_called()
                connection.close.assert_called_once()

    def test_response_identity_mismatch_and_reflected_token_redacted(self):
        for raw in ({**codex_payload(), "account_id": "wrong"}, {**codex_payload(), "plan_type": TOKEN}):
            with patch.object(live.UsageClient, "request", return_value=raw):
                with self.assertRaises(live.LiveError) as caught:
                    live.fetch(entry(), set(), "codex")
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_muse_observation_preserves_original_age_and_never_becomes_live(self):
        raw = {"usage": {"observedAtMs": 1700000000000, "tier": "power",
                         "window": {"usedPercent": 22, "windowDurationMins": 300, "resetsAtMs": 1700005000000},
                         "weekly": {"usedPercent": 66, "resetsAtMs": 1700500000000}}}
        snapshot = app.muse_snapshot(raw)
        self.assertEqual(app.timestamp(snapshot["captured_at"]).timestamp(), 1700000000)
        self.assertEqual(snapshot["usage"]["five_hour"]["utilization"], 22)
        self.assertEqual(app.timestamp(snapshot["usage"]["seven_day"]["resets_at"]).timestamp(), 1700500000)
        for value in ({}, {"usage": None}, {"usage": {"observedAtMs": 1700000000000}},
                      {"usage": {"observedAtMs": 1700000000000, "window": {"windowDurationMins": 15}}}):
            with self.assertRaises(app.UsageError):
                app.muse_snapshot(value)


class ProviderStorageTests(unittest.TestCase):
    def setUp(self):
        for method, result in (("get", None), ("put", True), ("clear", True)):
            cache = patch("vault_cache." + method, return_value=result)
            cache.start()
            self.addCleanup(cache.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "data"
        self.source = self.root / "auth.json"
        self.source.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": TOKEN, "refresh_token": REFRESH, "id_token": "never-copy-id-token", "account_id": "fake-workspace"}}))
        self.source.chmod(0o600)
        self.assertEqual(self.cli("--setup")[0], 0)

    def cli(self, *args, stdin=b""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("credential_state.current", return_value={}), patch("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin))):
            code = app.main(["--jobs", "1", "--data-dir", str(self.directory), *args])
        return code, out.getvalue(), err.getvalue()

    def test_capture_codex_private_read_only_and_backward_compatible_vault(self):
        self.assertEqual(self.cli("--add", "codex-1", "--plan", "codex", "--name", "My Codex")[0], 0)
        os.utime(self.source, ns=(1000000000, 2000000000))
        before = self.source.stat()
        with patch("live_usage.passphrase", return_value=PASSWORD), patch("socket.socket") as network:
            code, _, _ = self.cli("--capture", "codex-1", "--credential-file", str(self.source))
            self.assertEqual(code, 0)
            network.assert_not_called()
        after = self.source.stat()
        self.assertEqual((before.st_atime_ns, before.st_mtime_ns, before.st_ino, before.st_mode),
                         (after.st_atime_ns, after.st_mtime_ns, after.st_ino, after.st_mode))
        with app.Store(self.directory) as store:
            entries, key, salt = live.unlock_vault(store.read("vault.json"), PASSWORD)
            self.assertEqual(entries["codex-1"], {**entry(), "identity": {"name": "My Codex"}})
            entries["max-1"] = {"access_token": "FAKE-OLD-CLAUDE-TOKEN", "expires_at": None}
            self.assertEqual(live.unlock_vault(live.encrypt_vault(entries, key, salt), PASSWORD)[0], entries)
        raw = (self.directory / "vault.json").read_bytes()
        for secret in (TOKEN, REFRESH, "never-copy-id-token", "fake-workspace"):
            self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(REFRESH, json.dumps(entries))

    def test_codex_jwt_expiry_and_api_key_refusal(self):
        claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() - 10}).encode()).decode().rstrip("=")
        self.source.write_text(json.dumps({"tokens": {"access_token": "eyJhbGciOiJub25lIn0." + claims + ".FAKE", "account_id": "fake-workspace"}}))
        saved = live.read_access_token(self.source, "codex")
        with patch.object(live.UsageClient, "request") as network, self.assertRaises(live.LiveError):
            live.fetch(saved, set(), "codex")
        network.assert_not_called()
        self.source.write_text(json.dumps({"OPENAI_API_KEY": "FAKE-API-KEY"}))
        with self.assertRaises(live.LiveError):
            live.read_access_token(self.source, "codex")

    def test_add_multi_provider_report_once_no_processes_or_polling(self):
        for ident, plan in (("codex-1", "codex"), ("codex-2", "codex"), ("muse-1", "muse_code")):
            self.assertEqual(self.cli("--add", ident, "--plan", plan)[0], 0)
        entries = {"codex-1": entry(), "codex-2": {**entry(), "access_token": TOKEN + "2", "account_id": "fake-other"}}
        with contextlib.ExitStack() as stack:
            for target in ("subprocess.Popen", "os.fork", "os.system", "threading.Thread.start", "time.sleep"):
                stack.enter_context(patch(target, side_effect=AssertionError("Background work prohibited")))
            stack.enter_context(patch.object(live, "load_vault", return_value=entries))
            network = stack.enter_context(patch.object(live.UsageClient, "request", return_value=codex_payload()))
            code, out, _ = self.cli("--json")
        self.assertEqual(code, 3)
        data = json.loads(out)
        self.assertEqual(len(data["accounts"]), 10)
        self.assertEqual(data["live_accounts"], 2)
        self.assertEqual(network.call_count, 2)
        self.assertEqual([call.args[3] for call in network.call_args_list], ["fake-workspace", "fake-other"])
        self.assertEqual(data["accounts"][-1]["status"], "not_configured")
        self.assertEqual(next(a for a in data["accounts"] if a["provider"] == "codex")["reported_plan"], "pro")
        self.assertIn("Codex pro", app.render(data, dt.datetime.now(app.UTC)))
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("fake-workspace", out)

    def test_muse_capture_does_not_read_credentials_and_import_stays_historical(self):
        self.cli("--add", "muse-1", "--plan", "muse_code")
        with patch.object(live, "read_access_token") as read, patch("socket.socket") as network:
            self.assertEqual(self.cli("--capture", "muse-1", "--credential-file", str(self.source))[0], 2)
            read.assert_not_called()
            network.assert_not_called()
        exported = {"usage": {"observedAtMs": 1700000000000, "weekly": {"usedPercent": 44}}}
        self.assertEqual(self.cli("--import-muse", "muse-1", stdin=json.dumps(exported).encode())[0], 0)
        _, out, _ = self.cli("--json")
        muse = json.loads(out)["accounts"][-1]
        self.assertEqual(muse["status"], "snapshot")
        self.assertFalse(muse["live"])
        self.assertEqual(muse["usage"]["seven_day"]["utilization"], 44)

    def test_bad_add_preserves_config_and_muse_stores_protected(self):
        before = (self.directory / "accounts.json").read_bytes()
        for args in (("--add", "max-1", "--plan", "codex"), ("--add", "../oops", "--plan", "codex"), ("--add", "codex-1")):
            self.assertEqual(self.cli(*args)[0], 2)
            self.assertEqual((self.directory / "accounts.json").read_bytes(), before)
        with self.assertRaises(app.UsageError):
            app.Store(self.root / "muse", create=True)


if __name__ == "__main__":
    unittest.main()
