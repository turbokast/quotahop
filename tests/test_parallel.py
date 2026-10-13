"""Real short-lived workers; fake requests/credentials, never real network."""
import datetime as dt
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

import claude_usage as app
import live_usage as live
import muse_cli_usage as muse
import parallel_usage as parallel


def timed_task():
    start = time.monotonic()
    time.sleep(0.08)
    return {"pid": os.getpid(), "start": start, "end": time.monotonic()}


class WorkerTests(unittest.TestCase):
    def assert_reaped(self, pids):
        for pid in pids:
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def track_forks(self, pids):
        fork = os.fork
        def tracked():
            pid = fork()
            if pid:
                pids.append(pid)
            return pid
        return patch.object(parallel.os, "fork", side_effect=tracked)

    def test_bounded_overlap_results_in_order_and_all_workers_reaped(self):
        results = parallel.run([timed_task] * 8, workers=4)
        events = sorted([(r["start"], 1) for r in results] + [(r["end"], -1) for r in results])
        active = peak = 0
        for _, delta in events:
            active += delta
            peak = max(peak, active)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assert_reaped([r["pid"] for r in results])

    def test_timeout_and_crash_are_bounded_redacted_and_reaped(self):
        def stuck():
            time.sleep(60)
        def broken():
            raise RuntimeError("FAKE-SECRET-MUST-NOT-APPEAR")
        pids = []
        with self.track_forks(pids):
            results = parallel.run([stuck, broken], timeout=0.05)
        self.assertEqual(results, [None, None])
        self.assert_reaped(pids)

    def test_interrupt_and_termination_clean_up_and_restore_handlers(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handler = signal.getsignal(sig)
            def interrupt():
                os.kill(os.getppid(), sig)
                time.sleep(60)
            pids = []
            with self.track_forks(pids), self.assertRaises(KeyboardInterrupt):
                parallel.run([interrupt, timed_task], timeout=1)
            self.assert_reaped(pids)
            self.assertEqual(signal.getsignal(sig), handler)

    def test_partial_fork_failure_reaps_already_started_workers(self):
        fork = os.fork
        pids = []
        def fail_second():
            if pids:
                raise OSError("fake resource exhaustion")
            pid = fork()
            if pid:
                pids.append(pid)
            return pid
        with patch.object(parallel.os, "fork", side_effect=fail_second), self.assertRaises(OSError):
            parallel.run([timed_task] * 2)
        self.assert_reaped(pids)


class ParallelReportTests(unittest.TestCase):
    def test_combined_live_cycle_deduplicates_and_never_modifies_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = app.Store(Path(tmp) / "data", create=True)
            self.addCleanup(store.__exit__, None, None, None)
            log = Path(tmp) / "fake-requests.jsonl"
            source = Path(tmp) / "fake-provider-auth"
            source.write_text("FAKE-CREDENTIAL-CANARY")
            before = source.stat()
            accounts = app.default_accounts() + [
                {"id": "codex-1", "name": "Codex", "plan": "codex"},
                {"id": "muse-1", "name": "Muse", "plan": "muse_code", "source": "cli_current"}]
            entries = {a["id"]: {"access_token": "FAKE-ACCESS-" + str(i), "expires_at": None}
                       for i, a in enumerate(accounts[:7])}
            entries["team-5"] = dict(entries["team-4"])
            entries["codex-1"] = {"provider": "codex", "access_token": "FAKE-CODEX-ACCESS",
                                  "expires_at": None, "account_id": "fake-workspace"}

            def request(client, method, url, token, account_id=None):
                with log.open("a") as stream:
                    stream.write(json.dumps([method, url]) + "\n")
                time.sleep(0.03)
                if url == muse.URL:
                    return {"subs_usage": {"weekly": {"used_percent": 15}}}
                if url == live.CODEX_URL:
                    return {"rate_limit": None, "plan_type": "pro"}
                if token == "FAKE-ACCESS-0":
                    raise live.LiveError("authentication_error", "Expired.")
                return {"five_hour": {"utilization": 42}}

            with patch.object(live.UsageClient, "request", request), patch.object(muse.MuseCliClient, "request", request), patch.object(
                    muse, "read_credentials", return_value=("dca:FAKE-DEVICE-TOKEN", "FAKE-API-KEY")), patch.object(
                    live, "read_access_token", side_effect=AssertionError("No auth source reads")), patch.object(
                    app.Store, "write", side_effect=AssertionError("No report writes")):
                result = app.concurrent_report(accounts, store, dt.datetime.now(app.UTC), entries)
            self.assertEqual([a["id"] for a in result["accounts"]], [a["id"] for a in accounts])
            self.assertEqual(result["live_accounts"], 7)
            self.assertEqual(result["accounts"][0]["error_code"], "authentication_error")
            self.assertEqual(result["accounts"][6]["error_code"], "duplicate_account")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(calls.count(["GET", live.URL]), 6)
            self.assertEqual(calls.count(["GET", live.CODEX_URL]), 1)
            self.assertEqual(calls.count(["POST", muse.URL]), 1)
            self.assertEqual(len(calls), 8)
            self.assertNotIn("FAKE-ACCESS", json.dumps(result))
            self.assertEqual(source.stat(), before)
            self.assertEqual([p.name for p in (Path(tmp) / "data").iterdir()], ['quota-cache'])

    def test_worker_failure_remains_visible_without_retry(self):
        accounts = app.default_accounts()[:2]
        entries = {a["id"]: {"access_token": "FAKE-" + a["id"], "expires_at": None} for a in accounts}
        with patch.object(parallel, "run", return_value=[None, None]) as run:
            result = app.concurrent_report(accounts, None, dt.datetime.now(app.UTC), entries)
        self.assertFalse(result["live"])
        self.assertEqual([a["error_code"] for a in result["accounts"]], ["worker_failed"] * 2)
        run.assert_called_once()
