"""Independent Muse device enrollment. OAuth renewal is not exposed by Muse 1.4.6.

Never re-mint keys as a substitute for renewing the device access token. Owned
Muse grants participate in reporting and background expiry tracking only.
"""
import json
import math
import os
import re
import time
from urllib.parse import urlsplit, parse_qs

import account_identity
import isolated_auth as isolated
import live_usage as live
from owned_codex import Rpc as BaseRpc, private_read

ORIGIN = 'isolated-muse-device-v1'
METHODS = frozenset({'initialize', 'initialized', 'account/loginStart', 'account/read'})


def validate_auth(auth):
    if not isinstance(auth, dict) or set(auth) != {'schema_version', 'providers'} or auth['schema_version'] != 1:
        raise live.LiveError('invalid_auth', 'Unsupported isolated Muse grant format.')
    providers = auth['providers']
    if not isinstance(providers, dict) or set(providers) != {'meta'} or not isinstance(providers['meta'], dict):
        raise live.LiveError('invalid_auth', 'Expected one independent Meta device grant.')
    meta = providers['meta']
    if (set(meta) - {'access_token', 'refresh_token', 'expires_at', 'mechanism', 'obtained_via', 'api_key',
                    'api_base_url', 'user_full_name', 'user_email', 'user_avatar_url', 'subs_active'}
            or meta.get('mechanism') != 'oauth' or meta.get('obtained_via') != 'device_code'
            or meta.get('api_base_url') not in ('https://api.meta.ai', 'https://api.meta.ai/')
            or meta.get('subs_active') is not True):
        raise live.LiveError('invalid_auth', 'Expected an active independent Muse subscription grant.')
    live.validate_token(meta.get('api_key'))
    if meta.get('refresh_token') is not None:
        live.validate_token(meta['refresh_token'])
    if not isinstance(meta.get('access_token'), str) or not re.fullmatch(r'dca:[A-Za-z0-9._~+/=-]{10,8192}', meta['access_token']):
        raise live.LiveError('invalid_auth', 'Invalid Muse device access token.')
    expiry = meta.get('expires_at')
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
        raise live.LiveError('auth_expiry', 'Muse did not provide a usable device grant expiry.')
    return auth


def project(auth):
    meta = validate_auth(auth)['providers']['meta']
    entry = {'provider': 'muse_code', 'kind': 'cli_owned', 'access_token': meta['access_token'],
             'api_key': meta['api_key'], 'expires_at': meta['expires_at']}
    identity = account_identity.extract(meta.get('user_email'), meta.get('user_full_name'),
                                        tuple(str(v) for k, v in meta.items() if k in ('access_token', 'api_key', 'refresh_token')))
    if not identity.get('email'):
        raise live.LiveError('auth_identity', 'Muse did not provide an account email; enrollment refused.')
    entry['identity'] = identity
    return entry


def validate_entry(entry):
    if (not isinstance(entry, dict) or set(entry) != {'provider', 'kind', 'access_token', 'api_key', 'expires_at', 'identity'}
            or entry['provider'] != 'muse_code' or entry['kind'] != 'cli_owned'):
        raise ValueError('Invalid isolated Muse entry')
    live.validate_token(entry['api_key'])
    if not isinstance(entry['access_token'], str) or not re.fullmatch(r'dca:[A-Za-z0-9._~+/=-]{10,8192}', entry['access_token']):
        raise ValueError('Invalid Muse device access token')
    expiry = entry['expires_at']
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
        raise ValueError('Invalid Muse expiry')
    account_identity.validate(entry['identity'])
    return entry


def matches(auth, entry):
    return project(auth) == entry


