"""Automatic switch on a usage-limit stop. Fake store, settings and Herdr; no network."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import auto_switch
import claude_usage as app
import live_usage as live

SESSION = "11111111-2222-4333-8444-555555555555"
OTHER = "66666666-7777-4888-9999-aaaaaaaaaaaa"
ACCOUNTS = [{"id": "max-1", "name": "Max 1", "plan": "max_20x"},
            {"id": "team-1", "name": "Team 1", "plan": "team_premium"}]


def event(session=SESSION, error="rate_limit", name="StopFailure"):
    return json.dumps({"hook_event_name": name, "error": error, "session_id": session,
                       "transcript_path": "/fake", "cwd": "/fake"}).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = app.Store(self.root / "tool", create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.notes = []

    def queue(self, *sessions, at=None):
        for session in sessions:
            auto_switch.hook(self.store, self.root / "tool", event(session), spawn=lambda _: None, now=at)


class HookTests(Base):
    def test_parse_event_accepts_only_limit_stops(self):
        self.assertEqual(auto_switch.parse_event(event()), SESSION)
        self.assertEqual(auto_switch.parse_event(event(error="billing_error")), SESSION)
        self.assertEqual(auto_switch.parse_event(event(SESSION.upper())), SESSION)
        for raw in (event(error="overloaded"), event(name="Stop"), event(session="../x"), b"{", b"\xff",
                    b"x" * (auto_switch.MAX_HOOK_INPUT + 1), b"[]"):
            self.assertIsNone(auto_switch.parse_event(raw))

    def test_hook_queues_once_and_starts_one_worker(self):
        started = []
        self.assertEqual(auto_switch.hook(self.store, "d", event(), spawn=started.append), "started")
        self.assertEqual(auto_switch.hook(self.store, "d", event(), spawn=started.append), "started")
        self.assertEqual([p["session_id"] for p in auto_switch.load(self.store)["pending"]], [SESSION])
        self.assertEqual(len(started), 2)
        self.assertEqual(auto_switch.hook(self.store, "d", event(name="Stop"), spawn=started.append), "ignored")

    def test_hook_does_not_start_second_worker(self):
        import fcntl
        fd = auto_switch._lock_file(self.store, auto_switch.WORKER_LOCK)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = []
        self.assertEqual(auto_switch.hook(self.store, "d", event(), spawn=started.append), "queued")
        self.assertEqual(started, [])

    def test_state_contains_no_credentials(self):
        self.queue(SESSION)
        raw = (self.root / "tool" / auto_switch.STATE).read_text()
        self.assertNotIn("token", raw.lower())
        self.assertEqual(os.stat(self.root / "tool" / auto_switch.STATE).st_mode & 0o777, 0o600)

    def test_cli_hook_never_fails(self):
        with patch("sys.stdin") as stdin:
            stdin.buffer.read.return_value = b"not json"
            self.assertEqual(app.main(["--data-dir", str(self.root / "missing"), "--auto-switch", "hook"]), 0)

    def test_reserved_account_id(self):
        with self.assertRaises(app.UsageError):
            app.validate_accounts({"accounts": [{"id": "auto-switch", "name": "x", "plan": "max_20x"}]})


class WorkerTests(Base):
    def run_worker(self, *, verdict, switch=None, nudged=None, key=b"k" * 32, current=ACCOUNTS[0]):
        calls = []

        def switcher(store, accounts, **kwargs):
            calls.append(kwargs)
            if isinstance(switch, Exception):
                raise switch
            return switch or {"changed": True, "account_name": "team@example.test", "plan": "team_premium",
                              "account_id": "team-1"}

        def vault_key(store, interactive):
            if key is None:
                raise live.LiveError("vault_locked", "locked")
            return key
        prompts = []

        def nudge(sessions, text):
            prompts.append((list(sessions), text))
            reachable = [s for s in sessions if nudged is None or s in nudged]
            return reachable, [s for s in sessions if s not in reachable]
        with patch.object(auto_switch, "vault_key", vault_key), \
                patch.object(auto_switch, "current_account", lambda *a: (current, {})), \
                patch.object(auto_switch, "at_limit", lambda *a: verdict), \
                patch.object(auto_switch, "nudge", nudge):
            result = auto_switch.run(self.store, ACCOUNTS, notify=self.notes.append, switcher=switcher)
        return result, calls, prompts

    def test_switches_excluding_limited_login_and_resumes_sessions(self):
        self.queue(SESSION, OTHER)
        result, calls, prompts = self.run_worker(verdict=True, nudged={SESSION})
        self.assertEqual(result["outcome"], "switched")
        self.assertEqual(calls, [{"automatic": True, "vault_key": b"k" * 32, "exclude": {"max-1"}}])
        self.assertEqual(prompts[0][0], [SESSION, OTHER])
        self.assertIn("switched to another subscription (team@example.test · Team Premium)", prompts[0][1])
        self.assertIn("resume everything", prompts[0][1])
        self.assertEqual(result["resumed_sessions"], 1)
        self.assertEqual(result["unreachable_sessions"], 1)
        self.assertIn("not in Herdr", self.notes[-1])
        state = auto_switch.load(self.store)
        self.assertEqual(state["pending"], [])
        self.assertEqual(len(state["switches"]), 1)
        self.assertEqual(state["last_switch"]["to"], "team-1")

    def test_locked_vault_keeps_sessions_queued(self):
        self.queue(SESSION)
        result, calls, prompts = self.run_worker(verdict=True, key=None)
        self.assertEqual(result["outcome"], "locked")
        self.assertEqual((calls, prompts), ([], []))
        self.assertEqual(len(auto_switch.load(self.store)["pending"]), 1)
        self.assertIn("--auto-switch run", self.notes[-1])

    def test_unverified_usage_never_switches(self):
        self.queue(SESSION)
        result, calls, _ = self.run_worker(verdict=None)
        self.assertEqual(result["outcome"], "check_failed")
        self.assertEqual(calls, [])
        self.assertEqual(len(auto_switch.load(self.store)["pending"]), 1)
        result, calls, _ = self.run_worker(verdict=True, current=None)
        self.assertEqual(result["outcome"], "check_failed")

    def test_headroom_without_recent_switch_does_nothing(self):
        self.queue(SESSION)
        result, calls, prompts = self.run_worker(verdict=False)
        self.assertEqual(result["outcome"], "not_at_limit")
        self.assertEqual((calls, prompts), ([], []))
        self.assertEqual(auto_switch.load(self.store)["pending"], [])

    def test_session_stopped_on_outgoing_login_is_resumed_without_switch(self):
        self.queue(SESSION)
        self.run_worker(verdict=True)
        self.queue(OTHER)
        result, calls, prompts = self.run_worker(verdict=False)
        self.assertEqual(result["outcome"], "resumed")
        self.assertEqual(calls, [])
        self.assertEqual(prompts[0][0], [OTHER])
        self.assertIn("is now on another subscription", prompts[0][1])

    def test_switch_cap(self):
        for _ in range(auto_switch.MAX_SWITCHES):
            self.queue(SESSION)
            self.assertEqual(self.run_worker(verdict=True)[0]["outcome"], "switched")
        self.queue(SESSION)
        result, calls, _ = self.run_worker(verdict=True)
        self.assertEqual(result["outcome"], "throttled")
        self.assertEqual(calls, [])

    def test_resume_prompt_cap_per_session(self):
        for _ in range(auto_switch.MAX_NUDGES):
            self.queue(SESSION)
            self.assertEqual(self.run_worker(verdict=True)[0]["resumed_sessions"], 1)
        self.queue(SESSION)
        result, _, prompts = self.run_worker(verdict=False)
        self.assertEqual(prompts[-1][0], [])
        self.assertIn("not prompted again", result["message"])

    def test_no_candidate_drops_queue_and_notifies(self):
        self.queue(SESSION)
        error = live.LiveError("switch_refused", "Cannot verify a switchable Claude subscription: 6 at quota limit.")
        result, _, prompts = self.run_worker(verdict=True, switch=error)
        self.assertEqual(result["outcome"], "no_candidate")
        self.assertEqual(prompts, [])
        self.assertEqual(auto_switch.load(self.store)["pending"], [])
        self.assertIn("at quota limit", self.notes[-1])

    def test_refused_switch_keeps_queue(self):
        self.queue(SESSION)
        error = live.LiveError("switch_refused", "Another capture/switch is active; retry when it finishes.")
        result, _, _ = self.run_worker(verdict=True, switch=error)
        self.assertEqual(result["outcome"], "switch_failed")
        self.assertEqual(len(auto_switch.load(self.store)["pending"]), 1)

    def test_stale_sessions_expire(self):
        self.queue(SESSION, at=time.time() - auto_switch.PENDING_TTL - 1)
        result, calls, _ = self.run_worker(verdict=True)
        self.assertEqual(result["outcome"], "idle")
        self.assertEqual(calls, [])

    def test_busy_worker(self):
        import fcntl
        fd = auto_switch._lock_file(self.store, auto_switch.WORKER_LOCK)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(auto_switch.run(self.store, ACCOUNTS)["outcome"], "busy")


class NudgeTests(unittest.TestCase):
    def agents(self):
        return json.dumps({"result": {"agents": [
            {"agent": "claude", "pane_id": "wA:p1", "agent_session": {"value": SESSION}},
            {"agent": "codex", "pane_id": "wB:p1", "agent_session": {"value": OTHER}}]}}).encode()

    def test_prompts_matching_claude_pane_only(self):
        calls = []

        def herdr(*args, timeout=15):
            calls.append(args)
            return self.agents() if args == ("agent", "list") else b"{}"
        with patch.object(auto_switch, "herdr", herdr):
            self.assertEqual(auto_switch.nudge([SESSION, OTHER], "resume"), ([SESSION], [OTHER]))
        self.assertIn(("agent", "prompt", "wA:p1", "resume"), calls)

    def test_falls_back_to_typing_when_prompt_refused(self):
        calls = []

        def herdr(*args, timeout=15):
            calls.append(args)
            if args[:2] == ("agent", "prompt"):
                raise OSError()
            return self.agents()
        with patch.object(auto_switch, "herdr", herdr):
            self.assertEqual(auto_switch.nudge([SESSION], "resume"), ([SESSION], []))
        self.assertEqual(calls[-2:], [("pane", "send-text", "wA:p1", "resume"), ("pane", "send-keys", "wA:p1", "enter")])

    def test_without_herdr_every_session_is_unreachable(self):
        with patch.object(auto_switch, "herdr", side_effect=OSError()):
            self.assertEqual(auto_switch.nudge([SESSION], "x"), ([], [SESSION]))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.json"
        self.other = {"matcher": "rate_limit", "hooks": [{"type": "command", "command": "other-tool"}]}
        self.path.write_text(json.dumps({"model": "opus", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "keep"}]}],
            "StopFailure": [self.other]}}))
        self.path.chmod(0o644)

    def test_enable_is_idempotent_and_preserves_other_settings(self):
        with patch("login_keyring.enabled", return_value=True):
            auto_switch.enable("/data", self.path)
            auto_switch.enable("/data", self.path)
        value = json.loads(self.path.read_text())
        self.assertEqual(value["model"], "opus")
        self.assertEqual(value["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": "keep"}]}])
        groups = value["hooks"]["StopFailure"]
        self.assertEqual(groups[0], self.other)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1]["matcher"], "rate_limit|billing_error")
        self.assertTrue(groups[1]["hooks"][0]["command"].endswith("--data-dir /data --auto-switch hook"))
        self.assertTrue(auto_switch.installed(self.path))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_disable_removes_only_ours(self):
        with patch("login_keyring.enabled", return_value=False):
            message = auto_switch.enable("/data", self.path)["message"]
        self.assertIn("enable-autostart", message)
        auto_switch.disable(self.path)
        value = json.loads(self.path.read_text())
        self.assertEqual(value["hooks"]["StopFailure"], [self.other])
        self.assertFalse(auto_switch.installed(self.path))

    def test_enable_creates_missing_settings_and_disable_cleans_up(self):
        path = Path(self.tmp.name) / "new" / "settings.json"
        with patch("login_keyring.enabled", return_value=True):
            auto_switch.enable("/data", path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        auto_switch.disable(path)
        self.assertEqual(json.loads(path.read_text()), {})

    def test_rejects_invalid_settings(self):
        self.path.write_text('{"hooks": {"StopFailure": {}}}')
        with self.assertRaises(live.LiveError):
            auto_switch.enable("/data", self.path)


class PlumbingTests(unittest.TestCase):
    def test_recommended_account_excludes_limited_login(self):
        import claude_switch
        seen = []

        def report(candidates, *a):
            seen.extend(c["id"] for c in candidates)
            return {"accounts": []}
        with patch.object(claude_switch, "select", lambda *a: None), \
                patch("claude_usage.concurrent_report", report):
            with self.assertRaises(live.LiveError):
                claude_switch.recommended_account(None, ACCOUNTS, {}, exclude={"max-1"})
        self.assertEqual(seen, ["team-1"])


if __name__ == "__main__":
    unittest.main()
