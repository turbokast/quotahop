"""Fake-only provider enrollment/rotation and report isolation tests."""
from contextlib import contextmanager
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import auth_renewal as renewal
import claude_usage as app
import credential_state
import isolated_auth
import live_usage as live
import owned_claude as claude
import owned_muse as muse
import saved_refresh

KEY, SALT = b'k' * 32, b's' * 16
ACCOUNT = '11111111-1111-1111-1111-111111111111'
ORG = '22222222-2222-2222-2222-222222222222'
OTHER_ORG = '33333333-3333-3333-3333-333333333333'
EMAIL = 'person@example.test'


def claude_pair(serial='old', org=ORG, plan='max_20x'):
    expiry = time.time() + 7200
    auth = {'claudeAiOauth': {'accessToken': 'FAKE-CLAUDE-ACCESS-' + serial,
        'refreshToken': 'FAKE-CLAUDE-REFRESH-' + serial, 'expiresAt': expiry * 1000,
        'scopes': ['user:profile', 'user:inference'], 'subscriptionType': 'max' if plan == 'max_20x' else 'team',
        'rateLimitTier': 'default_claude_max_20x'}}
    entry = {'access_token': auth['claudeAiOauth']['accessToken'], 'expires_at': expiry,
             'scopes': auth['claudeAiOauth']['scopes'], 'identity': {'email': EMAIL},
             'claude_identity': {'account_uuid': ACCOUNT, 'organization_uuid': org}}
    return auth, entry


def muse_pair():
    auth = {'schema_version': 1, 'providers': {'meta': {'access_token': 'dca:FAKE-DEVICE-ACCESS',
        'refresh_token': 'FAKE-DEVICE-REFRESH', 'api_key': 'FAKE-MUSE-API-KEY', 'api_base_url': 'https://api.meta.ai',
        'expires_at': time.time() + 7200, 'mechanism': 'oauth', 'obtained_via': 'device_code',
        'subs_active': True, 'user_email': EMAIL, 'user_full_name': 'Fake Person'}}}
    return auth, muse.project(auth)


