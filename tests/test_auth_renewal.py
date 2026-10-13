import base64
from contextlib import contextmanager
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch, mock_open

import auth_renewal as renewal
import claude_usage as app
import live_usage as live
import owned_codex as native

KEY = b'k' * 32
SALT = b's' * 16
EMAIL = 'person@example.test'


def fake_pair(serial='old', expiry=None, routing='fake-workspace'):
    expiry = expiry or time.time() + 7200
    claims = {'exp': expiry, 'https://api.openai.com/profile': {'email': EMAIL}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    access = 'eyJ0ZXN0Ijp0cnVlfQ.' + body + '.' + serial
    auth = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None, 'tokens': {'access_token': access,
            'refresh_token': 'FAKE-REFRESH-' + serial, 'id_token': 'FAKE-ID-TOKEN-' + serial,
            'account_id': routing}, 'last_refresh': '2026-10-13T00:00:00Z'}
    entry = {'provider': 'codex', 'access_token': access, 'expires_at': expiry,
             'account_id': routing, 'identity': {'email': EMAIL}}
    return auth, entry


def record(auth, entry, due=False):
    return {'origin': 'isolated-codex-device-v1', 'auth': auth, 'entry': entry,
            'state': 'ready', 'next_at': time.time() - 1 if due else entry['expires_at'] - 300,
            'published': renewal.digest(entry), 'updated_at': time.time()}


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private'
        self.store = app.Store(self.path, create=True)
        self.addCleanup(self.store.__exit__)
        self.accounts = app.default_accounts() + [{'id': 'codex-1', 'name': EMAIL, 'plan': 'codex'},
                                                 {'id': 'muse-1', 'name': 'Muse', 'plan': 'muse_code'}]
        self.auth, self.entry = fake_pair()
        self.store.write('accounts.json', {'accounts': self.accounts})
        self.store.write('vault.json', live.encrypt_vault({'codex-1': self.entry}, KEY, SALT))
        patcher = patch('live_usage.vault_cache.get', return_value=KEY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, value=None):
        self.store.write(renewal.REGISTRY, renewal.seal(value or {'codex-1': record(self.auth, self.entry, True)}, KEY), replace=True)

    def test_enrollment_never_reads_or_renews_shared_credentials(self):
        fresh, entry = fake_pair('enrolled')
        with patch.object(native, 'perform', return_value=(fresh, entry)) as call, \
             patch.object(live, 'default_source', side_effect=AssertionError('shared credential read')):
            result = renewal.enroll(self.store, self.accounts, EMAIL)
        call.assert_called_once_with()
        self.assertEqual(result['account_id'], 'codex-1')
        self.assertEqual(renewal.records(self.store, KEY)['codex-1']['auth'], fresh)
        self.assertEqual(live.load_vault(self.store)['codex-1'], entry)
        self.assertNotIn('refresh_token', json.dumps(live.load_vault(self.store)))
        for path in self.path.iterdir():
            self.assertNotIn('FAKE-REFRESH', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(renewal.status(self.store, self.accounts)['accounts'][-2]['state'], 'ready')

    def test_enrollment_rejects_different_workspace_or_email(self):
        for fresh, entry in [fake_pair('wrong', routing='another-workspace'), fake_pair('wrong-email')]:
            if entry['account_id'] == 'fake-workspace':
                entry['identity'] = {'email': 'another@example.test'}
            with patch.object(native, 'perform', return_value=(fresh, entry)), self.assertRaises(live.LiveError):
                renewal.enroll(self.store, self.accounts, EMAIL)
            self.assertEqual(live.load_vault(self.store)['codex-1'], self.entry)
            self.assertEqual(renewal.records(self.store, KEY), {})

    def test_provider_dispatch_does_not_use_codex_for_claude_or_muse(self):
        with patch.object(native, 'perform') as codex, \
             patch('owned_claude.perform', side_effect=live.LiveError('test', 'fake')) as claude, \
             patch('owned_muse.perform', side_effect=live.LiveError('test', 'fake')) as muse:
            for ident in ('max-1', 'team-1', 'muse-1'):
                with self.assertRaises(live.LiveError):
                    renewal.enroll(self.store, self.accounts, ident)
            codex.assert_not_called()
            self.assertEqual(claude.call_count, 2)
            muse.assert_called_once_with()

    def test_one_renewal_rotates_before_access_projection_then_waits_until_due(self):
        self.save()
        fresh, entry = fake_pair('rotated')
        def perform(auth):
            self.assertEqual(auth, self.auth)
            self.assertEqual(renewal.records(self.store, KEY)['codex-1']['state'], 'in_flight')
            return fresh, entry
        original_write = self.store.write
        def write(name, value, **kwargs):
            if name == 'vault.json':
                self.assertEqual(renewal.records(self.store, KEY)['codex-1']['auth'], fresh)
            original_write(name, value, **kwargs)
        with patch.object(native, 'perform', side_effect=perform) as call, patch.object(self.store, 'write', side_effect=write):
            renewal.cycle(self.store, KEY)
            renewal.cycle(self.store, KEY)
        call.assert_called_once()
        saved = renewal.records(self.store, KEY)['codex-1']
        self.assertEqual(saved['auth'], fresh)
        self.assertEqual(saved['state'], 'ready')
        self.assertEqual(saved['next_at'], entry['expires_at'] - 300)
        self.assertEqual(live.load_vault(self.store)['codex-1'], entry)

    def test_no_due_grant_does_not_rewrite_vault_or_call_provider(self):
        self.save({'codex-1': record(self.auth, self.entry)})
        before = self.store.read('vault.json')
        registry = self.store.read(renewal.REGISTRY)
        with patch.object(native, 'perform') as call:
            renewal.cycle(self.store, KEY)
        call.assert_not_called()
        self.assertEqual(self.store.read('vault.json'), before)
        self.assertEqual(self.store.read(renewal.REGISTRY), registry)

    def test_uncertain_outcomes_suspend_without_replaying_refresh_token(self):
        for error in (TimeoutError('FAKE-REFRESH'), live.LiveError('invalid_grant', 'FAKE-REFRESH')):
            self.save()
            with patch.object(native, 'perform', side_effect=error) as call:
                renewal.cycle(self.store, KEY)
                renewal.cycle(self.store, KEY)
            call.assert_called_once()
            self.assertEqual(renewal.records(self.store, KEY)['codex-1']['state'], 'suspended')
            self.assertNotIn('FAKE-REFRESH', json.dumps(renewal.status(self.store, self.accounts)))
            self.assertEqual(live.load_vault(self.store)['codex-1'], self.entry)

    def test_crash_marker_never_replays_token(self):
        item = record(self.auth, self.entry, True)
        item['state'] = 'in_flight'
        self.save({'codex-1': item})
        with patch.object(native, 'perform') as call:
            renewal.cycle(self.store, KEY)
        call.assert_not_called()
        self.assertEqual(renewal.records(self.store, KEY)['codex-1']['state'], 'suspended')

    def test_crash_between_grant_commit_and_publication_recovers_without_refresh(self):
        fresh, entry = fake_pair('rotated')
        item = record(fresh, entry)
        item['published'] = renewal.digest(self.entry)
        self.save({'codex-1': item})
        with patch.object(native, 'perform') as call:
            renewal.cycle(self.store, KEY)
        call.assert_not_called()
        self.assertEqual(live.load_vault(self.store)['codex-1'], entry)

    def test_external_capture_change_suspends_without_overwrite(self):
        self.save()
        _, different = fake_pair('manual-capture')
        self.store.write('vault.json', live.encrypt_vault({'codex-1': different}, KEY, SALT), replace=True)
        with patch.object(native, 'perform') as call:
            renewal.cycle(self.store, KEY)
        call.assert_not_called()
        self.assertEqual(live.load_vault(self.store)['codex-1'], different)
        self.assertEqual(renewal.records(self.store, KEY)['codex-1']['state'], 'suspended')

    def test_removed_account_is_never_resurrected(self):
        self.save()
        with patch.object(native, 'perform') as call:
            live.remove_account(self.store, 'codex-1')
            renewal.cycle(self.store, KEY)
        call.assert_not_called()
        self.assertEqual(renewal.records(self.store, KEY), {})
        self.assertNotIn('codex-1', live.load_vault(self.store))

    def test_forget_removes_grant_without_logout_or_access_token_loss(self):
        self.save()
        with patch.object(native, 'perform') as call:
            renewal.forget(self.store, self.accounts, EMAIL)
        call.assert_not_called()
        self.assertEqual(renewal.records(self.store, KEY), {})
        self.assertEqual(live.load_vault(self.store)['codex-1'], self.entry)

    def test_lock_serializes_enrollment_and_worker(self):
        with renewal.worker_lock(self.store):
            self.assertTrue(renewal.running(self.store))
            with self.assertRaises(live.LiveError):
                renewal.start(self.store, self.path)
            with self.assertRaises(live.LiveError):
                renewal.enroll(self.store, self.accounts, EMAIL)
        self.assertFalse(renewal.running(self.store))

    def test_stop_request_prevents_new_refresh(self):
        self.save()
        self.store.write(renewal.STOP, {'requested_at': time.time()})
        with patch.object(native, 'perform') as call:
            renewal.cycle(self.store, KEY)
        call.assert_not_called()

    def test_real_worker_start_status_stop_with_future_fake_grant_no_network(self):
        self.save({'codex-1': record(self.auth, self.entry)})
        before = dict(os.environ)
        try:
            renewal.start(self.store, self.path)
            self.assertTrue(renewal.running(self.store))
            with patch.object(live, 'passphrase', side_effect=AssertionError('status asks password')):
                self.assertTrue(renewal.status(self.store, self.accounts)['running'])
                renewal.stop(self.store, timeout=10)
            self.assertFalse(renewal.running(self.store))
        finally:
            renewal.stop(self.store, timeout=10)
        self.assertEqual(os.environ, before)
        self.assertEqual(renewal.records(self.store, KEY)['codex-1']['auth'], self.auth)

    def test_enrollment_starts_real_worker_once_and_reuses_unlocked_key(self):
        fresh, entry = fake_pair('independent')
        with patch.object(native, 'perform', return_value=(fresh, entry)), \
             patch.object(renewal, 'unlock', wraps=renewal.unlock) as unlock:
            try:
                result = renewal.enroll_and_start(self.store, self.path, self.accounts, EMAIL)
                self.assertTrue(result['running'])
                self.assertTrue(renewal.running(self.store))
                self.assertEqual(unlock.call_count, 1)
                self.assertNotIn('FAKE-', json.dumps(result))
            finally:
                renewal.stop(self.store, timeout=10)
        self.assertEqual(live.load_vault(self.store)['codex-1'], entry)

    def test_enroll_next_account_pauses_and_restarts_existing_worker(self):
        self.save({'codex-1': record(self.auth, self.entry)})
        accounts = self.accounts + [{'id': 'codex-2', 'name': 'Second', 'plan': 'codex'}]
        self.store.write('accounts.json', {'accounts': accounts}, replace=True)
        fresh, entry = fake_pair('second', routing='second-workspace')
        def perform():
            # Enrollment itself owns the lifecycle lock. The old worker must
            # have stopped and left its final stopped status before sign-in.
            self.assertFalse(self.store.read(renewal.STATUS)['running'])
            return fresh, entry
        try:
            renewal.start(self.store, self.path)
            with patch.object(native, 'perform', side_effect=perform):
                result = renewal.enroll_and_start(self.store, self.path, accounts, 'codex-2')
            self.assertTrue(result['running'])
            self.assertTrue(renewal.running(self.store))
            self.assertEqual(set(renewal.records(self.store, KEY)), {'codex-1', 'codex-2'})
        finally:
            renewal.stop(self.store, timeout=10)

    def test_failed_or_cancelled_enrollment_restores_only_previously_running_worker(self):
        for was_running in (False, True):
            for error in (live.LiveError('failed', 'Sign-in failed.'), KeyboardInterrupt()):
                with patch.object(renewal, 'running', return_value=was_running), \
                     patch.object(renewal, 'stop') as stop, \
                     patch.object(renewal, 'start') as start, \
                     patch.object(renewal, 'enroll', side_effect=error):
                    with self.assertRaises(type(error)):
                        renewal.enroll_and_start(self.store, self.path, self.accounts, EMAIL)
                self.assertEqual(stop.call_count, int(was_running))
                self.assertEqual(start.call_count, int(was_running))

    def test_cli_startup_failure_keeps_enrollment_and_returns_actionable_error(self):
        fresh, entry = fake_pair('saved')
        with patch.object(native, 'perform', return_value=(fresh, entry)), \
             patch.object(renewal, 'start', side_effect=OSError('FAKE-SECRET')), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            code = app.main(['--data-dir', str(self.path), '--auth-enroll', EMAIL, '--json'])
        self.assertEqual(code, 1)
        result = json.loads(out.getvalue())
        self.assertFalse(result['running'])
        self.assertIn('--background start', result['message'])
        self.assertNotIn('FAKE-SECRET', out.getvalue())
        self.assertEqual(live.load_vault(self.store)['codex-1'], entry)

    def test_ambiguous_email_rejected_before_stopping_worker(self):
        accounts = self.accounts + [{'id': 'codex-2', 'name': EMAIL, 'plan': 'codex'}]
        with patch.object(renewal, 'stop') as stop, patch.object(renewal, 'start') as start, \
             patch.object(native, 'perform') as perform, self.assertRaises(app.UsageError):
            renewal.enroll_and_start(self.store, self.path, accounts, EMAIL)
        stop.assert_not_called()
        start.assert_not_called()
        perform.assert_not_called()

    def test_plan_is_inferred_from_unique_saved_account(self):
        entries = {'codex-1': self.entry}
        self.assertEqual(renewal.choose(self.accounts, entries, EMAIL)['plan'], 'codex')
        self.assertEqual(renewal.choose(self.accounts, entries, 'max-1')['plan'], 'max_20x')
        self.assertEqual(renewal.choose(self.accounts, entries, 'team-1')['plan'], 'team_premium')

    def test_seven_independent_grants_renew_without_crossing_identities(self):
        accounts, entries, grants, replies = [], {}, {}, {}
        for i in range(7):
            ident = f"codex-{i}"
            auth, entry = fake_pair(str(i), routing=f"workspace-{i}")
            accounts.append({'id': ident, 'name': f"Codex {i}", 'plan': 'codex'})
            entries[ident] = entry
            grants[ident] = record(auth, entry, True)
            replies[auth['tokens']['refresh_token']] = fake_pair(f"renewed-{i}", routing=f"workspace-{i}")
        self.store.write('accounts.json', {'accounts': accounts}, replace=True)
        self.store.write('vault.json', live.encrypt_vault(entries, KEY, SALT), replace=True)
        self.save(grants)
        with patch.object(native, 'perform', side_effect=lambda auth: replies[auth['tokens']['refresh_token']]) as call:
            renewal.cycle(self.store, KEY)
            renewal.cycle(self.store, KEY)
        self.assertEqual(call.call_count, 7)
        for i in range(7):
            self.assertEqual(live.load_vault(self.store)[f"codex-{i}"]['account_id'], f"workspace-{i}")

    def test_lock_command_stops_worker_before_forgetting_key(self):
        events = []
        with patch.object(renewal, 'stop', side_effect=lambda store: events.append('stop')), \
             patch.object(live, 'lock_vault', side_effect=lambda store: events.append('lock')), \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(app.main(['--data-dir', str(self.path), '--lock']), 0)
        self.assertEqual(events, ['stop', 'lock'])

    def test_cli_status_does_not_unlock_or_start_a_worker(self):
        with patch.object(live, 'unlock_cached', side_effect=AssertionError('unlock')), \
             patch.object(renewal, 'start', side_effect=AssertionError('spawn')), \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            result = app.main(['--data-dir', str(self.path), '--background', 'status', '--json'])
        self.assertEqual(result, 0)
        self.assertFalse(json.loads(out.getvalue())['running'])

    def test_reserved_ids_and_capture_cannot_replace_owned_grant(self):
        for ident in renewal.RESERVED:
            with self.assertRaises(app.UsageError):
                app.validate_accounts({'accounts': [{'id': ident, 'plan': 'codex', 'name': 'Test'}]})
        self.save()
        with patch.object(live, 'read_access_token', return_value=self.entry), self.assertRaises(live.LiveError) as caught:
            live.capture(self.store, 'codex-1', '/unused', 'codex')
        self.assertEqual(caught.exception.code, 'managed_account')


class NativeBoundaryTests(unittest.TestCase):
    def test_rpc_allowlist_prohibits_inference_and_logout(self):
        process = MagicMock()
        with patch.object(native.selectors, 'DefaultSelector'):
            rpc = native.Rpc(process)
        for method in ('turn/start', 'thread/start', 'account/logout', 'command/exec', 'account/chatgptAuthTokens/refresh', 'responses/create'):
            with self.assertRaises(live.LiveError):
                rpc.send(method, {})
        for method, params in [('account/login/start', {'type': 'chatgptAuthTokens'}),
                               ('account/read', {'refreshToken': 'yes'})]:
            with self.assertRaises(live.LiveError):
                rpc.send(method, params)
        process.stdin.write.assert_not_called()
        self.assertEqual(native.METHODS, {'initialize', 'initialized', 'account/login/start', 'account/read'})

    def test_device_login_uses_first_party_flow_and_only_displays_code_on_tty(self):
        with patch.object(native.selectors, 'DefaultSelector'):
            rpc = native.Rpc(MagicMock())
        rpc.notifications.append({'method': 'account/login/completed',
                                  'params': {'loginId': 'fake-login-id', 'success': True}})
        result = {'type': 'chatgptDeviceCode', 'loginId': 'fake-login-id',
                  'verificationUrl': 'https://auth.openai.com/codex/device', 'userCode': 'FAKE-1234'}
        tty = mock_open()
        with patch.object(rpc, 'call', return_value=result) as call, \
             patch.object(native.os, 'open', return_value=99), patch.object(native.os, 'close'), \
             patch('builtins.open', tty), patch('sys.stdout', new_callable=io.StringIO) as out:
            rpc.login()
        call.assert_called_once_with('account/login/start', {'type': 'chatgptDeviceCode'})
        tty.assert_called_once_with('/dev/tty', 'w')
        self.assertEqual(out.getvalue(), '')
        self.assertIn('FAKE-1234', ''.join(c.args[0] for c in tty().write.call_args_list))

    def test_noninteractive_enrollment_fails_before_starting_auth_flow(self):
        with patch.object(native.selectors, 'DefaultSelector'):
            rpc = native.Rpc(MagicMock())
        with patch.object(native.os, 'open', side_effect=OSError()), patch.object(rpc, 'call') as call:
            with self.assertRaises(live.LiveError) as caught:
                rpc.login()
        self.assertEqual(caught.exception.code, 'tty_required')
        call.assert_not_called()

    def test_auth_reply_cannot_redirect_device_sign_in(self):
        with patch.object(native.selectors, 'DefaultSelector'):
            rpc = native.Rpc(MagicMock())
        with patch.object(native.os, 'open', return_value=99), patch.object(native.os, 'close'), \
             patch.object(rpc, 'call', return_value={'verificationUrl': 'https://evil.invalid/FAKE-REFRESH'}), \
             self.assertRaises(live.LiveError) as caught:
            rpc.login()
        self.assertNotIn('FAKE-REFRESH', str(caught.exception))

    def test_rpc_errors_never_echo_provider_secrets(self):
        with patch.object(native.selectors, 'DefaultSelector'):
            rpc = native.Rpc(MagicMock())
        with patch.object(rpc, 'receive', return_value={'id': 1, 'error': {'message': 'FAKE-REFRESH'}}):
            with self.assertRaises(live.LiveError) as caught:
                rpc.call('account/read', {'refreshToken': True})
        self.assertNotIn('FAKE-REFRESH', str(caught.exception))

    def test_missing_sandbox_is_fail_closed(self):
        with patch.object(native.shutil, 'which', return_value=None), self.assertRaises(live.LiveError):
            native.command(Path('/tmp/owned-context'))

    def test_sandbox_has_no_existing_home_or_keyring_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'native'
            executable.write_bytes(b'\x7fELFfake')
            with patch.object(native.shutil, 'which', side_effect=lambda name: str(executable) if name == 'codex' else '/usr/bin/bwrap'):
                cmd = native.command(Path(directory) / 'context')
        self.assertIn('--clearenv', cmd)
        self.assertIn('--unshare-pid', cmd)
        self.assertIn('--die-with-parent', cmd)
        self.assertEqual(cmd[-2:], ['/opt/codex', 'app-server'])
        mounts = [cmd[i+1] for i, word in enumerate(cmd) if word in ('--bind', '--ro-bind')]
        self.assertNotIn(str(Path.home()), mounts)
        self.assertNotIn('/run', mounts)
        self.assertNotIn('/', mounts)
        self.assertNotIn('DBUS_SESSION_BUS_ADDRESS', cmd)
        self.assertNotIn('OPENAI_API_KEY', cmd)

    def test_perform_refresh_uses_only_account_read_and_copies_access_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'auth.json'
            auth, entry = fake_pair('new')
            source.write_text(json.dumps(auth)); source.chmod(0o600)
            rpc = MagicMock()
            rpc.call.return_value = {'account': {'type': 'chatgpt', 'email': EMAIL}}
            @contextmanager
            def session(_):
                yield rpc, source
            with patch.object(native, 'session', side_effect=session):
                result, projected = native.perform(fake_pair()[0])
        rpc.login.assert_not_called()
        rpc.call.assert_called_once_with('account/read', {'refreshToken': True})
        self.assertEqual(result, auth)
        self.assertEqual(projected, entry)
        self.assertNotIn('refresh_token', projected)

    @unittest.skipUnless(shutil.which('bwrap') and shutil.which('codex'), 'native CLI/bubblewrap unavailable')
    def test_actual_filesystem_sandbox_hides_existing_auth_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hidden = root / 'shared-auth-canary'
            hidden.write_text('unchanged')
            context = root / 'context'
            context.mkdir(mode=0o700)
            (context / 'home').mkdir(mode=0o700)
            (context / 'codex').mkdir(mode=0o700)
            cmd = native.command(context)
            script = ("import pathlib,os,json; p=pathlib.Path(" + repr(str(hidden)) + "); "
                      "print(json.dumps({'visible':p.exists(),'home':os.environ['HOME'],"
                      "'codex':os.environ['CODEX_HOME'],'dbus':'DBUS_SESSION_BUS_ADDRESS' in os.environ}))")
            cmd[-2:] = ['/usr/bin/python3', '-c', script]
            result = subprocess.run(cmd, capture_output=True, timeout=10, env={'PATH': '/usr/bin:/bin'})
            self.assertEqual(result.returncode, 0, 'Required bubblewrap filesystem isolation failed')
            self.assertEqual(json.loads(result.stdout), {'visible': False, 'home': '/owned/home', 'codex': '/owned/codex', 'dbus': False})
            self.assertEqual(hidden.read_text(), 'unchanged')

    def test_session_always_reaps_process_and_deletes_private_context(self):
        proc = MagicMock()
        proc.poll.return_value = None
        seen = []
        def cmd(context):
            seen.append(context)
            self.assertEqual(context.stat().st_mode & 0o777, 0o700)
            self.assertEqual((context / 'codex/auth.json').stat().st_mode & 0o777, 0o600)
            return ['fake-native']
        with patch.object(native, 'command', side_effect=cmd), patch.object(native.subprocess, 'Popen', return_value=proc), \
             patch.object(native, 'Rpc'):
            with self.assertRaises(RuntimeError):
                with native.session(fake_pair()[0]):
                    raise RuntimeError('simulate failure')
        proc.terminate.assert_called_once()
        proc.wait.assert_called()
        self.assertFalse(seen[0].exists())


if __name__ == '__main__':
    unittest.main()
