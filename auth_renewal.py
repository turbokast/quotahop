"""Explicit background renewal of independently enrolled subscription grants.

The ordinary usage report never starts this worker. Captured/shared refresh
credentials and switch undo records are never inputs to renewal.
"""
import base64
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import select
import signal
import stat
import subprocess
import sys
import time

import account_identity
import live_usage as live
import owned_codex
import owned_claude
import owned_muse

AAD = b'cc-usage-owned-auth-v1'
REGISTRY = 'renewal.json'
STATUS = 'renewal-status.json'
STOP = 'renewal-stop.json'
LOCK = 'renewal-lock.json'
RESERVED = {'renewal', 'renewal-status', 'renewal-stop', 'renewal-lock'}
BACKENDS = {'codex': owned_codex, 'claude': owned_claude, 'muse_code': owned_muse}
ORIGINS = {'codex': 'isolated-codex-device-v1', 'claude': owned_claude.ORIGIN, 'muse_code': owned_muse.ORIGIN}
LIMITATIONS = {provider: 'Requires a fresh independent sign-in with --auth-enroll ACCOUNT; captured CLI grants are never renewed.' for provider in BACKENDS}
LIMITATIONS['muse_code'] = 'Independent sign-in and expiry tracking supported; Muse device-token renewal is unavailable. Re-enroll when expired.'


def provider_for(account):
    return 'codex' if account['plan'] == 'codex' else 'muse_code' if account['plan'] == 'muse_code' else 'claude'


def perform(account, auth=None):
    backend = BACKENDS[provider_for(account)]
    kwargs = {'account': account} if backend is owned_claude else {}
    return backend.perform(**kwargs) if auth is None else backend.perform(auth, **kwargs)



def seal(records, key):
    aes, _ = live.crypto()
    nonce = os.urandom(12)
    ciphertext = aes(key).encrypt(nonce, json.dumps(records, allow_nan=False).encode(), AAD)
    return {'version': 1, 'nonce': base64.b64encode(nonce).decode(), 'ciphertext': base64.b64encode(ciphertext).decode()}


def records(store, key):
    try:
        envelope = store.read(REGISTRY)
    except FileNotFoundError:
        return {}
    try:
        if set(envelope) != {'version', 'nonce', 'ciphertext'} or envelope['version'] != 1:
            raise ValueError()
        aes, _ = live.crypto()
        value = live.json_object(aes(key).decrypt(base64.b64decode(envelope['nonce'], validate=True),
                                                 base64.b64decode(envelope['ciphertext'], validate=True), AAD))
        for ident, item in value.items():
            if (not isinstance(item, dict) or item.get('origin') not in ORIGINS.values()
                    or item.get('state') not in ('ready', 'in_flight', 'suspended', 'reauth_required', 'cli_owned', 'legacy_hold')
                    or set(item) - {'origin', 'auth', 'entry', 'state', 'next_at', 'published', 'updated_at', 'ownership'}
                    or not {'origin', 'auth', 'entry', 'state', 'next_at', 'published', 'updated_at'} <= set(item)):
                raise ValueError()
            provider = next(p for p, origin in ORIGINS.items() if origin == item['origin'])
            BACKENDS[provider].validate_auth(item['auth'])
            # Reuse the existing strict access-only schema validator.
            live.unlock_vault(live.encrypt_vault({ident: item['entry']}, key, b'0' * 16), None, key=key)
            if (type(item['next_at']) not in (int, float) or type(item['updated_at']) not in (int, float)
                    or not isinstance(item['published'], str) or len(item['published']) != 64):
                raise ValueError()
            if provider == 'codex':
                if (item['entry'].get('provider') != 'codex'
                        or item['entry']['access_token'] != item['auth']['tokens']['access_token']
                        or item['entry']['account_id'] != item['auth']['tokens']['account_id']):
                    raise ValueError()
            elif not BACKENDS[provider].matches(item['auth'], item['entry']):
                raise ValueError()
            if 'ownership' in item and item['ownership'] not in ('private', 'cli'):
                raise ValueError()
            if provider == 'claude':
                if item.get('ownership') == 'cli':
                    item['state'] = 'cli_owned'
                elif 'ownership' not in item and item['state'] == 'ready':
                    # Old versions exported access tokens without tracking use by
                    # live sessions. An absent current-file match proves nothing.
                    item['state'] = 'legacy_hold'
        return value
    except Exception:
        raise live.LiveError('renewal_locked', 'Cannot read encrypted renewal grants; no authentication attempted.') from None


