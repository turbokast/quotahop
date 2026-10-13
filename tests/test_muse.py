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
import muse_usage as muse

COOKIE = "FAKE-PORTAL-COOKIE-NOT-A-REAL-SESSION"
PASSWORD = b"fake-private-vault-passphrase"
NOW = dt.datetime(2026, 10, 12, tzinfo=app.UTC)
RESET = int(NOW.timestamp()) + 18000


def entry(team="123"):
    return {"provider": "muse_code", "session_cookie": COOKIE,
            "subject_id": "456", "team_id": team, "team_name": "My Muse subscription"}


def payload():
    return {"subscription_quota": {"tier": "Muse Code Power Usage", "window_duration_secs": 18000,
        "window_weighted_used": "420", "window_weighted_limit": "1000", "window_resets_at": RESET,
        "weekly_weighted_used": "6500", "weekly_weighted_limit": "10000", "weekly_resets_at": RESET + 604800}}


class MuseParsingTests(unittest.TestCase):
    def test_exact_weighted_subscription_ratios(self):
        parsed = app.parse_muse_portal(payload(), NOW)
        self.assertEqual(parsed["five_hour"]["utilization"], 42)
        self.assertEqual(parsed["seven_day"]["utilization"], 65)
        self.assertEqual(app.timestamp(parsed["five_hour"]["resets_at"]).timestamp(), RESET)
        self.assertEqual(parsed["reported_plan"], "Muse Code Power Usage")

    def test_idle_is_reported_zero_only_with_zero_counter(self):
        raw = payload()
        raw["subscription_quota"].update(window_weighted_used="0", window_resets_at=None)
        parsed = app.parse_muse_portal(raw, NOW)
        self.assertEqual(parsed["five_hour"], {"utilization": 0, "resets_at": None})
        self.assertEqual(parsed["seven_day"]["utilization"], 65)

    def test_missing_zero_limits_and_stale_data_are_unknown(self):
        for changes in ({"window_weighted_used": None}, {"window_weighted_limit": None},
                        {"window_weighted_limit": "0"}, {"window_resets_at": 1}, {"window_resets_at": None}):
            raw = payload()
            raw["subscription_quota"].update(changes)
            self.assertIsNone(app.parse_muse_portal(raw, NOW)["five_hour"]["utilization"])
        raw = payload()
        raw["subscription_quota"]["weekly_resets_at"] = 1
        self.assertIsNone(app.parse_muse_portal(raw, NOW)["seven_day"]["utilization"])

    def test_large_precise_counters_overage_and_alternate_durations(self):
        raw = payload()
        raw["subscription_quota"].update(window_weighted_used=str(42*10**40), window_weighted_limit=str(100*10**40))
        self.assertEqual(app.parse_muse_portal(raw, NOW)["five_hour"]["utilization"], 42)
        raw["subscription_quota"].update(window_weighted_used="1200", window_weighted_limit="1000", window_duration_secs=900)
        parsed = app.parse_muse_portal(raw, NOW)
        self.assertIsNone(parsed["five_hour"]["utilization"])
        self.assertEqual(parsed["model_windows"]["Muse subscription (15m)"]["utilization"], 120)

    def test_payg_or_malformed_data_is_never_subscription_usage(self):
        for raw in ({"spend": 42}, {"subscription_quota": None}):
            with self.assertRaises(live.LiveError) as caught:
                app.parse_muse_portal(raw, NOW)
            self.assertEqual(caught.exception.code, "no_subscription")
        for bad in (True, -1, "NaN", "1e99", "1.5", [], "9"*81):
            raw = payload()
            raw["subscription_quota"]["window_weighted_used"] = bad
            with self.assertRaises(app.UsageError):
                app.parse_muse_portal(raw, NOW)
        for raw in ({"subscription_quota": []}, {"subscription_quota": {"cookie": COOKIE}}):
            with self.assertRaises(app.UsageError):
                app.parse_muse_portal(raw, NOW)


class MuseNetworkTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = json.dumps(payload()).encode()
        self.patcher = patch("muse_usage.http.client.HTTPSConnection", return_value=self.connection)
        self.factory = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def request(self, path="/api/portal/teams/123/subscription-quota"):
        return muse.MuseClient().request("GET", muse.BASE + path, COOKIE)

    def test_only_expected_get_and_cookie_scope_no_auth_or_rotation(self):
        self.request()
        self.assertEqual(self.factory.call_args.args, ("dev.meta.ai",))
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("GET", "/api/portal/teams/123/subscription-quota"))
        self.assertEqual(kwargs["headers"]["Cookie"], "llama_dev_sess=" + COOKIE)
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertNotIn("body", kwargs)
        self.response.getheader.assert_not_called()  # no Set-Cookie adoption
        self.connection.request.assert_called_once()
        self.connection.close.assert_called_once()
        self.response.close.assert_called_once()
        context = self.factory.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIsNone(context.keylog_filename)

    def test_exact_allowlist_blocks_mint_login_inference_and_url_tricks(self):
        for method, url in (("POST", muse.BASE+muse.ME), ("POST", "https://api.meta.ai/muse-code/key"),
                            ("GET", muse.BASE+"/api/auth/refresh"), ("POST", "https://api.meta.ai/v1/responses"),
                            ("GET", muse.BASE+"/api/portal/teams/123/members"),
                            ("GET", muse.BASE+"/api/portal/teams/123/subscription-quota?secret=x"),
                            ("GET", muse.BASE+"/api/portal/teams/../subscription-quota"),
                            ("GET", muse.BASE+"/api/portal/teams/%31/subscription-quota"),
                            ("GET", "https://dev.meta.ai.evil.invalid"+muse.ME),
                            ("GET", "https://dev.meta.ai@evil.invalid"+muse.ME),
                            ("GET", muse.BASE+":443"+muse.ME),
                            ("GET", muse.BASE+muse.ME+"#fragment"), ("HEAD", muse.BASE+muse.ME)):
            with self.assertRaises(live.LiveError):
                muse.MuseClient().request(method, url, COOKIE)
        self.factory.assert_not_called()

    def test_cookie_injection_and_header_pastes_refused(self):
        for bad in ("llama_dev_sess="+COOKIE, COOKIE+"; other=value", COOKIE+"\r\nx:y", '"'+COOKIE+'"', COOKIE+",bad", COOKIE+"\\x", ""):
            with self.assertRaises(live.LiveError):
                muse.MuseClient().request("GET", muse.BASE+muse.ME, bad)
        self.factory.assert_not_called()

    def test_failures_closed_redacted_never_retried(self):
        for status in (401, 403, 429, 301, 302, 307, 500):
            self.connection.reset_mock()
            self.response.status = status
            self.response.read.return_value = COOKIE.encode()
            with self.assertRaises(live.LiveError) as caught:
                self.request()
            self.assertNotIn(COOKIE, str(caught.exception))
            self.connection.request.assert_called_once()
            self.response.read.assert_not_called()
            self.connection.close.assert_called_once()

    def test_network_timeout_and_tls_errors_redacted(self):
        for error in (TimeoutError(COOKIE), ConnectionError(COOKIE), ssl.SSLError(COOKIE)):
            self.connection.reset_mock()
            self.connection.getresponse.side_effect = error
            with self.assertRaises(live.LiveError) as caught:
                self.request()
            self.assertNotIn(COOKIE, str(caught.exception))
            self.connection.close.assert_called_once()
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_reflected_or_escaped_cookie_never_leaks(self):
        for raw in (json.dumps({"tier": COOKIE}).encode(), json.dumps({"tier": COOKIE}).replace("FAKE", "\\u0046AKE").encode()):
            self.response.read.return_value = raw
            with self.assertRaises(live.LiveError) as caught:
                self.request()
            self.assertNotIn(COOKIE, str(caught.exception))

    def test_two_reads_per_report_only_one_quota_and_identity_must_match(self):
        with patch.object(muse.MuseClient, "request", side_effect=[{"userId": "456"}, payload()]) as network:
            muse.fetch(entry(), set())
            self.assertEqual([c.args[1] for c in network.call_args_list], [muse.BASE+muse.ME, muse.BASE+"/api/portal/teams/123/subscription-quota"])
        with patch.object(muse.MuseClient, "request", return_value={"userId": "999"}) as network:
            with self.assertRaises(live.LiveError) as caught:
                muse.fetch(entry(), set())
            self.assertEqual(caught.exception.code, "account_mismatch")
            network.assert_called_once()


