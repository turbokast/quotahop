"""Single-owner Claude grants shared with Claude Code. Never runs inference.

Each Claude grant has exactly one renewal owner at a time:

* ``cli_owned``: the grant is in Claude Code's credentials file. Claude Code
  renews it; QuotaHop only mirrors the latest copy (encrypted) so it can take
  the grant back later.
* ``ready``: QuotaHop holds the grant and renews it in the background.

Claude Code serializes refreshes with ``.oauth_refresh.lock`` (plus a legacy
``<config>.lock``) and re-reads the credentials file under that lock before
refreshing; if the file changed it adopts the file instead of refreshing. A
switch therefore holds those locks while replacing the file, and the outgoing
grant returns to QuotaHop. Running sessions move to the new grant and never
refresh the outgoing one again, so no login is lost by switching.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import time

import live_usage as live

OAUTH_KEYS = ('accessToken', 'refreshToken', 'expiresAt', 'refreshTokenExpiresAt', 'clientId',
              'scopes', 'subscriptionType', 'rateLimitTier')
LOCK_WAIT = 10
_seen = {}


def grant(oauth):
    """A full Claude Code grant in the isolated-grant format, or None if access-only."""
    import owned_claude
    if not isinstance(oauth, dict) or not isinstance(oauth.get('refreshToken'), str) or not oauth['refreshToken']:
        return None
    auth = {'claudeAiOauth': {k: oauth[k] for k in OAUTH_KEYS if oauth.get(k) is not None}}
    try:
        return owned_claude.validate_auth(auth)
    except live.LiveError:
        return None


def entry_for(auth, base=None):
    """Access-only vault entry for a grant; keeps the saved identity binding."""
    oauth = auth['claudeAiOauth']
    entry = {'access_token': oauth['accessToken'], 'expires_at': oauth['expiresAt'] / 1000}
    if oauth.get('scopes') is not None:
        entry['scopes'] = oauth['scopes']
    for key in ('claude_identity', 'identity'):
        if (base or {}).get(key):
            entry[key] = base[key]
    return entry


def digest(token):
    return hashlib.sha256((token or '').encode()).hexdigest()


def read_cli():
    import claude_switch
    claude_switch.check_environment()
    source = claude_switch.paths()[0]
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOATIME)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise live.LiveError('unsafe_credentials', 'Claude credentials must be an owned private regular file.')
        value = live.json_object(stream.read(live.MAX_BYTES + 1))
    oauth = value.get('claudeAiOauth')
    return (oauth if isinstance(oauth, dict) else None), (info.st_ino, info.st_mtime_ns, info.st_size)


def changed(stamp):
    """Cheap per-process change detection for the worker's polling loop."""
    if _seen.get('stamp') == stamp and time.monotonic() - _seen.get('at', 0) < 300:
        return False
    _seen.update(stamp=stamp, at=time.monotonic())
    return True


def identify(accounts, entries, saved, oauth):
    """Exact matches only: known token, known refresh token, then server profile."""
    if not isinstance(oauth, dict) or not isinstance(oauth.get('accessToken'), str):
        return None
    claude = [a for a in accounts if a['plan'] in ('max_20x', 'team_premium')]
    token, refresh = oauth['accessToken'], oauth.get('refreshToken')
    for test in (lambda a: entries.get(a['id'], {}).get('access_token') == token
                 or saved.get(a['id'], {}).get('entry', {}).get('access_token') == token,
                 lambda a: bool(refresh) and saved.get(a['id'], {}).get('auth', {}).get('claudeAiOauth', {}).get('refreshToken') == refresh):
        matches = [a['id'] for a in claude if test(a)]
        if len(matches) == 1:
            return matches[0]
    import owned_claude
    if not any(item['origin'] == owned_claude.ORIGIN for item in saved.values()):
        return None  # Nothing to mirror or take back; avoid a profile request.
    expiry = oauth.get('expiresAt')
    if not isinstance(expiry, (int, float)) or expiry / 1000 <= time.time() + 30:
        return None
    import claude_switch
    import saved_refresh
    try:
        profile = claude_switch.read_profile({'access_token': token})
        kind = (profile.get('organization') or {}).get('organization_type')
        plan = {'claude_max': 'max_20x', 'claude_team': 'team_premium'}.get(kind)
        binding = saved_refresh.binding({'accountUuid': (profile.get('account') or {}).get('uuid'),
                                         'organizationUuid': (profile.get('organization') or {}).get('uuid')})
    except (live.LiveError, ValueError, TypeError, KeyError, AttributeError):
        return None
    matches = [a['id'] for a in claude if a['plan'] == plan and binding in (
        entries.get(a['id'], {}).get('claude_identity'), saved.get(a['id'], {}).get('entry', {}).get('claude_identity'))]
    return matches[0] if len(matches) == 1 else None


