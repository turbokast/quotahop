"""Foreground panel tests: fake vaults, fake quotas, no provider requests."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import tomllib
import unittest
from unittest.mock import MagicMock, patch

import claude_usage as app
import live_usage as live
import usage_panel as panel

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("herdr_plugin", ROOT / "plugins/herdr/plugin.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
NOW = dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)
KEY, SALT = b"k" * 32, b"s" * 16
ENTRY = {"access_token": "FAKE-PANEL-ACCESS-ONLY", "expires_at": NOW.timestamp() + 3600}


def fixture():
    accounts = app.default_accounts() + [
        {"id": "codex", "name": "coder@example.test", "plan": "codex"},
        {"id": "muse", "name": "muse@example.test", "plan": "muse_code"}]
    for a in accounts:
        a.update(provider=app.PROVIDERS[a['plan']], status="live", live=True,
                 selected=a['id'] == 'max-1', switch_readiness='ready',
                 usage=app.parse_usage({"five_hour": {"utilization": 42, "resets_at": (NOW + dt.timedelta(hours=2)).isoformat()},
                                       "seven_day": {"utilization": 64, "resets_at": (NOW + dt.timedelta(days=3)).isoformat()}}))
    return {"accounts": accounts, "generated_at": NOW.isoformat(), "live_accounts": 9}


class RenderTests(unittest.TestCase):
    def test_all_nine_accounts_groups_plans_and_selection_fit_small_panel(self):
        data = fixture()
        rows = panel.lines(data, 43, NOW, details=True)
        text = '\n'.join(s for s, _ in rows)
        for a in data['accounts']:
            self.assertIn(a['name'], text)
        self.assertIn('▶ Max 1 · Max ✓', text)
        self.assertIn('Team', text)
        self.assertIn('42%', text)
        self.assertIn('2h00', text)
        self.assertLess(text.index('OPENAI'), text.index('ANTHROPIC'))
        self.assertLess(text.index('ANTHROPIC'), text.index('META'))
        self.assertTrue(all(app.display_width(s) <= 43 for s, _ in rows))

    def test_unknown_errors_overage_and_snapshots_remain_honest(self):
        data = fixture()
        data['accounts'][0].update(status='error', error_code='authentication_error', usage=None, live=False)
        data['accounts'][1].update(status='not_configured', usage=None, live=False)
        data['accounts'][2].update(status='snapshot', live=False)
        data['accounts'][-1]['usage']['five_hour']['utilization'] = 101
        data['accounts'][-2]['usage']['five_hour'] = {'utilization': None, 'resets_at': None}
        text = '\n'.join(s for s, _ in panel.lines(data, 43, NOW, details=True))
        for term in ('AUTH ERROR', 'SETUP', '◷', '101%', '—'):
            self.assertIn(term, text)
        self.assertNotIn('0%', text)

    def test_small_width_unicode_controls_and_details(self):
        data = fixture()
        data['accounts'][0]['name'] = '名字é' * 15 + '\x1b\r\n'
        for width in (0, 1, 12, 26, 43, 80):
            for s, _ in panel.lines(data, width, NOW, details=True):
                self.assertLessEqual(app.display_width(s), width)
                self.assertNotIn('\x1b', s)
                self.assertNotIn('\r', s)
        self.assertIn('13 Oct', '\n'.join(s for s, _ in panel.lines(data, 43, NOW, details=True)))

    def test_expired_alternatives_and_renewal_problems_are_named(self):
        data = fixture()
        claude = [a for a in data['accounts'] if a['provider'] == 'claude']
        for a in claude[1:]:
            a.update(status='error', error_code='token_expired', usage=None, live=False,
                     switch_readiness='expired', selected=False, renewal_state='suspended')
        text = '\n'.join(s for s, _ in panel.summary_lines(data, 60, NOW))
        self.assertIn(f'All {len(claude) - 1} expired · re-enroll', text)
        self.assertIn('Renewal stopped', text)
        self.assertNotIn('Check failed', text)
        claude[1]['renewal_state'] = 'blocked'
        data['cli_sync_problem'] = 'Current CLI identity has no unique saved account/organization binding.'
        text = '\n'.join(s for s, _ in panel.summary_lines(data, 120, NOW))
        self.assertIn('Expired · renewal paused', text)
        self.assertIn('Renewal paused (1)', text)
        self.assertIn('CLI sync: Current CLI identity', text)
        self.assertEqual(app.error_label({'error_code': 'duplicate_account'}), 'DUPLICATE')
        self.assertEqual(app.error_label({'error_code': 'token_expired'}), 'EXPIRED')

    def test_countdown(self):
        for seconds, expected in ((-1, 'due'), (30, '<1m'), (600, '10m'), (3900, '1h05'), (90000, '1d01')):
            self.assertEqual(panel.countdown((NOW + dt.timedelta(seconds=seconds)).isoformat(), NOW), expected)
        self.assertEqual(panel.countdown(None, NOW), '—')

    def test_summary_only_shows_active_and_next_remaining(self):
        data = fixture()
        data['accounts'][-2]['selected'] = True
        data['accounts'][-1]['selected'] = True
        text = '\n'.join(s for s, _ in panel.lines(data, 43, NOW))
        self.assertEqual(text.count('Active'), 3)
        self.assertEqual(text.count('Next'), 3)
        self.assertIn('58%', text)
        self.assertIn('36%', text)
        self.assertNotIn('42%', text)
        self.assertNotIn('Max 1', text)
        self.assertNotIn('@', text)
        self.assertNotIn('reset', text)
        self.assertLessEqual(len(text.splitlines()), 12)

    def test_next_uses_recommendation_and_excludes_unusable_accounts(self):
        rows = fixture()['accounts'][:7]
        active = rows[0]
        rows[1]['usage']['five_hour']['utilization'] = 0
        rows[1]['switch_readiness'] = 'expired'
        rows[2]['usage']['five_hour']['utilization'] = 100
        rows[3].update(status='snapshot', live=False)
        rows[4]['selected'] = None
        rows[5]['usage']['five_hour']['utilization'] = 10
        rows[5]['usage']['seven_day']['utilization'] = 10
        self.assertEqual(panel.next_account(list(reversed(rows)), active, NOW)['id'], rows[5]['id'])
        rows[5]['credential_expires_at'] = NOW.timestamp() - 1
        self.assertEqual(panel.next_account(rows, active, NOW)['id'], rows[6]['id'])

    def test_unknown_selection_does_not_invent_active_or_alternative(self):
        data = fixture()
        for row in data['accounts']:
            row['selected'] = None
        text = '\n'.join(s for s, _ in panel.lines(data, 43, NOW))
        self.assertEqual(text.count('Active  '), 3)
        self.assertIn('Unverified', text)
        self.assertEqual(text.count('Saved?'), 2)  # Single saved Codex/Muse, explicitly unverified.
        self.assertIsNone(panel.next_account([data['accounts'][0]], data['accounts'][0], NOW))

    def test_summary_missing_overage_small_remaining_and_error(self):
        data = fixture()
        row = data['accounts'][0]
        row['usage']['five_hour']['utilization'] = 101
        row['usage']['seven_day']['utilization'] = None
        text = '\n'.join(s for s, _ in panel.lines(data, 43, NOW))
        self.assertIn('Active      0%       —', text)
        row['usage']['five_hour']['utilization'] = 99.9
        self.assertEqual(panel.remaining(row, 'five_hour'), '<1%')
        row.update(live=False, status='error')
        text = '\n'.join(s for s, _ in panel.lines(data, 43, NOW))
        self.assertIn('Active  ERROR', text)

    def test_failures_are_never_reported_as_exhausted_and_best_survives_unknown_selection(self):
        data = fixture()
        for row in data['accounts']:
            row['selected'] = None
        text = '\n'.join(s for s, _ in panel.lines(data, 32, NOW))
        self.assertIn('Best?', text)
        self.assertIn('58%', text)
        data['accounts'][0]['selected'] = True
        for row in data['accounts'][1:7]:
            row.update(status='error', live=False, error_code='rate_limited', usage=None)
        text = '\n'.join(s for s, _ in panel.lines(data, 60, NOW))
        self.assertIn('Throttled · quota unknown', text)
        self.assertNotIn('At quota limit', text)
        self.assertEqual(panel.cooldown(data), 300)
        for row in data['accounts'][1:7]:
            row['error_code'] = 'network_error'
        self.assertIn('Check failed', '\n'.join(s for s, _ in panel.lines(data, 60, NOW)))

    def test_only_confirmed_exhaustion_says_at_limit(self):
        data = fixture()
        for row in data['accounts'][1:7]:
            row['usage']['five_hour']['utilization'] = 100
        self.assertIn('At quota limit', '\n'.join(s for s, _ in panel.lines(data, 43, NOW)))

    def test_recent_readings_refresh_at_expiry_without_tight_polling(self):
        data = fixture()
        data['accounts'][0].update(status='recent', live=False, captured_at=(NOW - dt.timedelta(seconds=45)).isoformat())
        self.assertEqual(panel.refresh_delay(data, 60, NOW), 15)
        self.assertEqual(panel.refresh_delay(data, 60, NOW + dt.timedelta(seconds=14)), 5)
        data['accounts'][1].update(status='error', live=False, error_code='rate_limited', retry_after_seconds=900)
        self.assertEqual(panel.refresh_delay(data, 60, NOW), 900)

    def test_summary_stays_within_narrow_width(self):
        for width in (0, 1, 12, 26, 43):
            self.assertTrue(all(app.display_width(s) <= width for s, _ in panel.lines(fixture(), width, NOW)))


class SourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'data'
        self.store = app.Store(self.path, create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.store.write('accounts.json', {'accounts': app.default_accounts()})
        self.store.write('vault.json', live.encrypt_vault({'max-1': ENTRY}, KEY, SALT))

    def test_cached_key_reused_in_panel_without_extending_global_ttl(self):
        source = panel.Source(self.path)
        with patch.object(panel.vault_cache, 'get', return_value=KEY) as get, \
             patch.object(panel.vault_cache, 'put') as put, \
             patch.object(live, 'passphrase', side_effect=AssertionError('prompt')):
            self.assertEqual(source.load()[1]['max-1']['access_token'], ENTRY['access_token'])
            get.return_value = None
            self.assertEqual(source.load()[1]['max-1']['access_token'], ENTRY['access_token'])
            self.assertEqual(get.call_count, 1)
            put.assert_not_called()
        source.lock()
        self.assertIsNone(source.key)
        with self.assertRaises(live.LiveError):
            source.load()

    def test_updated_credentials_read_each_cycle_without_writes(self):
        source = panel.Source(self.path)
        source.key = KEY
        before = (self.path / 'vault.json').read_bytes()
        source.load()
        self.assertEqual((self.path / 'vault.json').read_bytes(), before)
        updated = {**ENTRY, 'access_token': 'FAKE-NEW-PANEL-TOKEN'}
        self.store.write('vault.json', live.encrypt_vault({'max-1': updated}, KEY, SALT), replace=True)
        self.assertEqual(source.load()[1]['max-1']['access_token'], updated['access_token'])

    def test_external_lock_and_rekey_release_session_key(self):
        source = panel.Source(self.path)
        source.key = KEY
        panel.lock_panels(self.store)
        self.assertFalse(source.check())
        self.assertIsNone(source.key)
        with patch.object(live, 'unlock_cached', return_value=({'max-1': ENTRY}, KEY, SALT)):
            source.load(prompt=True)
        self.assertTrue(source.check())
        self.store.write('vault.json', live.encrypt_vault({'max-1': ENTRY}, b'x' * 32, b'y' * 16), replace=True)
        with self.assertRaises(live.LiveError):
            source.load()
        self.assertIsNone(source.key)

    def test_missing_cache_never_prompts_automatically(self):
        with patch.object(panel.vault_cache, 'get', return_value=None), \
             patch.object(live, 'passphrase', side_effect=AssertionError('prompt')):
            with self.assertRaises(live.LiveError):
                panel.Source(self.path).load()

    def test_lock_marker_reserved_and_private(self):
        panel.lock_panels(self.store)
        self.assertEqual((self.path / panel.LOCK_FILE).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(app.UsageError):
            app.validate_accounts({'accounts': [{'id': 'panel-lock', 'name': 'test', 'plan': 'codex'}]})


class FetchTests(unittest.TestCase):
    def poll(self, fetch):
        limit = time.monotonic() + 3
        while time.monotonic() < limit:
            result = fetch.poll()
            if result is not None:
                return result
            time.sleep(.01)
        self.fail('worker did not finish')

    def test_report_transfer_and_no_remaining_child(self):
        data = fixture()
        data['accounts'][0]['credential_expires_at'] = NOW.timestamp()  # permitted report field
        fetch = panel.Fetch(lambda: data)
        pid = fetch.pid
        self.addCleanup(fetch.close)
        self.assertEqual(self.poll(fetch), data)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        self.assertNotIn(ENTRY['access_token'], fetch.buffer.decode())

    def test_timeout_and_cancellation_reap_worker(self):
        for timeout in (.02, 90):
            fetch = panel.Fetch(lambda: time.sleep(20), timeout=timeout)
            pid = fetch.pid
            self.addCleanup(fetch.close)
            if timeout == 90:
                fetch.close()
            else:
                self.assertIn('timed out', self.poll(fetch)['error'])
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)

    def test_exception_contents_never_reach_ui(self):
        def fail():
            raise ValueError('FAKE-DO-NOT-LOG-SECRET')
        fetch = panel.Fetch(fail)
        self.addCleanup(fetch.close)
        self.assertNotIn('FAKE', json.dumps(self.poll(fetch)))

    def test_collect_uses_existing_report_and_ranking_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data'
            with app.Store(path, create=True):
                pass
            data = fixture()
            with patch.object(app, 'concurrent_report', return_value=data) as report, \
                 patch.object(panel.credential_state, 'current', return_value={}), \
                 patch.object(panel.credential_state, 'annotate', side_effect=lambda d, *a: d), \
                 patch('subprocess.Popen', side_effect=AssertionError('native CLI')), \
                 patch('socket.socket', side_effect=AssertionError('extra network')):
                result = panel.collect(path, [], {}, 4, 'recommended', False)
            report.assert_called_once()
            self.assertEqual(len(result['accounts']), 9)


class EntryTests(unittest.TestCase):
    def test_panel_flags_rejected_before_access(self):
        for args in (['--panel', '--json'], ['--panel-interval', '60'], ['--panel', '--panel-interval', '1']):
            with patch.object(app, 'Store', side_effect=AssertionError('store')), \
                 contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.main(args), 2)

    def test_panel_opt_in_only(self):
        with patch.object(panel, 'run', return_value=0) as run:
            self.assertEqual(app.main(['--panel', '--panel-interval', '120']), 0)
            self.assertEqual(run.call_args.kwargs['interval'], 120)
        with patch('sys.stdin.isatty', return_value=False), self.assertRaises(app.UsageError):
            panel.run(Path('/does-not-exist'))

    def test_manifest_has_no_automatic_hooks_or_build(self):
        manifest = tomllib.loads((ROOT / 'plugins/herdr/herdr-plugin.toml').read_text())
        self.assertEqual(manifest['id'], plugin.PLUGIN)
        self.assertEqual(manifest['platforms'], ['linux'])
        self.assertFalse({'events', 'startup', 'build'} & manifest.keys())

    def test_plugin_uses_installed_command_and_explicit_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'config.json'
            cfg.write_text(json.dumps({'quotahop_path': '/fake/quotahop', 'data_dir': '/fake/vault', 'interval_seconds': 120}))
            with patch.dict(os.environ, {'HERDR_PLUGIN_CONFIG_DIR': tmp}), patch.object(plugin.os, 'access', return_value=True):
                self.assertEqual(plugin.config(), ['/fake/quotahop', '--panel', '--panel-interval', '120', '--data-dir', '/fake/vault'])
                cfg.write_text('{"password":"FAKE"}')
                with self.assertRaises(ValueError):
                    plugin.config()

    def test_plugin_open_targets_caller_and_preserves_focus(self):
        layout = {'layout': {'panes': [{'pane_id': 'w1:p1', 'rect': {'width': 172}}]}}
        opened = {'plugin_pane': {'pane': {'pane_id': 'w1:p3'}}}
        measured = {'layout': {'panes': [{'pane_id': 'w1:p3', 'rect': {'width': 86}}]}}
        with patch.object(plugin, 'config', return_value=['/fake/quotahop', '--panel']), \
             patch.dict(os.environ, {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'w1:p1'}), \
             patch.object(plugin, 'herdr', side_effect=[layout, opened, measured, {}]) as call:
            self.assertEqual(plugin.main(['open']), 0)
        self.assertIn('--no-focus', call.call_args_list[1].args)
        self.assertIn('w1:p1', call.call_args_list[1].args)
        self.assertIn('w1:p3', call.call_args_list[2].args)

    def test_plugin_expands_a_restored_narrow_split(self):
        layout = {'layout': {'panes': [{'pane_id': 'w1:p1', 'rect': {'width': 164}}]}}
        opened = {'plugin_pane': {'pane': {'pane_id': 'w1:p3'}}}
        measured = {'layout': {'panes': [{'pane_id': 'w1:p3', 'rect': {'width': 25}}]}}
        with patch.object(plugin, 'config', return_value=['/fake/quotahop', '--panel']), \
             patch.dict(os.environ, {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'w1:p1'}), \
             patch.object(plugin, 'herdr', side_effect=[layout, opened, measured, {}]) as call:
            self.assertEqual(plugin.main(['open']), 0)
        self.assertEqual(call.call_args.args[-3:], ('left', '--amount', '0.0488'))


class LifecycleTests(unittest.TestCase):
    def screen(self, keys):
        screen = MagicMock()
        screen.getmaxyx.return_value = (30, 44)
        screen.getch.side_effect = keys
        return screen

    def test_quit_cancels_an_inflight_check_and_drops_key(self):
        source = MagicMock()
        source.load.return_value = ([], {})
        with patch.object(panel.curses, 'curs_set'), patch.object(panel, 'Fetch') as fetch:
            fetch.return_value.poll.return_value = None
            self.assertEqual(panel._screen(self.screen([ord('q')]), source, 60, 4, 'recommended', False, False), 0)
            fetch.assert_called_once()
            fetch.return_value.close.assert_called_once()
        source.lock.assert_called_once()

    def test_pause_cancels_batch_and_prevents_checks_after_interval(self):
        source = MagicMock()
        source.load.return_value = ([], {})
        with patch.object(panel.curses, 'curs_set'), patch.object(panel, 'Fetch') as fetch, \
             patch.object(panel.time, 'monotonic', side_effect=[10, 200, 400]):
            fetch.return_value.poll.return_value = None
            panel._screen(self.screen([ord('p'), -1, ord('q')]), source, 60, 4, 'recommended', False, False)
            fetch.assert_called_once()
            fetch.return_value.close.assert_called_once()

    def test_success_waits_full_interval_before_next_batch(self):
        source = MagicMock()
        source.load.return_value = ([], {})
        with patch.object(panel.curses, 'curs_set'), patch.object(panel, 'Fetch') as fetch, \
             patch.object(panel.time, 'monotonic', side_effect=[10, 20, 21, 79, 80]):
            fetch.return_value.poll.return_value = fixture()
            panel._screen(self.screen([-1, -1, -1, -1, ord('q')]), source, 60, 4, 'recommended', False, False)
            self.assertEqual(fetch.call_count, 2)

    def test_external_lock_cancels_check_and_clears_report(self):
        source = MagicMock()
        source.load.return_value = ([], {})
        source.check.side_effect = [True, False]
        screen = self.screen([-1, ord('q')])
        with patch.object(panel.curses, 'curs_set'), patch.object(panel, 'Fetch') as fetch, \
             patch.object(panel.time, 'monotonic', side_effect=[10, 12]):
            panel._screen(screen, source, 60, 4, 'recommended', False, False)
            fetch.assert_called_once()
            fetch.return_value.close.assert_called_once()
        self.assertTrue(any('Locked' in str(call) for call in screen.addstr.call_args_list))

    def test_pause_resume_while_locked_does_not_crash_or_fetch(self):
        source = MagicMock()
        source.load.side_effect = live.LiveError('vault_locked', 'Fake locked vault')
        with patch.object(panel.curses, 'curs_set'), patch.object(panel, 'Fetch') as fetch:
            self.assertEqual(panel._screen(self.screen([ord('p'), ord('p'), ord('q')]), source,
                                          60, 4, 'recommended', False, False), 0)
            fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