class MuseCaptureTests(unittest.TestCase):
    def setUp(self):
        for method, result in (("get", None), ("put", True), ("clear", True)):
            cache = patch("vault_cache." + method, return_value=result)
            cache.start()
            self.addCleanup(cache.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)/"data"
        self.store = app.Store(self.directory, create=True)
        self.addCleanup(self.store.__exit__, None, None, None)

    def test_capture_selects_explicit_team_encrypts_session_and_touches_no_cli_files(self):
        responses = [{"userId": "456"}, {"teams": [{"team_id": "123", "team_name": "My Muse subscription"},
                     {"team_id": "789", "team_name": "Another team"}]}, payload()]
        with patch.object(muse, "hidden_cookie", return_value=COOKIE), patch.object(muse.MuseClient, "request", side_effect=responses) as network, patch.object(live, "passphrase", return_value=PASSWORD), patch.object(live, "read_access_token") as source, contextlib.redirect_stderr(io.StringIO()):
            live.capture(self.store, "muse-1", None, "muse_code", muse_team="123")
            source.assert_not_called()
            self.assertEqual(network.call_count, 3)
        encrypted = self.store.read("vault.json")
        self.assertNotIn(COOKIE, json.dumps(encrypted))
        saved, _, _ = live.unlock_vault(encrypted, PASSWORD)
        self.assertEqual(saved["muse-1"], entry())
        self.assertEqual((self.directory/"vault.json").stat().st_mode & 0o777, 0o600)

    def test_unlisted_team_rejected_before_quota_and_before_save(self):
        with patch.object(muse, "hidden_cookie", return_value=COOKIE), patch.object(muse.MuseClient, "request", side_effect=[{"userId": "456"}, {"teams": [{"team_id": "123", "team_name": "mine"}]}]) as network, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(live.LiveError):
            live.capture(self.store, "muse-1", None, "muse_code", muse_team="999")
        self.assertEqual(network.call_count, 2)
        self.assertFalse((self.directory/"vault.json").exists())

    def test_no_hidden_input_fallback(self):
        import warnings
        def unsafe(*_):
            warnings.warn("unsafe", muse.getpass.GetPassWarning)
            self.fail("Echoed fallback used")
        with patch("muse_usage.os.open", return_value=123), patch("muse_usage.os.close"), patch("muse_usage.getpass.getpass", side_effect=unsafe), self.assertRaises(live.LiveError):
            muse.hidden_cookie()

    def test_normal_run_multi_account_reports_scoped_data_no_writes_children_or_polling(self):
        accounts = [{"id": f"muse-{i}", "name": f"Muse {i}", "plan": "muse_code"} for i in range(1, 3)]
        entries = {a["id"]: entry(str(123+i)) for i, a in enumerate(accounts)}
        with contextlib.ExitStack() as stack:
            for target in ("subprocess.Popen", "os.fork", "os.system", "threading.Thread.start", "time.sleep"):
                stack.enter_context(patch(target, side_effect=AssertionError("Background work forbidden")))
            writes = stack.enter_context(patch.object(self.store, "write", side_effect=AssertionError("Report write forbidden")))
            requests = stack.enter_context(patch.object(muse.MuseClient, "request", side_effect=[{"userId": "456"}, payload(), {"userId": "456"}, payload()]))
            report = app.report(accounts, self.store, NOW, entries)
        self.assertEqual(report["live_accounts"], 2)
        self.assertTrue(report["live"])
        self.assertEqual(requests.call_count, 4)
        self.assertEqual(report["accounts"][0]["quota_scope"], "selected_meta_portal_team")
        self.assertFalse(report["accounts"][0]["cli_account_verified"])
        self.assertNotIn(COOKIE, json.dumps(report))
        self.assertIn("Meta portal team:", app.render(report, NOW, verbose=True))
        writes.assert_not_called()

    def test_cli_capture_and_json_report_end_to_end_with_fake_session(self):
        self.store.write("accounts.json", {"accounts": [{"id": "muse-1", "name": "Personal Muse", "plan": "muse_code"}]})
        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), patch("credential_state.current", return_value={}):
                code = app.main(["--jobs", "1", "--data-dir", str(self.directory), *args])
            return code, out.getvalue()
        with patch.object(muse, "hidden_cookie", return_value=COOKIE), patch.object(muse.MuseClient, "request", side_effect=[
                {"userId": "456"}, {"teams": [{"team_id": "123", "team_name": "Personal"}]}, payload()]), patch.object(live, "passphrase", return_value=PASSWORD):
            code, out = run("--capture", "muse-1", "--muse-team", "123", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn(COOKIE, out)
        with patch.object(live, "passphrase", return_value=PASSWORD), patch.object(muse.MuseClient, "request", side_effect=[{"userId": "456"}, payload()]):
            code, out = run("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["live"])
        self.assertEqual(data["accounts"][0]["portal_team"], "Personal")
        self.assertNotIn(COOKIE, out)

    def test_auth_error_does_not_fall_back_to_historical_export(self):
        self.store.write("muse-1.json", {"captured_at": NOW.isoformat(), "usage": {"five_hour": {"utilization": 10}}})
        accounts = [{"id": "muse-1", "name": "Muse", "plan": "muse_code"}]
        with patch.object(muse.MuseClient, "request", side_effect=live.LiveError("authentication_error", "Expired portal session.")) as requests:
            data = app.report(accounts, self.store, NOW, {"muse-1": entry()})
        self.assertFalse(data["live"])
        self.assertEqual(data["accounts"][0]["status"], "error")
        self.assertIsNone(data["accounts"][0]["usage"])
        requests.assert_called_once()


if __name__ == "__main__":
    unittest.main()