def reconcile(store, key, accounts, entries, saved, oauth):
    """Caller holds the vault lock. Returns (saved, entries, cli_account_id).

    Mirrors the CLI's current grant, returns every other CLI-held grant to
    QuotaHop, and releases legacy holds that the CLI no longer holds.
    """
    import auth_renewal
    import owned_claude
    now = time.time()
    current = identify(accounts, entries, saved, oauth)
    cli_grant = grant(oauth)
    if current is None and isinstance(oauth, dict) and oauth.get('accessToken'):
        # The CLI holds a login we cannot attribute (offline, throttled or
        # foreign). Reclaiming anything now could create a second owner.
        return saved, entries, None
    for ident, item in saved.items():
        if item['origin'] != owned_claude.ORIGIN or ident == current:
            continue
        if item['state'] in ('cli_owned', 'legacy_hold') and grant(item['auth']['claudeAiOauth']):
            if cli_grant and item['auth']['claudeAiOauth']['refreshToken'] == cli_grant['claudeAiOauth']['refreshToken']:
                continue  # Same grant still in the CLI under an unknown identity: never two owners.
            entry = entry_for(item['auth'], entries.get(ident) or item['entry'])
            item.update(entry=entry, state='ready', ownership='private', published=auth_renewal.digest(entry),
                        next_at=entry['expires_at'] - 300, updated_at=now)
            item.pop('blocked', None)
            entries[ident] = entry
    if current is not None and cli_grant is not None:
        item = saved.get(current)
        entry = entry_for(cli_grant, entries.get(current) or (item or {}).get('entry'))
        same = item is not None and item['auth']['claudeAiOauth'].get('refreshToken') == cli_grant['claudeAiOauth']['refreshToken']
        if item is None or item['state'] != 'ready' or same:
            # Claude Code holds and renews this grant. Mirror its latest copy.
            saved[current] = {'origin': owned_claude.ORIGIN, 'auth': cli_grant, 'entry': entry, 'state': 'cli_owned',
                              'ownership': 'cli', 'next_at': 0, 'published': auth_renewal.digest(entry), 'updated_at': now}
            entries[current] = entry
        # Otherwise QuotaHop renews its own independent grant for this account.
    return saved, entries, current


@contextmanager
def refresh_locks(config_dir):
    """Claude Code's OAuth refresh locks (mkdir style). Never break another holder's lock."""
    config_dir = Path(os.path.abspath(config_dir))
    real = Path(os.path.realpath(config_dir))
    targets = [(config_dir, '.oauth_refresh.lock'), (real.parent, real.name + '.lock')]
    held = []
    try:
        for directory, name in targets:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                end = time.monotonic() + LOCK_WAIT
                while True:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                        break
                    except FileExistsError:
                        if time.monotonic() >= end:
                            raise live.LiveError('claude_refreshing', 'Claude Code is refreshing its login; retry in a few seconds. No authentication changed.') from None
                        time.sleep(0.2)
                held.append((fd, name, os.stat(name, dir_fd=fd, follow_symlinks=False).st_ino))
            except BaseException:
                os.close(fd)
                raise
        yield
    finally:
        for fd, name, inode in reversed(held):
            try:
                if os.stat(name, dir_fd=fd, follow_symlinks=False).st_ino == inode:
                    os.rmdir(name, dir_fd=fd)
            except FileNotFoundError:
                pass
            finally:
                os.close(fd)


def sync(store, key, *, force=False):
    """Worker/report entry point. Caller holds the vault lock."""
    import auth_renewal
    from claude_usage import validate_accounts
    try:
        oauth, stamp = read_cli()
    except (live.LiveError, OSError, ValueError):
        return None
    if not force and not changed(stamp):
        return None
    accounts = validate_accounts(store.read('accounts.json'))
    saved = auth_renewal.records(store, key)
    entries, _, salt = live.unlock_vault(store.read('vault.json'), None, key=key)
    before = (repr(sorted(saved.items())), repr(sorted(entries.items())))
    saved, entries, current = reconcile(store, key, accounts, dict(entries), saved, oauth)
    if repr(sorted(saved.items())) != before[0]:
        store.write(auth_renewal.REGISTRY, auth_renewal.seal(saved, key), replace=True)
    if repr(sorted(entries.items())) != before[1]:
        store.write('vault.json', live.encrypt_vault(entries, key, salt), replace=True)
    return current
