import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import signal
import ssl
import tempfile
import time
import unittest
import warnings
from unittest.mock import MagicMock, patch

import claude_usage as app
import live_usage as live

FAKE_TOKEN = "sk-ant-oat01-TEST-ONLY-NOT-A-REAL-TOKEN"
FAKE_REFRESH = "FAKE-REFRESH-MUST-NEVER-BE-USED"
PASSWORD = b"fake-test-passphrase-only"


class LiveNetworkTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.read.return_value = b'{"five_hour":{"utilization":42,"resets_at":null}}'
        self.factory = patch("live_usage.http.client.HTTPSConnection", return_value=self.connection)
        self.factory_mock = self.factory.start()
        self.addCleanup(self.factory.stop)

    def request(self):
        return live.UsageClient().request("GET", live.URL, FAKE_TOKEN)

    def test_exact_get_allowlist_and_no_inference_or_refresh(self):
        value = self.request()
        self.assertEqual(value["five_hour"]["utilization"], 42)
        self.assertEqual(live.READ_ONLY_ENDPOINTS, {("GET", "https://api.anthropic.com/api/oauth/usage"), ("GET", "https://chatgpt.com/backend-api/wham/usage")})
        self.factory_mock.assert_called_once()
        self.assertEqual(self.factory_mock.call_args.args, ("api.anthropic.com",))
        context = self.factory_mock.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIsNone(context.keylog_filename)
        args, kwargs = self.connection.request.call_args
        self.assertEqual(args, ("GET", "/api/oauth/usage"))
        self.assertNotIn("body", kwargs)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + FAKE_TOKEN)
        self.assertEqual(kwargs["headers"]["User-Agent"], "quotahop/1.0")
        self.assertNotIn(FAKE_REFRESH, str(self.connection.mock_calls))
        self.connection.request.assert_called_once()
        self.connection.close.assert_called_once()
        self.response.close.assert_called_once()
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_auth_errors_rate_limits_redirects_and_http_errors_never_retry(self):
        for status, code in ((401, "authentication_error"), (403, "authentication_error"),
                             (429, "rate_limited"), (301, "redirect_blocked"),
                             (302, "redirect_blocked"), (307, "redirect_blocked"),
                             (308, "redirect_blocked"), (500, "http_error")):
            self.connection.reset_mock()
            self.response.status = status
            self.response.read.return_value = FAKE_TOKEN.encode()
            with self.assertRaises(live.LiveError) as caught:
                self.request()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(FAKE_TOKEN, str(caught.exception))
            self.connection.request.assert_called_once()
            self.response.read.assert_not_called()
            self.connection.close.assert_called_once()

    def test_network_failures_are_redacted_and_closed(self):
        for exception, code in ((TimeoutError(FAKE_TOKEN), "timeout"),
                                (ConnectionError(FAKE_TOKEN), "network_error"),
                                (ssl.SSLError(FAKE_TOKEN), "tls_error")):
            self.connection.reset_mock()
            self.connection.getresponse.side_effect = exception
            with self.assertRaises(live.LiveError) as caught:
                self.request()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(FAKE_TOKEN, str(caught.exception))
            self.connection.request.assert_called_once()
            self.connection.close.assert_called_once()
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_hard_deadline_covers_blocking_request_and_cleans_timer(self):
        self.connection.getresponse.side_effect = lambda: time.sleep(1)
        handler = signal.getsignal(signal.SIGALRM)
        with patch("live_usage.ssl.SSLContext"), patch("live_usage.TIMEOUT", 0.02), self.assertRaises(live.LiveError) as caught:
            self.request()
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))
        self.connection.close.assert_called_once()

    def test_malformed_and_oversized_responses(self):
        for raw in (FAKE_TOKEN.encode(), b"{}" * live.MAX_BYTES, b'{"five_hour":NaN}'):
            self.response.read.return_value = raw
            with self.assertRaises(live.LiveError) as caught:
                self.request()
            self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_expired_tokens_never_sent_and_duplicates_blocked(self):
        with self.assertRaises(live.LiveError) as caught:
            live.fetch({"access_token": FAKE_TOKEN, "expires_at": 1}, set())
        self.assertEqual(caught.exception.code, "token_expired")
        self.factory_mock.assert_not_called()
        seen = set()
        entry = {"access_token": FAKE_TOKEN, "expires_at": None}
        live.fetch(entry, seen)
        with self.assertRaises(live.LiveError) as caught:
            live.fetch(entry, seen)
        self.assertEqual(caught.exception.code, "duplicate_account")
        self.connection.request.assert_called_once()

    def test_no_proxy_or_tls_secret_log_environment_support(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keys.log"
            with patch.dict(os.environ, {"HTTPS_PROXY": "http://untrusted.invalid", "SSLKEYLOGFILE": str(path)}):
                self.request()
            self.assertFalse(path.exists())
            self.connection.set_tunnel.assert_not_called()
            self.assertEqual(self.factory_mock.call_args.args, (live.HOST,))


class VaultTests(unittest.TestCase):
    def setUp(self):
        for method, result in (("get", None), ("put", True), ("clear", True)):
            cache = patch("vault_cache." + method, return_value=result)
            cache.start()
            self.addCleanup(cache.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / ".credentials.json"
        self.write_source()
        self.store = app.Store(self.root / "data", create=True)
        self.addCleanup(self.store.__exit__, None, None, None)

    def write_source(self, token=FAKE_TOKEN):
        self.source.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token, "refreshToken": FAKE_REFRESH,
            "expiresAt": (time.time() + 3600) * 1000, "subscriptionType": "max",
        }}))
        self.source.chmod(0o600)

    def capture(self, ident):
        with patch("live_usage.passphrase", return_value=PASSWORD):
            return live.capture(self.store, ident, self.source)

    def test_capture_is_read_only_including_atime_no_network(self):
        original_open = os.open
        flags_used = []

        def observed(path, flags, *args, **kwargs):
            if Path(path) == self.source:
                flags_used.append(flags)
            return original_open(path, flags, *args, **kwargs)

        os.utime(self.source, ns=(1_000_000_000, 2_000_000_000))
        before = self.source.stat()
        with patch("live_usage.os.open", side_effect=observed), patch("socket.socket") as network:
            self.capture("max-1")
            network.assert_not_called()
        after = self.source.stat()
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(before.st_atime_ns, after.st_atime_ns)
        self.assertEqual(before.st_ino, after.st_ino)
        self.assertEqual(before.st_mode, after.st_mode)
        self.assertEqual(len(flags_used), 1)
        self.assertEqual(flags_used[0] & os.O_ACCMODE, os.O_RDONLY)
        self.assertFalse(flags_used[0] & (os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        self.assertIn(FAKE_REFRESH, self.source.read_text())

    def test_vault_encrypted_and_contains_only_access_token_and_expiry(self):
        self.capture("max-1")
        data = (self.root / "data/vault.json").read_bytes()
        self.assertNotIn(FAKE_TOKEN.encode(), data)
        self.assertNotIn(FAKE_REFRESH.encode(), data)
        entries, _, _ = live.unlock_vault(self.store.read("vault.json"), PASSWORD)
        self.assertEqual(set(entries["max-1"]), {"access_token", "expires_at"})
        self.assertEqual(entries["max-1"]["access_token"], FAKE_TOKEN)
        self.assertNotIn(FAKE_REFRESH, json.dumps(entries))
        self.assertEqual((self.root / "data/vault.json").stat().st_mode & 0o777, 0o600)

    def test_wrong_passphrase_tampering_and_duplicate_capture(self):
        self.capture("max-1")
        with self.assertRaises(live.LiveError):
            live.unlock_vault(self.store.read("vault.json"), b"wrong-password")
        envelope = self.store.read("vault.json")
        envelope["ciphertext"] = "AAAA"
        with self.assertRaises(live.LiveError):
            live.unlock_vault(envelope, PASSWORD)
        with self.assertRaises(live.LiveError) as caught:
            self.capture("max-2")
        self.assertEqual(caught.exception.code, "duplicate_account")

    def test_seven_captures_survive_shared_source_switches(self):
        for index, account in enumerate(app.default_accounts()):
            self.write_source(FAKE_TOKEN + str(index))
            self.capture(account["id"])
        with patch("live_usage.passphrase", return_value=PASSWORD):
            entries = live.load_vault(self.store)
        self.assertEqual(len(entries), 7)
        tokens_seen = []

        def fake_request(client, method, url, access_token):
            tokens_seen.append(access_token)
            return {"five_hour": {"utilization": len(tokens_seen)}}

        with patch.object(live.UsageClient, "request", fake_request):
            report = app.report(app.default_accounts(), self.store, dt.datetime.now(app.UTC), entries)
        self.assertEqual(len(set(tokens_seen)), 7)
        self.assertTrue(report["live"])
        self.assertEqual(report["live_accounts"], 7)
        self.assertEqual([a["usage"]["five_hour"]["utilization"] for a in report["accounts"]], list(range(1, 8)))
        self.assertNotIn(FAKE_TOKEN, json.dumps(report))
        self.assertEqual([a["plan"] for a in report["accounts"]], ["max_20x"] * 2 + ["team_premium"] * 5)

    def test_live_failure_has_no_snapshot_fallback(self):
        self.store.write("max-1.json", {"captured_at": "2026-01-01T00:00:00Z",
                                        "usage": {"five_hour": {"utilization": 42}}})
        entries = {"max-1": {"access_token": FAKE_TOKEN, "expires_at": None}}
        with patch.object(live.UsageClient, "request", side_effect=live.LiveError("authentication_error", "Expired; not refreshed.")):
            result = app.report(app.default_accounts(), self.store, dt.datetime.now(app.UTC), entries)
        first = result["accounts"][0]
        self.assertEqual(first["status"], "error")
        self.assertIsNone(first["usage"])
        self.assertFalse(first["live"])

    def test_unsafe_credential_sources_refused(self):
        self.source.chmod(0o644)
        with self.assertRaises(live.LiveError):
            live.read_access_token(self.source)
        self.source.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.source)
        with self.assertRaises(live.LiveError):
            live.read_access_token(link)
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(live.LiveError):
            live.read_access_token(fifo)

    def test_passphrase_never_falls_back_to_echoed_input(self):
        def unsafe_prompt(*_):
            warnings.warn("Cannot control terminal echo", live.getpass.GetPassWarning)
            self.fail("An echoing fallback must not run")

        with patch("live_usage.os.open", return_value=123), patch("live_usage.os.close"), patch("live_usage.getpass.getpass", side_effect=unsafe_prompt):
            with self.assertRaises(live.LiveError) as caught:
                live.passphrase()
        self.assertEqual(caught.exception.code, "tty_required")

    def test_live_run_no_children_workers_or_polling(self):
        entries = {a["id"]: {"access_token": FAKE_TOKEN + str(i), "expires_at": None}
                   for i, a in enumerate(app.default_accounts())}
        forbidden = AssertionError("Unexpected background activity")
        with contextlib.ExitStack() as stack:
            for target in ("subprocess.Popen", "os.fork", "os.system", "threading.Thread.start", "time.sleep"):
                stack.enter_context(patch(target, side_effect=forbidden))
            network = stack.enter_context(patch.object(live.UsageClient, "request", return_value={"five_hour": {"utilization": 1}}))
            result = app.report(app.default_accounts(), self.store, dt.datetime.now(app.UTC), entries)
        self.assertTrue(result["live"])
        self.assertEqual(network.call_count, 7)


if __name__ == "__main__":
    unittest.main()
