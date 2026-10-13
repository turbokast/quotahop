import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import claude_usage as app

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 1, 15, 12, tzinfo=app.UTC)


def snapshot(value=42):
    return {"captured_at": "2026-01-15T11:00:00Z", "usage": {
        "five_hour": {"utilization": value, "resets_at": "2026-01-15T13:23:00Z"},
        "seven_day": {"utilization": 64, "resets_at": "2026-01-18T14:00:00Z"},
        "seven_day_sonnet": {"utilization": 12, "resets_at": "2026-01-19T14:00:00Z"},
        "extra_usage": {"is_enabled": True, "utilization": 10,
                        "used_credits": 100, "monthly_limit": 1000},
    }}


class ParserTests(unittest.TestCase):
    def test_parse_windows_and_extra(self):
        usage = app.parse_usage(snapshot()["usage"])
        self.assertEqual(usage["five_hour"]["utilization"], 42)
        self.assertEqual(usage["model_windows"]["seven_day_sonnet"]["utilization"], 12)
        self.assertEqual(usage["extra_usage"]["used_credits"], 100)

    def test_missing_null_and_zero_are_distinct(self):
        for data in ({}, {"five_hour": None}, {"five_hour": {}}):
            self.assertIsNone(app.parse_usage(data)["five_hour"]["utilization"])
        self.assertEqual(app.parse_usage({"five_hour": {"utilization": 0}})
                         ["five_hour"]["utilization"], 0)

    def test_unrelated_api_tokens_are_not_subscription_usage(self):
        value = app.parse_usage({"input_count": 1234, "total_cost": 999})
        self.assertIsNone(value["five_hour"]["utilization"])
        self.assertIsNone(value["seven_day"]["utilization"])

    def test_invalid_percentages(self):
        for value in (-1, 101, "42", True, [], float("nan"), float("inf"), 10**400):
            with self.subTest(value=value), self.assertRaises(app.UsageError):
                app.parse_usage({"five_hour": {"utilization": value}})

    def test_invalid_schema(self):
        for value in ([], "bad", {"five_hour": 2}, {"extra_usage": []},
                      {"extra_usage": {"is_enabled": "yes"}}):
            with self.subTest(value=value), self.assertRaises(app.UsageError):
                app.parse_usage(value)

    def test_invalid_resets(self):
        for value in (True, 5, "bad", "2026-01-01T12:00:00", "2026-02-30T00:00:00Z"):
            with self.subTest(value=value), self.assertRaises(app.UsageError):
                app.timestamp(value)

    def test_countdowns(self):
        for seconds, expected in ((-1, "elapsed"), (0, "elapsed"), (30, "<1m"),
                                  (60, "1m"), (4980, "1h 23m"), (266400, "3d 2h")):
            self.assertEqual(app.duration(seconds), expected)

    def test_local_timezone_and_dst(self):
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Europe/London"
            time.tzset()
            summer = app.reset_text("2026-07-01T12:00:00Z", NOW)
            winter = app.reset_text("2026-01-15T13:23:00Z", NOW)
            self.assertIn("13:00 BST", summer)
            self.assertIn("1h 23m", winter)
            self.assertIn("13:23 GMT", winter)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_json_rejects_duplicate_keys_nonfinite_large_and_malformed(self):
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                     b"\xff", b"{}" * app.LIMIT, b"["):
            with self.assertRaises(app.UsageError):
                app.decode(data)

    def test_secrets_are_rejected_without_echo(self):
        for key in ("accessToken", "refresh_token", "sessionKey", "Cookie", "api_key"):
            with self.assertRaises(app.UsageError) as caught:
                app.decode(json.dumps({key: "FAKE-SECRET-DO-NOT-PRINT"}))
            self.assertNotIn("FAKE-SECRET", str(caught.exception))
        for value in ("sk-ant-oat01-FAKE", "Bearer FAKE", "eyJfake.fake.fake"):
            with self.assertRaises(app.UsageError):
                app.decode(json.dumps({"name": value}))

    def test_account_validation(self):
        for change in ({"id": "../../.claude"}, {"id": "accounts"}, {"plan": "api"},
                       {"name": "x\x1b[31m"}, {"name": "x\u202ey"}, {"name": ""}):
            accounts = app.default_accounts()
            accounts[0].update(change)
            with self.assertRaises(app.UsageError):
                app.validate_accounts({"accounts": accounts})
        accounts = app.default_accounts()
        accounts[1]["id"] = accounts[0]["id"]
        with self.assertRaises(app.UsageError):
            app.validate_accounts({"accounts": accounts})

    def test_future_capture_rejected(self):
        value = snapshot()
        value["captured_at"] = "9999-01-01T00:00:00Z"
        with self.assertRaises(app.UsageError):
            app.validate_snapshot(value)


class StorageAndCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "usage"
        self.assertEqual(self.run_cli("--setup")[0], 0)

    def run_cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("credential_state.current", return_value={}):
            if stdin is None:
                code = app.main(["--jobs", "1", "--data-dir", str(self.directory), *args])
            else:
                with patch("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin))):
                    code = app.main(["--jobs", "1", "--data-dir", str(self.directory), *args])
        return code, out.getvalue(), err.getvalue()

    def test_seven_separate_accounts_and_correct_plans(self):
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 3)
        data = json.loads(out)
        self.assertEqual(len(data["accounts"]), 7)
        self.assertEqual([a["plan"] for a in data["accounts"]],
                         ["max_20x"] * 2 + ["team_premium"] * 5)
        self.assertTrue(all(a["status"] == "not_configured" for a in data["accounts"]))
        self.assertTrue(all(a["usage"] is None for a in data["accounts"]))
        self.assertFalse(data["live"])

    def test_independent_snapshots(self):
        for index, account in enumerate(app.default_accounts()):
            result = self.run_cli("--import", account["id"],
                                  stdin=json.dumps(snapshot(index)).encode())
            self.assertEqual(result[0], 0, result)
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([a["usage"]["five_hour"]["utilization"] for a in data["accounts"]], list(range(7)))
        self.assertTrue(all(a["status"] == "snapshot" and not a["live"] for a in data["accounts"]))

    def test_permissions_and_setup_is_idempotent(self):
        config = self.directory / "accounts.json"
        before = config.stat()
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(before.st_mode), 0o600)
        self.assertEqual(self.run_cli("--setup")[0], 0)
        self.assertEqual(config.stat().st_mtime_ns, before.st_mtime_ns)

    def test_custom_names(self):
        with app.Store(self.directory) as store:
            data = store.read("accounts.json")
            data["accounts"][0]["name"] = "Personal Max"
            store.write("accounts.json", data, replace=True)
        self.assertIn("Personal Max", self.run_cli()[1])

    def test_default_report_is_only_table_and_verbose_restores_details(self):
        self.run_cli("--import", "max-1", stdin=json.dumps(snapshot()).encode())
        code, out, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertEqual(err, "")
        self.assertEqual(len(out.splitlines()), 14)  # Table plus the requested selection key.
        self.assertTrue(out.startswith("╭─ Anthropic "))
        self.assertIn("42%", out)
        self.assertIn("Sign in", out)
        self.assertNotIn("*", out)
        detailed_code, detailed, detailed_err = self.run_cli("--verbose")
        self.assertEqual(detailed_code, code)
        self.assertEqual(detailed_err, "")
        for detail in ("Subscription usage", "seven_day_sonnet", "Extra usage:",
                       "Snapshots are historical", app.live_usage.NOTICE):
            self.assertNotIn(detail, out)
            self.assertIn(detail, detailed)

    def test_account_error_explanation_requires_verbose(self):
        with patch.object(app.live_usage, "load_vault", return_value={"max-1": {"provider": "claude"}}), patch.object(
                app.live_usage, "fetch", side_effect=app.live_usage.LiveError("authentication_error", "Saved token expired.")) as fetch:
            for flags in ((), ("--verbose",)):
                code, out, err = self.run_cli(*flags)
                self.assertEqual(code, 1)
                self.assertEqual(err, "")
                self.assertIn("ERROR", out)
                self.assertEqual("Saved token expired." in out, bool(flags))
            self.assertEqual(fetch.call_count, 2)  # One retrieval per execution.

    def test_verbose_json_retains_full_structured_details(self):
        self.run_cli("--import", "max-1", stdin=json.dumps(snapshot()).encode())
        results = []
        for flags in (("--json",), ("--json", "--verbose")):
            code, out, err = self.run_cli(*flags)
            self.assertEqual(code, 3)
            self.assertEqual(err, "")
            results.append(json.loads(out)["accounts"])
        self.assertEqual(results[0], results[1])
        self.assertIn("seven_day_sonnet", results[0][0]["usage"]["model_windows"])

    def test_one_read_per_account_no_writes_on_report(self):
        calls = []
        original = app.Store.read

        def observed(store, name):
            calls.append(name)
            return original(store, name)

        with patch.object(app.Store, "read", observed), patch.object(app.Store, "write") as write:
            self.run_cli()
            write.assert_not_called()
        self.assertEqual(calls, ["accounts.json", "vault.json"] + [a["id"] + ".json" for a in app.default_accounts()])

    def test_terminal_report_never_claims_zero_after_reset(self):
        self.run_cli("--import", "max-1", stdin=json.dumps(snapshot(42)).encode())
        out = self.run_cli("--verbose")[1]
        self.assertIn("42%", out)
        self.assertIn("elapsed", out)
        self.assertIn("SNAPSHOT", out)
        self.assertIn("seven_day_sonnet", out)
        self.assertIn("currency/unit scale unverified", out)

    def test_invalid_snapshot_is_per_account_error(self):
        with app.Store(self.directory) as store:
            store.write("max-1.json", {"bogus": "value"})
        code, out, _ = self.run_cli("--json")
        self.assertEqual(code, 1)
        self.assertEqual([a["status"] for a in json.loads(out)["accounts"]],
                         ["error"] + ["not_configured"] * 6)

    def test_bad_import_preserves_existing_snapshot(self):
        self.run_cli("--import", "max-1", stdin=json.dumps(snapshot()).encode())
        before = (self.directory / "max-1.json").read_bytes()
        result = self.run_cli("--import", "max-1", stdin=b'{"token":"FAKE-SECRET"}')
        self.assertEqual(result[0], 2)
        self.assertNotIn("FAKE-SECRET", str(result))
        self.assertEqual(before, (self.directory / "max-1.json").read_bytes())

    def test_unknown_fields_not_persisted_or_printed(self):
        data = snapshot()
        data["usage"]["internal_debug"] = "FAKE-UNRECOGNIZED-SECRET"
        self.run_cli("--import", "max-1", stdin=json.dumps(data).encode())
        self.assertNotIn("FAKE-UNRECOGNIZED-SECRET", (self.directory / "max-1.json").read_text())
        self.assertNotIn("FAKE-UNRECOGNIZED-SECRET", self.run_cli("--json")[1])

    def test_unknown_argument_never_echoes_secret(self):
        code, out, err = self.run_cli("--token", "FAKE-SECRET")
        self.assertEqual(code, 2)
        self.assertNotIn("FAKE-SECRET", out + err)

    def test_unsafe_permissions_refused_without_chmod(self):
        config = self.directory / "accounts.json"
        config.chmod(0o644)
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o644)

    def test_symlink_and_hardlink_reads_refused(self):
        target = self.root / "secret"
        target.write_text("FAKE-CREDENTIAL")
        target.chmod(0o600)
        path = self.directory / "max-1.json"
        path.symlink_to(target)
        self.assertEqual(self.run_cli()[0], 1)
        path.unlink()
        os.link(target, path)
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(target.read_text(), "FAKE-CREDENTIAL")

    def test_fifo_does_not_block(self):
        os.mkfifo(self.directory / "max-1.json", 0o600)
        self.assertEqual(self.run_cli()[0], 1)

    def test_symlink_directory_and_protected_paths_refused(self):
        alias = self.root / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(OSError):
            app.Store(alias)
        for suffix in (".claude/test", ".config/claude/test", ".git/test"):
            with self.assertRaises(app.UsageError):
                app.Store(self.root / suffix, create=True)
        self.assertFalse((self.root / ".claude").exists())

    def test_import_does_not_follow_write_symlink(self):
        target = self.root / "credential"
        target.write_text("FAKE-CREDENTIAL")
        (self.directory / "max-1.json").symlink_to(target)
        self.run_cli("--import", "max-1", stdin=json.dumps(snapshot()).encode())
        self.assertEqual(target.read_text(), "FAKE-CREDENTIAL")

    def test_no_network_processes_threads_or_polling(self):
        forbidden = AssertionError("Side effect attempted")
        with contextlib.ExitStack() as stack:
            for target in ("socket.socket", "socket.getaddrinfo", "subprocess.Popen", "os.system",
                           "os.fork", "threading.Thread.start", "time.sleep", "claude_usage.UsageClient.request"):
                stack.enter_context(patch(target, side_effect=forbidden))
            self.assertEqual(self.run_cli("--setup")[0], 0)
            self.assertEqual(self.run_cli("--import", "max-1", stdin=json.dumps(snapshot()).encode())[0], 0)
            self.assertEqual(self.run_cli()[0], 3)

    def test_auth_timeout_and_connection_failures_cannot_trigger_auth_machinery(self):
        # Without captured accounts there must be no network or auth fallback.
        for failure in (PermissionError("FAKE-SECRET"), TimeoutError("FAKE-SECRET"),
                        ConnectionError("FAKE-SECRET")):
            with patch("socket.create_connection", side_effect=failure) as connect:
                code, out, err = self.run_cli("--json")
                self.assertEqual(code, 3)
                connect.assert_not_called()
                self.assertNotIn("FAKE-SECRET", out + err)

    def test_real_entrypoint_no_credential_access_or_mutation(self):
        fake_home = self.root / "home"
        fake_home.mkdir()
        files = []
        for directory in (fake_home / ".claude", fake_home / ".config/claude",
                          fake_home / "alternate-claude"):
            directory.mkdir(parents=True)
            path = directory / ".credentials.json"
            path.write_text('{"accessToken":"FAKE-ACCESS","refreshToken":"FAKE-REFRESH"}')
            path.chmod(0o600)
            files.append(path)
        before = [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in files]
        env = {**os.environ, "HOME": str(fake_home), "CLAUDE_CONFIG_DIR": str(fake_home / "alternate-claude")}
        # Audit the actual executable in a child with a deadline. The application
        # itself must never create a child, network socket, or open our canaries.
        wrapper = '''
import sys, runpy
def audit(event, args):
    if event.startswith(("socket.", "subprocess.")) or event in ("os.fork", "os.system", "os.posix_spawn"):
        raise RuntimeError("Forbidden process/network activity")
    if event == "open" and isinstance(args[0], str) and ".credentials.json" in args[0]:
        raise RuntimeError("Forbidden credential access")
sys.addaudithook(audit)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        completed = subprocess.run([sys.executable, "-B", "-c", wrapper, str(ROOT / "claude-usage"),
                                    "--data-dir", str(self.directory), "--json"],
                                   env=env, capture_output=True, text=True, timeout=3)
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(len(json.loads(completed.stdout)["accounts"]), 7)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in files])


class NetworkBoundaryTests(unittest.TestCase):
    def test_all_paths_and_methods_denied_before_network(self):
        self.assertEqual(app.READ_ONLY_ENDPOINTS, {("GET", "https://api.anthropic.com/api/oauth/usage"), ("GET", "https://chatgpt.com/backend-api/wham/usage")})
        paths = ("/v1/messages", "/v1/complete", "/v1/messages/batches", "/v1/oauth/token",
                 "/api/oauth/usage", "/api/oauth/profile", "/api/organizations/org/usage",
                 "/v1/claude_code/routines/example/fire", "//evil.example", "/api/oauth/usage?token=FAKE")
        with patch("socket.socket") as network:
            for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"):
                for path in paths:
                    if method == "GET" and path == "/api/oauth/usage":
                        continue
                    with self.subTest(method=method, path=path), self.assertRaises(app.live_usage.LiveError):
                        app.UsageClient().request(method, "https://api.anthropic.com" + path, "FAKE-TOKEN-ONLY")
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
