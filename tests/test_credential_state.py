"""Selection badges use fake credentials, never email guesses or live auth."""
import contextlib
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import claude_switch
import claude_usage as app
import credential_state as state
import live_usage as live

NOW = dt.datetime(2026, 10, 13, tzinfo=app.UTC)


def row(ident, plan="max_20x", used=20):
    return {"id": ident, "name": "same@example.test", "provider": app.PROVIDERS[plan], "plan": plan,
            "status": "live", "live": True, "captured_at": NOW.isoformat(), "reported_plan": None,
            "usage": app.parse_usage({"five_hour": {"utilization": used,
                "resets_at": (NOW + dt.timedelta(hours=1)).isoformat()}})}


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.entries = {"max": {"access_token": "FAKE-MAX-TOKEN", "expires_at": NOW.timestamp() + 3600},
                        "team": {"access_token": "FAKE-TEAM-TOKEN", "expires_at": NOW.timestamp() + 3600},
                        "codex": {"provider": "codex", "access_token": "FAKE-CODEX-TOKEN", "account_id": "workspace",
                                  "expires_at": NOW.timestamp() + 3600}}
        self.data = {"accounts": [row("max"), row("team", "team_premium"), row("codex", "codex"),
                                  {**row("muse", "muse_code"), "quota_scope": "current_muse_cli_login"}]}
        self.current = {"claude": state.fingerprint(self.entries["team"], "claude"),
                        "codex": state.fingerprint(self.entries["codex"], "codex"), "muse_code": b"fake"}

    def annotate(self, before=None, after=None):
        return state.annotate(self.data, self.entries, self.current if before is None else before,
                              self.current if after is None else after, NOW)

    def test_same_email_max_team_and_three_providers_exact_matching(self):
        before = copy.deepcopy(self.data)
        result = self.annotate()
        self.assertEqual([r["selected"] for r in result["accounts"]], [False, True, True, True])
        self.assertEqual(self.data, before)
        self.assertNotIn("FAKE-", json.dumps(result))
        self.assertEqual([r["switch_readiness"] for r in result["accounts"]],
                         ["ready", "ready", "not_switchable", "not_switchable"])

    def test_changed_unreadable_or_unknown_current_token_is_not_guessed_by_email(self):
        for after in ({}, {"claude": None}, {"claude": (b"rotated", None)}):
            self.assertTrue(all(r["selected"] is None for r in self.annotate(after=after)["accounts"][:2]))
        unknown = {"claude": (b"rotated", None)}
        self.assertTrue(all(r["selected"] is None for r in self.annotate(unknown, unknown)["accounts"][:2]))
        self.entries["max"]["access_token"] = self.entries["team"]["access_token"]
        self.assertTrue(all(r["selected"] is None for r in self.annotate()["accounts"][:2]))

    def test_codex_account_routing_and_muse_portal_do_not_match_other_contexts(self):
        self.entries["codex"]["account_id"] = "another-workspace"
        self.data["accounts"][3].pop("quota_scope")
        result = self.annotate()
        self.assertIsNone(result["accounts"][2]["selected"])
        self.assertIsNone(result["accounts"][3]["selected"])

    def test_readiness_distinguishes_expiry_scope_limits_and_unavailable_usage(self):
        for fields, expected in (({"expires_at": NOW.timestamp() - 1}, "expired"),
                                 ({"expires_at": NOW.timestamp() + 60}, "expiring"),
                                 ({"expires_at": None}, "expiry_unknown"),
                                 ({"scopes": ["user:profile"]}, "scope_missing")):
            entry = dict(self.entries["max"])
            self.entries["max"].update(fields)
            self.assertEqual(self.annotate()["accounts"][0]["switch_readiness"], expected)
            self.entries["max"] = entry
        for changed, expected in ((row("max", used=100), "exhausted"),
                                  (row("max", used=None), "usage_unknown"),
                                  ({**row("max"), "live": False, "status": "error"}, "unverified")):
            self.data["accounts"][0] = changed
            self.assertEqual(self.annotate()["accounts"][0]["switch_readiness"], expected)
        del self.entries["max"]
        self.assertEqual(self.annotate()["accounts"][0]["switch_readiness"], "not_captured")

    def test_highlight_legend_alignment_and_plain_output(self):
        result = self.annotate()
        plain, color = app.render(result, NOW, width=300), app.render(result, NOW, color=True, width=300)
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", color), plain)
        self.assertEqual(plain.count("▶ SELECTED"), 3)
        self.assertIn("○ Standby", plain)
        self.assertIn("✓ Ready · 1h 0m", plain)
        self.assertIn("already-running sessions may use another login", plain)
        self.assertIn("\x1b[1;36;7m", color)
        table_lines = [line for line in plain.splitlines() if line.startswith(("╭", "│", "├", "╰"))]
        self.assertEqual(len({app.display_width(line) for line in table_lines}), 1)

    def test_compact_ready_retains_live_status_and_explains_checkmark(self):
        result = self.annotate()
        text = app.render(result, NOW, width=100)
        table_lines = [line for line in text.splitlines() if line.startswith("│")]
        self.assertTrue(any("● LIVE ✓" in line for line in table_lines))
        self.assertTrue(any("● LIVE" in line and "✓" not in line for line in table_lines))
        self.assertIn("not renewal status", text)
        self.assertTrue(all(app.display_width(line) <= 100 for line in text.splitlines()))

    def test_only_read_only_credential_files_are_opened_and_no_process_or_network(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            claude, codex = root / "claude.json", root / "codex.json"
            claude.write_text(json.dumps({"claudeAiOauth": {"accessToken": "FAKE-CLAUDE-TOKEN",
                                    "refreshToken": "NEVER-USE-REFRESH", "expiresAt": 9999999999999}}))
            codex.write_text(json.dumps({"tokens": {"access_token": "FAKE-CODEX-TOKEN", "account_id": "workspace",
                                                    "refresh_token": "NEVER-USE-REFRESH"}}))
            for path in (claude, codex):
                path.chmod(0o600)
            before = [p.stat() for p in (claude, codex)]
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            stack.enter_context(patch.object(claude_switch, "paths", return_value=(claude, root / "unused.json")))
            stack.enter_context(patch.object(live, "default_source", return_value=codex))
            for target in ("socket.socket", "subprocess.Popen", "os.fork", "os.system", "time.sleep"):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            result = state.current(self.data["accounts"][:3])
            self.assertIsNotNone(result["claude"])
            self.assertIsNotNone(result["codex"])
            self.assertEqual([p.stat() for p in (claude, codex)], before)
            with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "FAKE-OVERRIDE"}):
                self.assertIsNone(state.current(self.data["accounts"][:1])["claude"])
            with patch.object(live, "read_access_token", side_effect=AssertionError("must not read")):
                self.assertEqual(state.current(self.data["accounts"], {}), {})