@contextmanager
def vault_lock(store, *, timeout=0):
    end = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(store.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= end:
                raise live.LiveError('vault_busy', 'Another credential operation is active; try again shortly.') from None
            time.sleep(min(0.1, max(0, end - time.monotonic())))
    try:
        yield
    finally:
        fcntl.flock(store.fd, fcntl.LOCK_UN)


@contextmanager
def worker_lock(store):
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=store.fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise live.LiveError('unsafe_lock', 'Renewal lock must be a private regular file.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise live.LiveError('renewal_busy', 'Background renewal or enrollment is already running. Use --background stop first.') from None
        yield
    finally:
        os.close(fd)


def running(store):
    try:
        with worker_lock(store):
            return False
    except live.LiveError as exc:
        if exc.code == 'renewal_busy':
            return True
        raise


def unlock(store):
    try:
        envelope = store.read('vault.json')
    except FileNotFoundError:
        salt = os.urandom(16)
        key = live.vault_key(live.passphrase(confirm=True), salt)
        envelope = live.encrypt_vault({}, key, salt)
        store.write('vault.json', envelope)
        live.remember_unlock(store, envelope, key)
        return {}, key, salt
    return live.unlock_cached(store, envelope)


def choose(accounts, entries, selector, plan=None):
    from claude_usage import UsageError
    candidates = [a for a in accounts if plan is None or a['plan'] == plan]
    matches = [a for a in candidates if a['id'] == selector] or [a for a in candidates if selector.casefold() in [
        a['name'].casefold(), *[v.casefold() for v in account_identity.saved(entries.get(a['id'], {})).values()]]]
    if len(matches) != 1:
        raise UsageError('Select one saved account by email/name or ID; use --plan to disambiguate. Add a new row with --add first.')
    return matches[0]


def digest(entry):
    return hashlib.sha256(entry.get('access_token', '').encode()).hexdigest()


def require_same(previous, fresh):
    old_email = account_identity.saved(previous).get('email')
    new_email = account_identity.saved(fresh).get('email')
    if (('access_token' in previous and previous.get('provider', 'claude') != fresh.get('provider', 'claude'))
            or (previous.get('claude_identity') and previous['claude_identity'] != fresh.get('claude_identity'))
            or (previous.get('account_id') and previous['account_id'] != fresh.get('account_id'))
            or (old_email and (not new_email or old_email.casefold() != new_email.casefold()))):
        raise live.LiveError('account_mismatch', 'Signed-in identity/workspace does not match this saved account; nothing replaced.')


def enrolled(store, key, ident):
    return ident in records(store, key)


def claude_handoff(store, key, ident, entry):
    """Prepare a grant for one-way transfer. Caller holds the vault lock.

    Never replay a grant already handed to a CLI, even after switching away.
    A legacy isolated grant can be transferred once, but cannot auto-renew.
    """
    item = records(store, key).get(ident)
    if (not item or item['origin'] != owned_claude.ORIGIN
            or item['state'] not in ('ready', 'legacy_hold')
            or item.get('ownership') == 'cli'):
        return None
    if not owned_claude.matches(item['auth'], entry):
        raise live.LiveError('grant_changed', 'Saved access token differs from its independent grant; switch refused to avoid exporting an older token.')
    return dict(item['auth']['claudeAiOauth'])


def commit_claude_handoff(store, key, ident, entry):
    """Retire renewal durably BEFORE any CLI write. Failure never re-enables it."""
    saved = records(store, key)
    item = saved.get(ident)
    if item is None or item['origin'] != owned_claude.ORIGIN:
        return
    if not owned_claude.matches(item['auth'], entry):
        raise live.LiveError('grant_changed', 'Claude grant changed before handoff; no authentication changed.')
    item.update(state='cli_owned', ownership='cli')
    store.write(REGISTRY, seal(saved, key), replace=True)
    from claude_usage import validate_accounts
    accounts = validate_accounts(store.read('accounts.json'))
    store.write(STATUS, public_status(accounts, saved, active=running(store)), replace=True)


def forget_locked(store, key, ident):
    saved = records(store, key)
    if ident in saved:
        del saved[ident]
        store.write(REGISTRY, seal(saved, key), replace=True)


def enroll(store, accounts, selector, plan=None, *, unlock_key=None):
    from claude_usage import validate_accounts
    # One owner for every grant. Enrollment cannot race the background worker.
    with worker_lock(store):
        with vault_lock(store):
            entries, key, salt = (unlock(store) if unlock_key is None else
                                  live.unlock_vault(store.read('vault.json'), None, key=unlock_key))
            account = choose(accounts, entries, selector, plan)
            provider = provider_for(account)
            if enrolled(store, key, account['id']):
                raise live.LiveError('already_enrolled', 'Already enrolled. To sign in again, use --auth-forget ACCOUNT first.')
            previous = entries.get(account['id'], {})
            # Enrollment establishes a NEW independent grant, so an expired or
            # revoked capture must never be needed to authorize it. The native
            # backend verifies the fresh login's plan/profile. Preserve any
            # existing email and account/organisation binding below; for legacy
            # captures without an org binding, establish it from the fresh login.
        # No vault lock while the user completes device authorization.
        previous_status = public_status(accounts, records(store, key))
        store.write(STATUS, {**previous_status, 'operation': 'enrollment'}, replace=True)
        try:
            auth, entry = perform(account)
        finally:
            store.write(STATUS, previous_status, replace=True)
        require_same(previous, entry)
        if '@' in account['name']:
            require_same({'identity': {'email': account['name']}}, entry)
        with vault_lock(store):
            current_accounts = validate_accounts(store.read('accounts.json'))
            if account not in current_accounts:
                raise live.LiveError('account_changed', 'Account configuration changed during sign-in; enrollment discarded.')
            current, _, _ = live.unlock_vault(store.read('vault.json'), None, key=key)
            if current.get(account['id'], {}) != previous:
                raise live.LiveError('account_changed', 'Saved credentials changed during sign-in; enrollment discarded.')
            if provider == 'claude' and entry.get('claude_identity') and any(
                    other != account['id'] and value.get('provider', 'claude') == 'claude'
                    and value.get('claude_identity') == entry['claude_identity']
                    for other, value in current.items()):
                raise live.LiveError('duplicate_account', 'This Claude account and organisation are already saved under another entry; nothing replaced.')
            saved = records(store, key)
            now = time.time()
            saved[account['id']] = {'origin': ORIGINS[provider], 'auth': auth, 'entry': entry,
                'state': 'ready', 'next_at': entry['expires_at'] - (0 if provider == 'muse_code' else 300), 'published': digest(previous), 'updated_at': now}
            if provider == 'claude':
                saved[account['id']]['ownership'] = 'private'
            # Persist the rotated/independent grant before publishing its access token.
            store.write(REGISTRY, seal(saved, key), replace=True)
            current[account['id']] = entry
            store.write('vault.json', live.encrypt_vault(current, key, salt), replace=True)
            store.write(STATUS, public_status(current_accounts, saved), replace=True)
        message = ('Independent Muse login enrolled; background expiry tracking available. Device-token renewal is not supported by this Muse CLI.'
                   if provider == 'muse_code' else 'Independent login enrolled. Start automatic renewal with --background start.')
        return {'message': message, 'account_id': account['id']}


def enroll_and_start(store, data_dir, accounts, selector, plan=None):
    """One unlock for enrollment and startup, including long interactive logins.

    Stop a prior worker before taking ownership of grants. Failed/cancelled
    enrollment restores that worker; first-time failure never starts one.
    """
    with vault_lock(store, timeout=40):
        entries, key, _ = unlock(store)
        account = choose(accounts, entries, selector, plan)
        if enrolled(store, key, account['id']):
            raise live.LiveError('already_enrolled', 'Already enrolled. To sign in again, use --auth-forget ACCOUNT first.')
    was_running = running(store)
    if was_running:
        stop(store)
    try:
        result = enroll(store, accounts, selector, plan, unlock_key=key)
    except BaseException:
        if was_running:
            try:
                start(store, data_dir, key=key)
            except Exception:
                raise live.LiveError('restart_failed', 'Enrollment did not complete and the previous background worker could not restart. Run --background start.') from None
        raise
    try:
        start(store, data_dir, key=key)
    except Exception:
        # The grant is already safely committed: don't invite a duplicate login.
        return {**result, 'running': False, 'background_error': True,
                'message': 'Independent login saved, but background startup failed. Retry --background start; do not enroll again.'}
    provider = provider_for(account)
    return {**result, 'running': True,
            'message': ('Independent Muse login saved; background expiry tracking started. Muse device-token renewal remains unavailable.'
                        if provider == 'muse_code' else 'Independent login saved; background renewal started automatically.')}


def forget(store, accounts, selector, plan=None):
    with vault_lock(store):
        entries, key, _ = unlock(store)
        account = choose(accounts, entries, selector, plan)
        forget_locked(store, key, account['id'])
        store.write(STATUS, public_status(accounts, records(store, key), active=running(store)), replace=True)
    return {'message': 'Independent renewal grant removed locally; no provider logout or revocation. Saved access token remains.', 'account_id': account['id']}


def public_status(accounts, saved, *, active=False):
    rows = []
    for account in accounts:
        item = saved.get(account['id'])
        provider = provider_for(account)
        state = item['state'] if item else 'needs_enrollment'
        mode = 'expiry_tracking' if provider == 'muse_code' else 'automatic_renewal'
        if state in ('cli_owned', 'legacy_hold'):
            mode = 'cli_renewal' if state == 'cli_owned' else 'renewal_hold'
        rows.append({'id': account['id'], 'name': account['name'], 'provider': provider, 'state': state, 'mode': mode,
                     'next_at': item['next_at'] if item and state == 'ready' else None,
                     'expires_at': item['entry']['expires_at'] if item else None,
                     'message': ('Claude Code owns this grant; QuotaHop will never refresh it. Re-enroll for a new independent standby grant.' if state == 'cli_owned'
                                 else 'Legacy Claude grant may be in use by a session. Automatic renewal blocked; explicitly switch to hand it to Claude Code, or re-enroll.' if state == 'legacy_hold'
                                 else 'Renewal outcome uncertain: re-enroll; automatic retries disabled.' if state in ('in_flight', 'suspended')
                                 else LIMITATIONS[provider] if provider == 'muse_code' else 'Independent grant; renews five minutes before expiry.' if item else LIMITATIONS[provider])})
    return {'running': active, 'checked_at': time.time(), 'ownership_version': 2, 'accounts': rows}


def status(store, accounts):
    active = running(store)
    try:
        result = store.read(STATUS)
    except FileNotFoundError:
        result = public_status(accounts, {})
    configured = {a['id']: a for a in accounts}
    # Removed accounts never remain in status output; freshly added rows appear.
    old = {a['id']: a for a in result.get('accounts', []) if a.get('id') in configured}
    defaults = public_status(accounts, {})['accounts']
    result['accounts'] = [{**old[a['id']], 'name': a['name']}
                          if a['id'] in old and old[a['id']].get('provider') == a['provider'] and old[a['id']].get('state') != 'unsupported' else a
                          for a in defaults]
    result['running'] = active
    if result.get('ownership_version') != 2:
        for row in result['accounts']:
            if row['provider'] == 'claude' and row['state'] == 'ready':
                row.update(state='legacy_hold', mode='renewal_hold', next_at=None,
                           message='Legacy grant ownership is unknown; automatic renewal is blocked by this version.')
    result['message'] = ('Independent sign-in is running in another terminal.' if result.get('operation') == 'enrollment'
                         else 'Background renewal running; vault key held in memory.') if active else 'Background renewal stopped; no automatic renewal is running.'
    return result


def stop(store, timeout=45):
    if not running(store):
        return {'message': 'Background renewal is stopped.', 'running': False}
    try:
        if store.read(STATUS).get('operation') == 'enrollment':
            raise live.LiveError('enrollment_active', 'Independent sign-in is active in another terminal; cancel it there with Ctrl-C.')
    except FileNotFoundError:
        pass
    # No PID-based signalling: never risk signalling a reused/unrelated PID.
    store.write(STOP, {'requested_at': time.time()}, replace=True)
    end = time.monotonic() + timeout
    while running(store) and time.monotonic() < end:
        time.sleep(0.1)
    if running(store):
        raise live.LiveError('stop_pending', 'Stop requested; operation is still finishing. Check --background status shortly.')
    return {'message': 'Background renewal stopped; in-memory unlock released.', 'running': False}


def cycle(store, key, now=None):
    """At most one native auth operation per due account, serialized under lock."""
    from claude_usage import validate_accounts
    now = time.time() if now is None else now
    with vault_lock(store):
        accounts = validate_accounts(store.read('accounts.json'))
        saved = records(store, key)
        entries, _, salt = live.unlock_vault(store.read('vault.json'), None, key=key)
        original_saved = json.dumps(saved, sort_keys=True)
        valid = {a['id']: a for a in accounts}
        saved = {ident: item for ident, item in saved.items() if ident in valid
                 and item['origin'] == ORIGINS[provider_for(valid[ident])]}
        for ident, item in saved.items():
            try:
                store.read(STOP)
                break
            except FileNotFoundError:
                pass
            if item['state'] == 'in_flight':
                item['state'] = 'suspended'  # crash after sending: never replay an old refresh token
            if item['state'] != 'ready':
                continue
            current_digest = digest(entries.get(ident, {}))
            if current_digest not in (item['published'], digest(item['entry'])):
                item['state'] = 'suspended'
                continue
            if item['next_at'] <= now and item['origin'] == owned_muse.ORIGIN:
                item['state'] = 'reauth_required'
                continue  # No native key re-mint or device-token refresh attempt.
            if item['next_at'] <= now:
                item['state'] = 'in_flight'
                store.write(REGISTRY, seal(saved, key), replace=True)
                store.write(STATUS, public_status(accounts, saved, active=True), replace=True)
                try:
                    fresh, entry = perform(valid[ident], item['auth'])
                    require_same(item['entry'], entry)
                    item.update(auth=fresh, entry=entry, state='ready', next_at=entry['expires_at'] - 300, updated_at=time.time())
                except Exception:
                    # Provider errors can reflect secrets; persist a fixed state only.
                    # A lost successful response may have rotated the token. No replay.
                    item['state'] = 'suspended'
                store.write(REGISTRY, seal(saved, key), replace=True)
            if item['state'] == 'ready':
                if entries.get(ident) != item['entry']:
                    entries[ident] = item['entry']
                    store.write('vault.json', live.encrypt_vault(entries, key, salt), replace=True)
                item['published'] = digest(item['entry'])
        if json.dumps(saved, sort_keys=True) != original_saved:
            store.write(REGISTRY, seal(saved, key), replace=True)
        result = public_status(accounts, saved, active=True)
        store.write(STATUS, result, replace=True)
        return result


def start(store, data_dir, *, key=None):
    with worker_lock(store):
        with vault_lock(store):
            if key is None:
                _, key, _ = unlock(store)
            else:
                # Revalidate with the in-memory enrollment key; never send it
                # through argv, environment, status, or the JSON result.
                live.unlock_vault(store.read('vault.json'), None, key=key)
            saved = records(store, key)
            if not any(i['state'] == 'ready' for i in saved.values()):
                raise live.LiveError('enrollment_required', 'No grants eligible for QuotaHop renewal. CLI-owned and legacy-held Claude grants cannot be renewed here; enroll a fresh independent standby grant with --auth-enroll.')
            store.delete(STOP)
    read_fd, write_fd = os.pipe()
    ready_read, ready_write = os.pipe()
    proc = None
    try:
        proc = subprocess.Popen([sys.executable, '-B', str(Path(__file__).absolute()), '--worker',
                                 str(Path(data_dir).absolute()), str(read_fd), str(ready_write)],
                                pass_fds=(read_fd, ready_write), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, umask=0o077)
        os.close(read_fd)
        read_fd = -1
        os.close(ready_write)
        ready_write = -1
        os.write(write_fd, key)
        os.close(write_fd)
        write_fd = -1
        if not select.select([ready_read], [], [], 10)[0] or os.read(ready_read, 1) != b'1':
            raise live.LiveError('worker_start', 'Renewal worker could not start; no authentication attempted.')
        proc.wait(timeout=5)  # reap the short-lived launcher; worker was explicitly detached
        return {'message': 'Background renewal started. Key stays in memory until --background stop or --lock. No restart after reboot.', 'running': True}
    except BaseException:
        if proc is not None:
            proc.kill()
            proc.wait()
        raise
    finally:
        for fd in (read_fd, write_fd, ready_read, ready_write):
            if fd >= 0:
                os.close(fd)


def worker(data_dir, key_fd, ready_fd):
    from claude_usage import Store, validate_accounts
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Prevent other unprivileged processes from opening /proc/PID/mem or fds.
    if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0:
        return 1
    with os.fdopen(key_fd, 'rb') as stream:
        key = stream.read(33)
    if len(key) != 32:
        return 1
    cancelled = False

    def shutdown(*_):
        nonlocal cancelled
        cancelled = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, shutdown)
    with Store(data_dir) as store, worker_lock(store):
        saved = records(store, key)
        accounts = validate_accounts(store.read('accounts.json'))
        store.write(STATUS, public_status(accounts, saved, active=True), replace=True)
        os.write(ready_fd, b'1')
        os.close(ready_fd)
        try:
            while not cancelled:
                try:
                    store.read(STOP)
                    break
                except FileNotFoundError:
                    pass
                try:
                    result = cycle(store, key)
                    if not any(row['state'] == 'ready' for row in result['accounts']):
                        break
                except live.LiveError as exc:
                    if exc.code != 'vault_busy':
                        raise
                # No quota polling. This is a local expiry/stop check only.
                for _ in range(10):
                    if cancelled:
                        break
                    time.sleep(0.5)
        finally:
            # Status age remains meaningful even after an abnormal exit.
            try:
                result = store.read(STATUS)
                result['running'] = False
                store.write(STATUS, result, replace=True)
            except Exception:
                pass
    return 0


def render_status(value):
    from claude_usage import duration
    lines = [value['message']]
    for row in value.get('accounts', []):
        label = row['state'].replace('_', ' ')
        if row['state'] == 'ready':
            label += (' · sign-in expires ' if row['provider'] == 'muse_code' else ' · renewal due ') + duration(max(0, row['next_at'] - time.time()))
        lines.append(f"  {row['name']}: {label}")
    return '\n'.join(lines)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 5 or sys.argv[1] != '--worker':
            raise ValueError()
        if os.fork():
            os._exit(0)
        os.setsid()
        raise SystemExit(worker(sys.argv[2], int(sys.argv[3]), int(sys.argv[4])))
    except Exception:
        # Never leak SDK/provider errors, grants, or stack traces to logs.
        raise SystemExit(1)
