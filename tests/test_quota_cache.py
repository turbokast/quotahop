"""Fake quotas only: back-to-back report/switch checks must not trigger bursts."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import claude_usage as app
import credential_state
import live_usage as live
import quota_cache as cache


class QuotaCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'tool'
        self.store = app.Store(self.path, create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.accounts = app.default_accounts()
        self.entries = {a['id']: {'access_token': 'FAKE-CACHE-' + a['id'],
                        'expires_at': time.time() + 3600} for a in self.accounts}
        self.raw = {'five_hour': {'utilization': 20}, 'seven_day': {'utilization': 40}}
        self.network = patch.object(live.UsageClient, 'request', return_value=self.raw).start()
        self.addCleanup(patch.stopall)

    def report(self):
        return app.report(self.accounts, self.store, dt.datetime.now(app.UTC), self.entries)

    def test_seven_accounts_shared_across_reports_and_switch_ranking(self):
        import claude_switch
        first = self.report()
        self.assertEqual(self.network.call_count, 7)
        self.assertEqual(first['live_accounts'], 7)
        again = self.report()
        self.assertEqual(self.network.call_count, 7)
        self.assertEqual(again['live_accounts'], 0)
        self.assertTrue(all(r['status'] == 'recent' and not r['live'] for r in again['accounts']))
        self.assertEqual([r['captured_at'] for r in first['accounts']], [r['captured_at'] for r in again['accounts']])
        winner = claude_switch.recommended_account(self.store, self.accounts, self.entries, jobs=1)
        self.assertIn(winner, self.entries)
        self.assertEqual(self.network.call_count, 7)
        now = dt.datetime.now(app.UTC)
        annotated = credential_state.annotate(again, self.entries, {}, {}, now)
        self.assertTrue(all(r['switch_readiness'] == 'ready' for r in annotated['accounts']))
        self.assertIn('RECENT', app.render(annotated, now, width=100))
        for path in (self.path / cache.DIRECTORY).iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for entry in self.entries.values():
                self.assertNotIn(entry['access_token'], path.read_text())

    def test_cache_expires_and_identity_rotation_never_reuses_another_grant(self):
        first = self.report()
        self.entries['max-1']['access_token'] += '-REPLACED'
        self.report()
        self.assertEqual(self.network.call_count, 8)
        with patch.object(cache.time, 'time', return_value=time.time() + 61):
            self.report()
        self.assertEqual(self.network.call_count, 15)
        old = {**first['accounts'][0], 'status': 'recent', 'live': False}
        self.assertEqual(app.recommendation(old, dt.datetime.now(app.UTC) + dt.timedelta(seconds=61))[0], 3)

    def test_cooldown_survives_reopen_and_never_claims_exhausted_or_retries(self):
        self.network.side_effect = live.LiveError('rate_limited', 'HTTP 429', retry_after=400)
        first = self.report()
        self.assertEqual(self.network.call_count, 7)
        with app.Store(self.path) as reopened:
            second = app.report(self.accounts, reopened, dt.datetime.now(app.UTC), self.entries)
        self.assertEqual(self.network.call_count, 7)
        for row in first['accounts'] + second['accounts']:
            self.assertEqual(row['error_code'], 'rate_limited')
            self.assertIsNone(row['usage'])
            self.assertEqual(app.recommendation(row, dt.datetime.now(app.UTC))[0], 3)
        text = app.render(second, dt.datetime.now(app.UTC), width=100)
        self.assertIn('THROTTLED', text)
        self.assertIn('quota unknown, not exhausted', text)
        self.network.side_effect = None
        with patch.object(cache.time, 'time', return_value=time.time() + 401):
            self.report()
        self.assertEqual(self.network.call_count, 14)

    def test_crossing_reset_never_uses_old_hundred_percent(self):
        reset = dt.datetime.now(app.UTC) + dt.timedelta(seconds=10)
        self.network.return_value = {'five_hour': {'utilization': 100, 'resets_at': reset.isoformat()}}
        self.report()
        self.network.return_value = self.raw
        with patch.object(cache.time, 'time', return_value=reset.timestamp() + 1):
            result = self.report()
        self.assertEqual(self.network.call_count, 14)
        self.assertTrue(all(r['usage']['five_hour']['utilization'] == 20 for r in result['accounts']))

    def test_failed_new_checks_never_fall_back_to_cached_usage(self):
        self.report()
        self.network.side_effect = live.LiveError('authentication_error', 'Rejected')
        with patch.object(cache.time, 'time', return_value=time.time() + 61):
            result = self.report()
        self.assertTrue(all(r['usage'] is None and r['error_code'] == 'authentication_error' for r in result['accounts']))

    def test_symlink_cache_does_not_touch_target_or_make_requests(self):
        target = self.path.parent / 'untouched'
        target.mkdir(mode=0o700)
        (self.path / cache.DIRECTORY).symlink_to(target)
        self.report()
        self.network.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])

    def test_duplicate_tokens_still_rejected_on_cache_hit(self):
        self.report()
        self.entries['max-2'] = self.entries['max-1']
        result = self.report()
        self.assertEqual(result['accounts'][1]['error_code'], 'duplicate_account')
        self.assertEqual(self.network.call_count, 7)

    def test_concurrent_readers_share_one_request(self):
        import parallel_usage
        entry = self.entries['max-1']
        def slow_request(*args):
            time.sleep(.1)
            return self.raw
        self.network.side_effect = slow_request
        def run():
            parsed, checked, reused = cache.fetch(self.store, 'max-1', entry, set())
            return {'used': parsed['five_hour']['utilization'], 'reused': reused}
        result = parallel_usage.run([run, run], workers=2)
        self.assertEqual(sorted(r['reused'] for r in result), [False, True])
        self.assertEqual([r['used'] for r in result], [20, 20])
        with self.assertRaises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)


class RetryHeaderTests(unittest.TestCase):
    def test_header_is_only_a_safe_numeric_delay(self):
        for value, expected in ((None, 300), ('invalid-FAKE-SECRET', 300), ('0', 300),
                                ('900', 900), ('999999999', 86400)):
            self.assertEqual(live.retry_delay(value), expected)
        from email.utils import format_datetime
        future = format_datetime(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=600))
        self.assertTrue(599 <= live.retry_delay(future) <= 600)
