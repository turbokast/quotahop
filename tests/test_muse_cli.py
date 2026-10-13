import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import signal
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import claude_usage as app
import live_usage as live
import muse_cli_usage as muse

TOKEN = "dca:FAKE-DEVICE-CREDENTIAL-FOR-TESTS"
KEY = "FAKE-MUSE-API-KEY-FOR-TESTS"
NOW = dt.datetime(2026, 10, 12, tzinfo=app.UTC)


def payload():
    return {"api_key": KEY, "is_subs_active": True, "subs_tier_name": "Muse Code High Usage",
            "subs_usage": {"window": {"used_percent": 1, "window_duration_mins": 300, "resets_at": 1791852179},
                           "weekly": {"used_percent": 0, "resets_at": 1792368000}}}


class ParsingTests(unittest.TestCase):
    def test_usage_and_resets(self):
        parsed, plan = muse.parse({k: v for k, v in payload().items() if k.startswith("subs_")})
        self.assertEqual(plan, "Muse Code High Usage")
        self.assertEqual(parsed["five_hour"]["utilization"], 1)
        self.assertEqual(parsed["seven_day"]["utilization"], 0)
        self.assertEqual(parsed["five_hour"]["resets_at"], "2026-10-13T00:42:59+00:00")

    def test_absent_does_not_become_zero(self):
        for value in ({}, {"subs_usage": None}, {"subs_usage": {}}):
            with self.assertRaises(live.LiveError) as caught:
                muse.parse(value)
            self.assertEqual(caught.exception.code, "usage_unavailable")
        result, _ = muse.parse({"subs_usage": {"weekly": {"used_percent": 12}}})
        self.assertIsNone(result["five_hour"]["utilization"])
        self.assertIsNone(result["seven_day"]["resets_at"])

    def test_alternate_duration_is_separate(self):
        result, _ = muse.parse({"subs_usage": {"window": {"used_percent": 15, "window_duration_mins": 60}}})
        self.assertIsNone(result["five_hour"]["utilization"])
        self.assertEqual(result["model_windows"]["Muse subscription (60m)"]["utilization"], 15)

    def test_overage_preserved_for_both_windows_and_alternate_duration(self):
        for minutes in (300, 60):
            result, _ = muse.parse({"subs_usage": {
                "window": {"used_percent": 101.25, "window_duration_mins": minutes, "resets_at": 1791852179},
                "weekly": {"used_percent": 137, "resets_at": 1792368000}}})
            short = result["five_hour"] if minutes == 300 else result["model_windows"]["Muse subscription (60m)"]
            self.assertEqual(short["utilization"], 101.25)
            self.assertEqual(short["resets_at"], "2026-10-13T00:42:59+00:00")
            self.assertEqual(result["seven_day"]["utilization"], 137)

    def test_malformed_values_and_secrets(self):
        for value in (True, -1, "1", float("nan"), float("inf"), float("-inf"), []):
            with self.assertRaises(app.UsageError):
                muse.parse({"subs_usage": {"weekly": {"used_percent": value}}})
        with self.assertRaises(app.UsageError):
            muse.parse({"subs_usage": {"api_key": KEY}})


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = json.dumps(payload()).encode()
        self.patcher = patch("muse_cli_usage.http.client.HTTPSConnection", return_value=self.connection)
        self.factory = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def request(self):
        return muse.MuseCliClient().request("POST", muse.URL, TOKEN, KEY)

    def test_single_bounded_bootstrap_only_with_onboarding_disabled(self):
        result = self.request()
        self.assertEqual(set(result), {"subs_usage", "subs_tier_name"})
        self.assertNotIn(KEY, json.dumps(result))
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("POST", "/muse-code/key"))
        self.assertEqual(json.loads(kwargs["body"]), {"onboard": False})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn(KEY, json.dumps(kwargs["headers"]))
        self.assertNotIn(KEY.encode(), kwargs["body"])
        self.connection.request.assert_called_once()
        self.connection.close.assert_called_once()
        self.response.close.assert_called_once()
        self.assertEqual(self.factory.call_args.args, ("api.meta.ai",))
        context = self.factory.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIsNone(context.keylog_filename)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_no_other_method_host_path_login_refresh_or_inference(self):
        for method, url in (("GET", muse.URL), ("PUT", muse.URL),
                            ("POST", muse.URL + "?onboard=true"),
                            ("POST", "https://api.meta.ai/v1/responses"),
                            ("POST", "https://api.meta.ai/v1/chat/completions"),
                            ("POST", "https://api.meta.ai/oauth/token"),
                            ("POST", "https://auth.meta.com/token"),
                            ("POST", "https://api.meta.ai.evil.invalid/muse-code/key")):
            with self.assertRaises(live.LiveError):
                muse.MuseCliClient().request(method, url, TOKEN, KEY)
        self.factory.assert_not_called()

    def test_errors_no_retry_no_credential_adoption(self):
        for status in (401, 403, 429, 302, 307, 500):
            self.connection.reset_mock()
            self.response.status = status
            self.response.read.return_value = TOKEN.encode()
            with self.assertRaises(live.LiveError) as caught:
                self.request()
            self.assertNotIn(TOKEN, str(caught.exception))
            self.connection.request.assert_called_once()
            self.response.read.assert_not_called()
            self.connection.close.assert_called_once()

    def test_timeout_network_tls(self):
        for error in (TimeoutError(KEY), ConnectionError(KEY), ssl.SSLError(KEY)):
            self.connection.reset_mock()
            self.connection.getresponse.side_effect = error
            with self.assertRaises(live.LiveError) as caught:
                self.request()
            self.assertNotIn(KEY, str(caught.exception))
            self.connection.request.assert_called_once()
            self.connection.close.assert_called_once()

    def test_changed_key_missing_subscription_and_reflected_secrets(self):
        for changes, code in (({"api_key": "FAKE-NEW-KEY"}, "key_changed"),
                              ({"is_subs_active": False}, "no_subscription"),
                              ({"subs_tier_name": TOKEN}, "invalid_data"),
                              ({"subs_usage": {"weekly": {"used_percent": KEY}}}, "invalid_data")):
            raw = payload()
            raw.update(changes)
            self.response.read.return_value = json.dumps(raw).replace("FAKE", "\\u0046AKE").encode()
            with self.assertRaises(live.LiveError) as caught:
                self.request()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_account_identity_is_projected_but_credentials_and_billing_are_not(self):
        raw = payload()
        raw.update(user_email="alice@example.test", user_full_name="Alice", payment_method="PRIVATE-BILLING", action_url="https://example.test/private")
        self.response.read.return_value = json.dumps(raw).encode()
        result = self.request()
        self.assertEqual(result["identity"], {"email": "alice@example.test", "name": "Alice"})
        for secret in (KEY, TOKEN, "PRIVATE-BILLING", "https://example.test/private"):
            self.assertNotIn(secret, json.dumps(result))
        raw.update(user_email=TOKEN, user_full_name=KEY)
        self.response.read.return_value = json.dumps(raw).encode()
        self.assertNotIn("identity", self.request())


