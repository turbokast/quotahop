"""Password rotation uses only fake credentials, temporary files and mocked workers."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import auth_renewal as renewal
import claude_usage as app
import live_usage as live
import vault_password as password
from test_owned_providers import claude_pair, record

OLD = b'old-fake-password-only'
NEW = b'new-fake-password-only'
SALT = b's' * 16


class PasswordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_key = live.vault_key(OLD, SALT)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data'
        self.store = app.Store(self.path, create=True, maintenance=True)
        self.addCleanup(self.store.__exit__)
        self.accounts = app.default_accounts()
        self.store.write('accounts.json', {'accounts': self.accounts})
        self.entries, self.grants = {}, {}
        for i, account in enumerate(self.accounts):
            auth, entry = claude_pair(str(i), plan=account['plan'])
            self.entries[account['id']] = entry
            self.grants[account['id']] = record('claude', auth, entry)
        self.envelope = live.encrypt_vault(self.entries, self.old_key, SALT)
        self.backup = {'paths': ['/fake/claude/credentials', '/fake/metadata'],
                       'claudeAiOauth': {'accessToken': 'FAKE-UNDO-ACCESS', 'refreshToken': 'FAKE-UNDO-REFRESH'}}
        self.store.write('vault.json', self.envelope)
        self.store.write('renewal.json', renewal.seal(self.grants, self.old_key))
        self.store.write('switch-backup.json', live.encrypt_vault(self.backup, self.old_key, SALT))
        self.cache_get = self.mock('vault_cache.get', return_value=None)
        self.cache_put = self.mock('vault_cache.put', return_value=True)
        self.cache_clear = self.mock('vault_cache.clear', return_value=True)
        self.prompt = self.mock('live_usage.passphrase', side_effect=[OLD, NEW])
        self.running = self.mock('auth_renewal.running', return_value=False)
        self.start = self.mock('auth_renewal.start')
        self.stop = self.mock('auth_renewal.stop')
        for target in ('socket.socket', 'live_usage.read_access_token', 'auth_renewal.perform', 'subprocess.Popen'):
            self.mock(target, side_effect=AssertionError('Unexpected provider or process access: ' + target))

    def mock(self, target, **kwargs):
        p = patch(target, **kwargs)
        obj = p.start()
        self.addCleanup(p.stop)
        return obj

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.path.iterdir() if p.name != renewal.LOCK}

    def verify_new(self):
        envelope = self.store.read('vault.json')
        entries, key, salt = live.unlock_vault(envelope, NEW)
        self.assertEqual(entries, self.entries)
        self.assertNotEqual(salt, SALT)
        self.assertEqual(renewal.records(self.store, key), self.grants)
        self.assertEqual(password.backup_value(self.store.read('switch-backup.json'), key), self.backup)
        with self.assertRaises(live.LiveError):
            live.unlock_vault(envelope, OLD)
        self.assertFalse((self.path / password.JOURNAL).exists())
        for p in self.path.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b'FAKE-', p.read_bytes())
            self.assertNotIn(OLD, p.read_bytes())
            self.assertNotIn(NEW, p.read_bytes())
        return key

    def test_changes_all_seven_accounts_grants_and_undo_without_provider_access(self):
        config = (self.path / 'accounts.json').read_bytes()
        result = password.change(self.store, self.path)
        self.assertTrue(result['changed'])
        self.assertEqual(config, (self.path / 'accounts.json').read_bytes())
        key = self.verify_new()
        self.cache_clear.assert_called_once_with(self.store.fd, self.envelope)
        self.assertEqual(self.cache_put.call_args.args[2], key)
        self.stop.assert_not_called()
        self.start.assert_not_called()

    def test_cached_unlock_needs_only_new_password(self):
        self.cache_get.return_value = self.old_key
        self.prompt.side_effect = [NEW]
        password.change(self.store, self.path)
        self.prompt.assert_called_once_with(confirm=True)
        self.verify_new()

    def test_wrong_old_password_changes_nothing_and_keeps_worker_running(self):
        self.running.return_value = True
        self.prompt.side_effect = [b'wrong-fake-password']
        before = self.snapshot()
        with self.assertRaises(live.LiveError):
            password.change(self.store, self.path)
        self.assertEqual(before, self.snapshot())
        self.stop.assert_not_called()

    def test_cancelled_or_invalid_new_password_changes_nothing(self):
        for failure in (KeyboardInterrupt(), live.LiveError('vault_locked', 'Passphrases must match.')):
            self.prompt.side_effect = [OLD, failure]
            before = self.snapshot()
            with self.assertRaises(type(failure)):
                password.change(self.store, self.path)
            self.assertEqual(before, self.snapshot())
        self.stop.assert_not_called()

    def test_worker_restarts_with_new_key_and_latest_rotated_grant(self):
        self.running.return_value = True
        def stop(store):
            auth, entry = claude_pair('rotated-during-prompt')
            self.entries['max-1'] = entry
            self.grants['max-1'] = record('claude', auth, entry)
            store.write('vault.json', live.encrypt_vault(self.entries, self.old_key, SALT), replace=True)
            store.write('renewal.json', renewal.seal(self.grants, self.old_key), replace=True)
        self.stop.side_effect = stop
        result = password.change(self.store, self.path)
        key = self.verify_new()
        self.assertTrue(result['running'])
        self.start.assert_called_once_with(self.store, self.path, key=key)

    def test_corrupt_undo_aborts_before_commit_and_restores_worker(self):
        self.running.return_value = True
        self.store.write('switch-backup.json', {'version': 1}, replace=True)
        before = self.snapshot()
        with self.assertRaises(live.LiveError):
            password.change(self.store, self.path)
        self.assertEqual(before, self.snapshot())
        self.start.assert_called_once_with(self.store, self.path, key=self.old_key)

    def test_each_partial_write_can_recover_with_new_password_only(self):
        original = self.store.write
        for fail_name in password.FILES:
            with self.subTest(fail_name=fail_name):
                # Each iteration starts from the same original key and files.
                original('vault.json', self.envelope, replace=True)
                original('renewal.json', renewal.seal(self.grants, self.old_key), replace=True)
                original('switch-backup.json', live.encrypt_vault(self.backup, self.old_key, SALT), replace=True)
                self.prompt.side_effect = [OLD, NEW]
                def failing(name, value, replace=False):
                    if name == fail_name:
                        raise OSError('fake disk failure')
                    return original(name, value, replace)
                with patch.object(self.store, 'write', side_effect=failing), self.assertRaises(live.LiveError) as caught:
                    password.change(self.store, self.path)
                self.assertEqual(caught.exception.code, 'rekey_pending')
                with app.Store(self.path) as normal:
                    for operation in (lambda: normal.read('vault.json'), lambda: normal.write('accounts.json', {}), lambda: normal.delete('renewal.json')):
                        with self.assertRaises(app.UsageError):
                            operation()
                snapshot = self.snapshot()
                self.prompt.side_effect = [OLD]
                with self.assertRaises(live.LiveError):
                    password.change(self.store, self.path)
                self.assertEqual(snapshot, self.snapshot())
                self.prompt.side_effect = [NEW]
                password.change(self.store, self.path)
                self.verify_new()

    def test_failed_journal_write_preserves_old_data_and_restarts_worker(self):
        self.running.return_value = True
        before = self.snapshot()
        with patch.object(self.store, 'write', side_effect=OSError('fake disk full')), self.assertRaises(OSError):
            password.change(self.store, self.path)
        self.assertEqual(before, self.snapshot())
        self.start.assert_called_once_with(self.store, self.path, key=self.old_key)

    def test_restart_failure_reports_password_changed(self):
        self.running.return_value = True
        self.start.side_effect = RuntimeError('FAKE-SECRET-DO-NOT-PRINT')
        result = password.change(self.store, self.path)
        self.assertTrue(result['changed'])
        self.assertTrue(result['background_error'])
        self.assertIn('new password', result['message'])
        self.assertNotIn('FAKE-SECRET', json.dumps(result))
        self.verify_new()

    def test_failure_after_final_unlink_reports_new_password_and_restarts_correctly(self):
        self.running.return_value = True
        original = self.store.delete
        def fail_after_unlink(name):
            original(name)
            if name == password.JOURNAL:
                raise OSError('fake directory fsync failure')
        with patch.object(self.store, 'delete', side_effect=fail_after_unlink), self.assertRaises(live.LiveError) as caught:
            password.change(self.store, self.path)
        self.assertEqual(caught.exception.code, 'rekey_finish')
        key = self.verify_new()
        self.start.assert_called_once_with(self.store, self.path, key=key)

    def test_interrupted_commit_restarts_worker_only_after_successful_recovery(self):
        self.running.return_value = True
        original = self.store.write
        def fail(name, value, replace=False):
            if name == 'vault.json':
                raise OSError('fake write failure')
            return original(name, value, replace)
        with patch.object(self.store, 'write', side_effect=fail), self.assertRaises(live.LiveError):
            password.change(self.store, self.path)
        self.start.assert_not_called()
        self.prompt.side_effect = [NEW]
        result = password.change(self.store, self.path)
        self.assertTrue(result['running'])
        self.start.assert_called_once_with(self.store, self.path, key=self.verify_new())

    def test_simple_vault_without_optional_files(self):
        self.store.delete('renewal.json')
        self.store.delete('switch-backup.json')
        password.change(self.store, self.path)
        self.assertEqual(live.unlock_vault(self.store.read('vault.json'), NEW)[0], self.entries)
        self.assertFalse((self.path / 'renewal.json').exists())

    def test_recovery_rejects_arbitrary_targets(self):
        self.store.write(password.JOURNAL, {'version': 1, 'restart': False, 'files': {'vault.json': self.envelope, 'accounts.json': {}}})
        before = self.snapshot()
        with self.assertRaises(live.LiveError):
            password.change(self.store, self.path)
        self.assertEqual(before, self.snapshot())

    def test_cli_alias_and_json_redaction(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = app.main(['--data-dir', str(self.path), '--reset-password', '--json'])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(out.getvalue())['changed'])
        self.assertNotIn('FAKE-', out.getvalue())
        self.verify_new()

    def test_reserved_recovery_filename_cannot_be_an_account(self):
        with self.assertRaises(app.UsageError):
            app.validate_accounts({'accounts': [{'id':'vault-rekey','name':'Fake','plan':'codex'}]})

    def test_missing_vault_does_not_prompt_or_stop_worker(self):
        self.store.delete('vault.json')
        with self.assertRaises(live.LiveError):
            password.change(self.store, self.path)
        self.prompt.assert_not_called()
        self.stop.assert_not_called()