class Rpc(BaseRpc):
    def send(self, method, params, *, notification=False):
        if (method not in METHODS or (method == 'account/loginStart' and params != {'type': 'deviceCode'})
                or (method == 'account/read' and params != {})):
            raise live.LiveError('prohibited_rpc', 'Muse method prohibited by the authentication allowlist.')
        self.serial += 1
        value = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            value['id'] = self.serial
        try:
            self.process.stdin.write(json.dumps(value).encode() + b'\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise live.LiveError('auth_process', 'Isolated Muse authentication stopped; output suppressed.') from None
        return self.serial

    def call(self, method, params, timeout=20):
        ident = self.send(method, params)
        end = time.monotonic() + timeout
        for _ in range(1000):
            value = self.receive(end)
            if value.get('id') == ident:
                if 'error' in value or not isinstance(value.get('result'), dict):
                    raise live.LiveError('auth_rejected', 'Muse authentication failed; provider output suppressed.')
                return value['result']
            if value.get('method') == 'account/loginCompleted':
                self.notifications.append(value)
        raise live.LiveError('auth_protocol', 'Excessive Muse authentication messages.')

    def login(self):
        try:
            tty = open('/dev/tty', 'w')
        except OSError:
            raise live.LiveError('tty_required', 'Independent Muse sign-in requires an interactive terminal.') from None
        with tty:
            value = self.call('account/loginStart', {'type': 'deviceCode'})
            url = value.get('verificationUrl')
            code = value.get('userCode')
            if not isinstance(url, str) or not isinstance(code, str) or not re.fullmatch(r'[A-Z0-9-]{4,32}', code):
                raise live.LiveError('auth_protocol', 'Unexpected Muse device authorization response.')
            parts = urlsplit(url)
            if (parts.scheme != 'https' or parts.netloc != 'auth.meta.com' or parts.path != '/oauth/device/'
                    or parts.fragment or (parts.query and parse_qs(parts.query) != {'code': [code]})):
                raise live.LiveError('auth_protocol', 'Unexpected Muse authorization URL; suppressed.')
            print('Sign in to this subscription at https://auth.meta.com/oauth/device/', file=tty)
            print('Device code: ' + code, file=tty, flush=True)
        end = time.monotonic() + 600
        for _ in range(10000):
            value = self.notifications.pop(0) if self.notifications else self.receive(end)
            if value.get('method') == 'account/loginCompleted':
                if value.get('params', {}).get('outcome') != 'granted':
                    raise live.LiveError('auth_rejected', 'Muse device sign-in did not complete; existing login unchanged.')
                return
        raise live.LiveError('auth_protocol', 'Excessive Muse authentication messages.')


def perform(auth=None):
    if auth is not None:
        # Intentionally no native process/network: the installed CLI's key
        # re-mint path does not renew DCA expiry and might affect a shared key.
        raise live.LiveError('reauth_required', 'Muse device access renewal is unavailable; use --auth-forget then --auth-enroll.')
    try:
        fd = os.open('/dev/tty', os.O_WRONLY | os.O_NOCTTY)
        os.close(fd)
    except OSError:
        raise live.LiveError('tty_required', 'Independent Muse sign-in requires an interactive terminal.') from None
    with isolated.context('muse') as root:
        with isolated.process(isolated.command('muse', root, ['serve', '--no-session-log', '--disable-shell', '--disable-write'])) as proc:
            rpc = Rpc(proc)
            try:
                rpc.call('initialize', {'clientInfo': {'name': 'cc_usage_auth', 'version': '1.0'},
                                        'capabilities': {'experimentalApi': True}}, timeout=10)
                rpc.send('initialized', {}, notification=True)
                rpc.login()
                auth = validate_auth(private_read(root / 'config/muse/auth.json'))
                entry = project(auth)
                if entry['expires_at'] < time.time() + 600:
                    raise live.LiveError('auth_expiry', 'Muse did not save a grant valid for at least ten minutes.')
                return auth, entry
            finally:
                rpc.selector.close()


def fetch(entry, seen):
    import muse_cli_usage as muse
    validate_entry(entry)
    if entry['expires_at'] <= time.time():
        raise live.LiveError('reauth_required', 'Independent Muse sign-in expired; use --auth-forget and --auth-enroll for this account.')
    key = ('muse-owned', entry['access_token'], entry['api_key'])
    if key in seen:
        raise live.LiveError('duplicate_account', 'Duplicate Muse grant; checked only once.')
    seen.add(key)
    raw = muse.MuseCliClient().request('POST', muse.URL, entry['access_token'], entry['api_key'])
    email = account_identity.saved(raw).get('email')
    if email and email.casefold() != entry['identity']['email'].casefold():
        raise live.LiveError('account_mismatch', 'Muse returned a different account identity; response suppressed.')
    return raw