class CommandTests(unittest.TestCase):
    def test_current_source_is_explicit_muse_only_and_unique(self):
        account = {"id": "muse-1", "name": "My Muse", "plan": "muse_code", "source": "cli_current"}
        self.assertEqual(app.validate_accounts({"accounts": [account]}), [account])
        for rows in ([{**account, "plan": "codex"}], [{**account, "source": "arbitrary"}],
                     [{**account, "source": None}], [account, {**account, "id": "muse-2"}]):
            with self.assertRaises(app.UsageError):
                app.validate_accounts({"accounts": rows})

    def test_combined_command_reports_muse_once_with_configured_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "data"
            accounts = app.default_accounts() + [
                {"id": "codex-1", "name": "My Codex", "plan": "codex"},
                {"id": "muse-1", "name": "My Muse", "plan": "muse_code", "source": "cli_current"}]
            with app.Store(directory, create=True) as store:
                store.write("accounts.json", {"accounts": accounts})
            entries = {a["id"]: {"fake": True} for a in accounts if a["plan"] != "muse_code"}
            def fetch(entry, seen, provider):
                return {"rate_limit": {}} if provider == "codex" else {"five_hour": {"utilization": 42}}
            muse_response = {k: v for k, v in payload().items() if k.startswith("subs_")}
            muse_response["subs_usage"]["window"]["used_percent"] = 101.25
            muse_response["subs_usage"]["weekly"]["used_percent"] = 137
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(live, "load_vault", return_value=entries))
                stack.enter_context(patch("credential_state.current", return_value={}))
                other = stack.enter_context(patch.object(live, "fetch", side_effect=fetch))
                stack.enter_context(patch.object(muse, "read_credentials", return_value=(TOKEN, KEY)))
                network = stack.enter_context(patch.object(muse.MuseCliClient, "request", return_value=muse_response))
                for target in ("subprocess.Popen", "os.fork", "os.system", "threading.Thread.start", "time.sleep", "claude_usage.Store.write"):
                    stack.enter_context(patch(target, side_effect=AssertionError(target)))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = app.main(["--jobs", "1", "--data-dir", str(directory), "--json"])
            self.assertEqual(code, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["live_accounts"], 9)
            self.assertTrue(data["live"])
            self.assertEqual(other.call_count, 8)
            network.assert_called_once_with("POST", muse.URL, TOKEN, KEY)
            row = data["accounts"][-1]
            self.assertEqual((row["id"], row["name"]), ("muse-1", "My Muse"))
            self.assertEqual(row["quota_scope"], "current_muse_cli_login")
            self.assertEqual(row["usage"]["five_hour"]["utilization"], 101.25)
            self.assertEqual(row["usage"]["seven_day"]["utilization"], 137)
            rendered = app.render(data, NOW)
            self.assertIn("101.25%", rendered)
            self.assertIn("137%", rendered)
            self.assertNotIn("switching that login changes the account", rendered)
            self.assertNotIn("POST /muse-code/key", rendered)
            detailed = app.render(data, NOW, verbose=True)
            self.assertIn("switching that login changes the account", detailed)
            self.assertIn("POST /muse-code/key", detailed)

    def test_combined_muse_failure_does_not_use_portal_or_snapshot(self):
        accounts = [{"id": "muse-1", "name": "My Muse", "plan": "muse_code", "source": "cli_current"},
                    {"id": "max-1", "name": "Max", "plan": "max_20x"}]
        store = MagicMock()
        store.read.side_effect = FileNotFoundError()
        with patch.object(muse, "read_credentials", side_effect=live.LiveError("authentication_error", "Expired.")), patch.object(live, "fetch") as other:
            data = app.report(accounts, store, NOW, {"muse-1": {"provider": "muse_code"}})
        self.assertFalse(data["live"])
        self.assertEqual(data["accounts"][0]["error_code"], "authentication_error")
        self.assertIsNone(data["accounts"][0]["usage"])
        self.assertEqual(data["accounts"][1]["status"], "not_configured")
        other.assert_not_called()
        store.read.assert_called_once_with("max-1.json")

    def test_unconfigured_muse_never_implicitly_reads_cli(self):
        store = MagicMock()
        store.read.side_effect = FileNotFoundError()
        with patch.object(muse, "read_credentials") as read:
            data = app.report([{"id": "muse-1", "name": "Muse", "plan": "muse_code"}], store, NOW)
        self.assertEqual(data["accounts"][0]["status"], "not_configured")
        read.assert_not_called()

    def test_source_unchanged_no_refresh_fields_or_network_when_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "muse/auth.json"
            path.parent.mkdir()
            raw = json.dumps({"providers": {"meta": {"access_token": TOKEN, "api_key": KEY,
                              "refresh_token": "FAKE-REFRESH-MUST-NOT-BE-USED"}}}).encode()
            path.write_bytes(raw)
            path.chmod(0o600)
            before = path.stat()
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), patch.object(muse.MuseCliClient, "request") as request:
                self.assertEqual(muse.read_credentials(), (TOKEN, KEY))
                request.assert_not_called()
            after = path.stat()
            self.assertEqual((before.st_atime_ns, before.st_mtime_ns), (after.st_atime_ns, after.st_mtime_ns))
            self.assertEqual(path.read_bytes(), raw)
            path.chmod(0o644)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), self.assertRaises(live.LiveError):
                muse.read_credentials()

    def test_explicit_option_no_vault_no_child_no_polling_secrets_or_writes(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(muse, "read_credentials", return_value=(TOKEN, KEY)))
            network = stack.enter_context(patch.object(muse.MuseCliClient, "request", return_value={k: v for k, v in payload().items() if k.startswith("subs_")}))
            for target in ("subprocess.Popen", "os.fork", "os.system", "threading.Thread.start", "time.sleep", "live_usage.load_vault", "claude_usage.Store"):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(app.main(["--muse-cli-current", "--json"]), 0)
            data = json.loads(stdout.getvalue())
            self.assertTrue(data["live"])
            self.assertEqual(data["accounts"][0]["quota_scope"], "current_muse_cli_login")
            rendered = app.render(data, NOW)
            self.assertNotIn("portal team", rendered)
            self.assertNotIn("Muse Muse Code", rendered)
            self.assertNotIn(KEY, stdout.getvalue() + stderr.getvalue())
            self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
            network.assert_called_once_with("POST", muse.URL, TOKEN, KEY)

    def test_standalone_table_and_verbose_notice_once(self):
        with patch.object(muse, "read_credentials", return_value=(TOKEN, KEY)), patch.object(
                muse.MuseCliClient, "request", return_value={k: v for k, v in payload().items() if k.startswith("subs_")}) as request:
            for flags in ([], ["--verbose"], ["--json", "--verbose"]):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(app.main(["--muse-cli-current", *flags]), 0)
                self.assertEqual(stderr.getvalue(), "")
                output = stdout.getvalue()
                if "--json" in flags:
                    self.assertTrue(json.loads(output)["live"])
                elif flags:
                    self.assertEqual(output.count(muse.NOTICE), 1)
                else:
                    self.assertTrue(output.startswith("╭─ Meta "))
                    self.assertEqual(len(output.splitlines()), 5)
                    self.assertNotIn(muse.NOTICE, output)
            self.assertEqual(request.call_count, 3)

    def test_failure_is_json_error_row_not_zero(self):
        with patch.object(muse, "read_credentials", side_effect=live.LiveError("authentication_error", "No login.")):
            data = muse.report(NOW)
        self.assertFalse(data["live"])
        self.assertIsNone(data["accounts"][0]["usage"])
        self.assertEqual(data["accounts"][0]["error_code"], "authentication_error")