def record(provider, auth, entry, due=False):
    return {'origin': renewal.ORIGINS[provider], 'auth': auth, 'entry': entry, 'state': 'ready',
            'next_at': time.time() - 1 if due else entry['expires_at'] - 300,
            'published': renewal.digest(entry), 'updated_at': time.time()}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = app.Store(Path(self.tmp.name) / 'private', create=True)
        self.addCleanup(self.store.__exit__)
        self.accounts = app.default_accounts() + [{'id': 'muse-1', 'name': 'Muse', 'plan': 'muse_code', 'source': 'cli_current'}]
        self.store.write('accounts.json', {'accounts': self.accounts})
        self.store.write('vault.json', live.encrypt_vault({}, KEY, SALT))
        p = patch('live_usage.vault_cache.get', return_value=KEY)
        p.start(); self.addCleanup(p.stop)

    def save(self, entries, records):
        self.store.write('vault.json', live.encrypt_vault(entries, KEY, SALT), replace=True)
        self.store.write(renewal.REGISTRY, renewal.seal(records, KEY), replace=True)

    def test_seven_claude_accounts_renew_independently_and_keep_orgs(self):
        entries, saved, replacements = {}, {}, {}
        for n, account in enumerate(self.accounts[:7]):
            org = f'{n+2:08d}-2222-2222-2222-222222222222'
            auth, entry = claude_pair(str(n), org, account['plan'])
            entries[account['id']] = entry
            saved[account['id']] = record('claude', auth, entry, True)
            replacements[account['id']] = claude_pair('new-' + str(n), org, account['plan'])
        self.save(entries, saved)
        def perform(auth, *, account):
            self.assertEqual(auth, saved[account['id']]['auth'])
            self.assertEqual(renewal.records(self.store, KEY)[account['id']]['state'], 'in_flight')
            return replacements[account['id']]
        with patch.object(claude, 'perform', side_effect=perform) as call, patch.object(live, 'default_source', side_effect=AssertionError('shared auth')):
            renewal.cycle(self.store, KEY)
            renewal.cycle(self.store, KEY)
        self.assertEqual(call.call_count, 7)
        for ident, (_, entry) in replacements.items():
            self.assertEqual(live.load_vault(self.store)[ident], entry)
        self.assertNotIn('FAKE-CLAUDE-REFRESH', json.dumps(live.load_vault(self.store)))
        self.assertNotIn('FAKE-', json.dumps(self.store.read(renewal.REGISTRY)))

    def test_same_email_max_and_team_enroll_separately(self):
        for ident, plan, org in [('max-1', 'max_20x', ORG), ('team-1', 'team_premium', OTHER_ORG)]:
            auth, entry = claude_pair(ident, org, plan)
            with patch.object(claude, 'perform', return_value=(auth, entry)):
                renewal.enroll(self.store, self.accounts, ident, plan)
        entries = live.load_vault(self.store)
        self.assertNotEqual(entries['max-1']['claude_identity'], entries['team-1']['claude_identity'])
        with self.assertRaises(app.UsageError):
            renewal.choose(self.accounts, entries, EMAIL)
        self.assertEqual(renewal.choose(self.accounts, entries, EMAIL, 'team_premium')['id'], 'team-1')

    def test_expired_legacy_claude_capture_does_not_block_new_enrollment(self):
        _, previous = claude_pair('expired', plan='team_premium')
        previous.pop('claude_identity')
        previous['expires_at'] = time.time() - 86400
        self.save({'team-1': previous}, {})
        fresh, entry = claude_pair('new-independent', plan='team_premium')
        with patch.object(claude, 'perform', return_value=(fresh, entry)) as perform, \
             patch('claude_switch.read_profile', side_effect=AssertionError('must not validate old token')), \
             patch.object(live, 'read_access_token', side_effect=AssertionError('must not read shared auth')):
            result = renewal.enroll(self.store, self.accounts, EMAIL, 'team_premium')
        self.assertEqual(result['account_id'], 'team-1')
        perform.assert_called_once_with(account=self.accounts[2])
        self.assertEqual(live.load_vault(self.store)['team-1'], entry)
        self.assertEqual(renewal.records(self.store, KEY)['team-1']['auth'], fresh)

    def test_revoked_unexpired_legacy_capture_is_not_contacted_during_enrollment(self):
        _, previous = claude_pair('revoked')
        previous.pop('claude_identity')
        self.save({'max-1': previous}, {})
        fresh, entry = claude_pair('fresh')
        with patch.object(claude, 'perform', return_value=(fresh, entry)), \
             patch('claude_switch.read_profile', side_effect=AssertionError('old token request')):
            renewal.enroll(self.store, self.accounts, 'max-1')
        self.assertEqual(live.load_vault(self.store)['max-1'], entry)

    def test_expired_capture_still_protects_saved_identity_and_organisation(self):
        for bound, mismatch in ((False, 'email'), (True, 'organisation')):
            _, previous = claude_pair('expired')
            previous['expires_at'] = time.time() - 86400
            if not bound:
                previous.pop('claude_identity')
            self.save({'max-1': previous}, {})
            fresh, entry = claude_pair('wrong', OTHER_ORG if bound else ORG)
            if mismatch == 'email':
                entry['identity'] = {'email': 'wrong@example.test'}
            with patch.object(claude, 'perform', return_value=(fresh, entry)), self.assertRaises(live.LiveError) as caught:
                renewal.enroll(self.store, self.accounts, 'max-1')
            self.assertEqual(caught.exception.code, 'account_mismatch')
            self.assertEqual(live.load_vault(self.store)['max-1'], previous)
            self.assertEqual(renewal.records(self.store, KEY), {})

    def test_legacy_enrollment_cannot_duplicate_an_already_bound_subscription(self):
        _, previous = claude_pair('legacy', plan='team_premium')
        previous.pop('claude_identity')
        fresh, entry = claude_pair('existing', plan='team_premium')
        self.save({'team-1': previous, 'team-2': entry}, {})
        with patch.object(claude, 'perform', return_value=(fresh, entry)), self.assertRaises(live.LiveError) as caught:
            renewal.enroll(self.store, self.accounts, 'team-1')
        self.assertEqual(caught.exception.code, 'duplicate_account')
        self.assertEqual(live.load_vault(self.store)['team-1'], previous)

    def test_cancelled_fresh_sign_in_preserves_expired_capture(self):
        _, previous = claude_pair('expired')
        previous.pop('claude_identity')
        previous['expires_at'] = time.time() - 86400
        self.save({'max-1': previous}, {})
        with patch.object(claude, 'perform', side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            renewal.enroll(self.store, self.accounts, 'max-1')
        self.assertEqual(live.load_vault(self.store)['max-1'], previous)
        self.assertEqual(renewal.records(self.store, KEY), {})

    def test_claude_organization_mismatch_suspends_without_publish(self):
        auth, entry = claude_pair()
        self.save({'max-1': entry}, {'max-1': record('claude', auth, entry, True)})
        with patch.object(claude, 'perform', return_value=claude_pair('wrong', OTHER_ORG)) as call:
            renewal.cycle(self.store, KEY); renewal.cycle(self.store, KEY)
        call.assert_called_once()
        self.assertEqual(live.load_vault(self.store)['max-1'], entry)
        self.assertEqual(renewal.records(self.store, KEY)['max-1']['state'], 'suspended')

    def test_sync_does_not_replace_independent_grant_from_shared_context(self):
        auth, entry = claude_pair()
        self.save({'max-1': entry}, {'max-1': record('claude', auth, entry)})
        with patch.object(live, 'read_access_token', side_effect=AssertionError('shared auth read')):
            result, _ = saved_refresh.synchronize(self.store, self.accounts, {'max-1': entry}, KEY, SALT, establish=True)
        self.assertEqual(result, {'max-1': entry})

    def test_muse_enroll_projects_no_refresh_and_tracks_expiry_without_network(self):
        auth, entry = muse_pair()
        with patch.object(muse, 'perform', return_value=(auth, entry)):
            renewal.enroll(self.store, self.accounts, 'muse-1')
        self.assertEqual(live.load_vault(self.store)['muse-1'], entry)
        self.assertNotIn('refresh_token', json.dumps(entry))
        with patch.object(muse, 'perform', side_effect=AssertionError('no background Muse exchange')):
            renewal.cycle(self.store, KEY, now=entry['expires_at'] - 1)
            self.assertEqual(renewal.records(self.store, KEY)['muse-1']['state'], 'ready')
            result = renewal.cycle(self.store, KEY, now=entry['expires_at'] + 1)
        row = result['accounts'][-1]
        self.assertEqual(row['state'], 'reauth_required')
        self.assertEqual(row['mode'], 'expiry_tracking')

    def test_owned_muse_report_overrides_current_login_source_without_reading_it(self):
        _, entry = muse_pair()
        raw = {'subs_usage': {'window': {'used_percent': 7, 'window_duration_mins': 300,
                            'resets_at': time.time() + 7200}}, 'subs_tier_name': 'Muse Code High Usage'}
        with patch('muse_cli_usage.read_credentials', side_effect=AssertionError('shared read')), \
             patch('muse_cli_usage.MuseCliClient.request', return_value=raw) as call:
            data = app.concurrent_report([self.accounts[-1]], self.store, dt.datetime.now(dt.timezone.utc), {'muse-1': entry})
        row = data['accounts'][0]
        self.assertEqual(row['status'], 'live')
        self.assertEqual(row['quota_scope'], 'owned_muse_cli_login')
        self.assertEqual(row['usage']['five_hour']['utilization'], 7)
        call.assert_called_once_with('POST', 'https://api.meta.ai/muse-code/key', entry['access_token'], entry['api_key'])
        self.assertNotIn('FAKE-', json.dumps(data))

    def test_muse_expired_entry_makes_no_request(self):
        _, entry = muse_pair(); entry['expires_at'] = time.time() - 1
        with patch('muse_cli_usage.MuseCliClient.request') as call, self.assertRaises(live.LiveError):
            muse.fetch(entry, set())
        call.assert_not_called()

    def test_foreign_grant_provenance_rejected(self):
        auth, entry = claude_pair()
        item = record('claude', auth, entry); item['origin'] = renewal.ORIGINS['muse_code']
        self.save({'max-1': entry}, {'max-1': item})
        with self.assertRaises(live.LiveError):
            renewal.records(self.store, KEY)


class NativeTests(unittest.TestCase):
    def test_claude_refresh_sends_only_initialization_and_saves_rotated_grant(self):
        old, _ = claude_pair(); fresh, expected = claude_pair('rotated')
        for grant in (old, fresh):
            grant['claudeAiOauth'].update(clientId='9d1c250a-e61b-44d9-88ed-5944d1962f5e', refreshTokenExpiresAt=(time.time()+86400)*1000)
        proc = MagicMock(); proc.returncode = 0
        @contextmanager
        def fake_process(cmd):
            root = Path(cmd[0])
            self.assertEqual(json.loads((root / 'claude/.credentials.json').read_text()), old)
            def communicate(payload, timeout):
                self.assertEqual(json.loads(payload), claude.INIT)
                self.assertNotIn('user', json.dumps(json.loads(payload)))
                (root / 'claude/.credentials.json').write_text(json.dumps(fresh))
                return json.dumps({'type': 'control_response', 'response': {'request_id': 'cc-usage-auth', 'subtype': 'success'}}).encode(), None
            proc.communicate.side_effect = communicate
            yield proc
        with patch.object(isolated_auth, 'command', side_effect=lambda provider, root, args: [str(root), *args]), \
             patch.object(isolated_auth, 'process', fake_process), \
             patch('claude_switch.target_auth', return_value=({}, {'accountUuid': ACCOUNT, 'organizationUuid': ORG, 'emailAddress': EMAIL})):
            auth, entry = claude.perform(old, account={'plan': 'max_20x'})
        self.assertEqual(auth, fresh)
        self.assertEqual(entry, expected)
        proc.communicate.assert_called_once()

    def test_native_login_format_includes_client_and_refresh_expiry(self):
        for expiry in (None, (time.time()+86400)*1000):
            auth, entry = claude_pair()
            auth['claudeAiOauth'].update(clientId='9d1c250a-e61b-44d9-88ed-5944d1962f5e', refreshTokenExpiresAt=expiry)
            self.assertEqual(claude.validate_auth(auth), auth)
            with tempfile.TemporaryDirectory() as directory, app.Store(Path(directory)/'private', create=True) as store:
                store.write(renewal.REGISTRY, renewal.seal({'max-1': record('claude', auth, entry)}, KEY))
                self.assertEqual(renewal.records(store, KEY)['max-1']['auth'], auth)

    def test_invalid_native_client_and_refresh_expiry_are_redacted(self):
        for field, value in [('clientId', 'FAKE-SECRET'), ('clientId', {}),
                             ('refreshTokenExpiresAt', True), ('refreshTokenExpiresAt', -1),
                             ('refreshTokenExpiresAt', float('inf')), ('unknownField', 'FAKE-SECRET')]:
            auth, _ = claude_pair(); auth['claudeAiOauth'][field] = value
            with self.assertRaises(live.LiveError) as caught:
                claude.validate_auth(auth)
            self.assertNotIn('FAKE-SECRET', str(caught.exception))

    def test_native_claude_timeout_is_sanitized(self):
        import subprocess
        auth, _ = claude_pair()
        proc = MagicMock(); proc.communicate.side_effect = subprocess.TimeoutExpired('FAKE-SECRET', 25)
        @contextmanager
        def fake_process(cmd): yield proc
        with patch.object(isolated_auth, 'command', return_value=[]), patch.object(isolated_auth, 'process', fake_process):
            with self.assertRaises(live.LiveError) as caught:
                claude.perform(auth, account={'plan': 'max_20x'})
        self.assertNotIn('FAKE-SECRET', str(caught.exception))

    def test_muse_automatic_exchange_is_prohibited(self):
        with patch.object(isolated_auth, 'process') as call, self.assertRaises(live.LiveError):
            muse.perform(muse_pair()[0])
        call.assert_not_called()

    def test_muse_rpc_cannot_start_sessions_turns_or_logout(self):
        rpc = object.__new__(muse.Rpc)
        rpc.serial = 0; rpc.process = MagicMock()
        for method in ('session/start', 'turn/start', 'account/logout', 'model/list', 'account/login/start'):
            with self.assertRaises(live.LiveError): rpc.send(method, {})
        rpc.process.stdin.write.assert_not_called()

    def test_unverified_native_binary_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / 'claude'; binary.write_bytes(b'not a verified executable')
            with patch('isolated_auth.shutil.which', return_value=str(binary)), self.assertRaises(live.LiveError):
                isolated_auth.native('claude')

    def test_sandbox_does_not_mount_home_or_pass_global_auth_environment(self):
        with patch.object(isolated_auth, 'native', return_value=Path('/fake/native')), patch('isolated_auth.shutil.which', return_value='/usr/bin/bwrap'):
            cmd = isolated_auth.command('claude', '/tmp/owned-fake', claude.ARGS)
        self.assertIn('--clearenv', cmd)
        self.assertIn('--unshare-pid', cmd)
        self.assertNotIn(str(Path.home()), cmd)
        self.assertNotIn('ANTHROPIC_AUTH_TOKEN', cmd)
        self.assertNotIn('--bind', cmd[cmd.index('/owned') + 1:])
        self.assertEqual(cmd[-len(claude.ARGS):], claude.ARGS)

    def test_muse_login_requires_terminal_before_starting_native_process(self):
        with patch('owned_muse.os.open', side_effect=OSError()), patch.object(isolated_auth, 'process') as proc:
            with self.assertRaises(live.LiveError) as caught:
                muse.perform()
        self.assertEqual(caught.exception.code, 'tty_required')
        proc.assert_not_called()

    def test_muse_device_success_uses_only_auth_rpc_and_terminal_output(self):
        from unittest.mock import mock_open
        rpc = object.__new__(muse.Rpc)
        rpc.notifications = [{'method': 'account/loginCompleted', 'params': {'outcome': 'granted'}}]
        value = {'userCode': 'FAKE-1234', 'verificationUrl': 'https://auth.meta.com/oauth/device/?code=FAKE-1234'}
        terminal = mock_open()
        output = io.StringIO()
        with patch('owned_muse.open', terminal), patch.object(rpc, 'call', return_value=value) as call, patch('sys.stdout', output):
            rpc.login()
        call.assert_called_once_with('account/loginStart', {'type': 'deviceCode'})
        self.assertEqual(output.getvalue(), '')
        self.assertIn('FAKE-1234', ''.join(c.args[0] for c in terminal().write.call_args_list))

    def test_muse_bootstrap_identity_mismatch_suppressed(self):
        _, entry = muse_pair()
        with patch('muse_cli_usage.MuseCliClient.request', return_value={'identity': {'email': 'other@example.test'}}):
            with self.assertRaises(live.LiveError) as caught:
                muse.fetch(entry, set())
        self.assertEqual(caught.exception.code, 'account_mismatch')

    def test_owned_and_portal_muse_entries_do_not_share_capture_identity(self):
        import account_identity
        _, entry = muse_pair()
        other = {**entry, 'access_token': 'dca:FAKE-OTHER-DEVICE'}
        self.assertFalse(account_identity.same_login(entry, other, 'muse_code'))
        self.assertFalse(account_identity.same_login(entry, {'provider': 'muse_code', 'subject_id': '1', 'team_id': '2'}, 'muse_code'))

    def test_muse_auth_cannot_redirect_api_host(self):
        auth, _ = muse_pair(); auth['providers']['meta']['api_base_url'] = 'https://evil.example'
        with self.assertRaises(live.LiveError): muse.validate_auth(auth)


if __name__ == '__main__':
    unittest.main()
