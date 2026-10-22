"""Local password changes with an encrypted, recoverable multi-file commit."""
import base64
import json
import os
from types import SimpleNamespace

import auth_renewal as renewal
import live_usage as live
import vault_cache

JOURNAL = 'vault-rekey.json'
FILES = ('renewal.json', 'switch-backup.json', 'vault.json')


def optional(store, name):
    try:
        return store.read(name)
    except FileNotFoundError:
        return None


def backup_value(envelope, key):
    """The undo record uses the vault cipher, but contains a different payload."""
    try:
        if set(envelope) != {'version', 'salt', 'nonce', 'ciphertext'} or envelope['version'] != 1:
            raise ValueError()
        salt, nonce, encrypted = [base64.b64decode(envelope[k], validate=True)
                                  for k in ('salt', 'nonce', 'ciphertext')]
        if len(salt) != 16 or len(nonce) != 12:
            raise ValueError()
        aes, _ = live.crypto()
        return live.json_object(aes(key).decrypt(nonce, encrypted, live.AAD))
    except Exception:
        raise live.LiveError('backup_locked', 'Cannot decrypt the switch undo record; password was not changed.') from None


def validate_pending(pending):
    if (not isinstance(pending, dict) or set(pending) != {'version', 'files', 'restart'}
            or pending['version'] != 1 or type(pending['restart']) is not bool
            or not isinstance(pending['files'], dict) or 'vault.json' not in pending['files']
            or set(pending['files']) - set(FILES)):
        raise live.LiveError('rekey_invalid', 'Invalid password-change recovery file; no files changed.')


def verify_files(files, key):
    live.unlock_vault(files['vault.json'], None, key=key)
    if 'renewal.json' in files:
        renewal.records(SimpleNamespace(read=lambda _: files['renewal.json']), key)
    if 'switch-backup.json' in files:
        backup_value(files['switch-backup.json'], key)


def commit(store, files):
    # Journal remains until every fsynced replacement is complete. Ordinary
    # Store readers/writers fail closed while it exists. Retrying is idempotent.
    for name in FILES:
        if name in files:
            store.write(name, files[name], replace=True)
    store.delete(JOURNAL)


def restart(store, data_dir, key, wanted):
    result = {'changed': True, 'running': False,
              'message': 'Vault password changed. Saved credentials, renewal grants, and switch undo history preserved.'}
    if wanted:
        try:
            renewal.start(store, data_dir, key=key)
            result.update(running=True)
            result['message'] += ' Background renewal restarted.'
        except Exception:
            result['background_error'] = True
            result['message'] += ' Background restart failed; run --background start with the new password.'
    return result


def change(store, data_dir):
    if not store.maintenance:
        raise live.LiveError('rekey_mode', 'Password changes require maintenance mode.')
    pending = optional(store, JOURNAL)
    if pending is not None:
        # An interrupted commit always finishes using the NEW password. No old
        # envelope or key is retained in the journal, and no provider is called.
        with renewal.worker_lock(store), renewal.vault_lock(store):
            pending = store.read(JOURNAL)
            validate_pending(pending)
            _, key, _ = live.unlock_vault(pending['files']['vault.json'], live.passphrase())
            verify_files(pending['files'], key)
            commit(store, pending['files'])
            live.remember_unlock(store, pending['files']['vault.json'], key)
        return restart(store, data_dir, key, pending['restart'])

    # Prompt before stopping the worker; a wrong password or cancelled prompt
    # leaves its lifecycle and all encrypted files alone.
    with renewal.vault_lock(store, timeout=40):
        envelope = optional(store, 'vault.json')
        if envelope is None:
            raise live.LiveError('vault_missing', 'No vault exists yet. Capture or enroll an account to create its password.')
        _, old_key, _ = live.unlock_cached(store, envelope)
    new_password = live.passphrase(confirm=True)
    new_salt = os.urandom(16)
    new_key = live.vault_key(new_password, new_salt)
    del new_password
    wanted = renewal.running(store)
    if wanted:
        renewal.stop(store)
    committed = False
    try:
        with renewal.worker_lock(store), renewal.vault_lock(store, timeout=40):
            # The worker may have rotated a grant while we prompted. Read fresh
            # data only after it has stopped and both mutation locks are held.
            envelope = store.read('vault.json')
            entries, _, _ = live.unlock_vault(envelope, None, key=old_key)
            files = {'vault.json': live.encrypt_vault(entries, new_key, new_salt)}
            if optional(store, 'renewal.json') is not None:
                files['renewal.json'] = renewal.seal(renewal.records(store, old_key), new_key)
            backup = optional(store, 'switch-backup.json')
            if backup is not None:
                files['switch-backup.json'] = live.encrypt_vault(backup_value(backup, old_key), new_key, new_salt)
            verify_files(files, new_key)
            pending = {'version': 1, 'files': files, 'restart': wanted}
            from claude_usage import LIMIT
            if len(json.dumps(pending, indent=2).encode()) + 1 > LIMIT:
                raise live.LiveError('rekey_large', 'Encrypted data is too large for safe password-change recovery; no files changed.')
            vault_cache.clear(store.fd, envelope)
            store.write(JOURNAL, pending)
            commit(store, files)
            committed = True
            live.remember_unlock(store, files['vault.json'], new_key)
    except BaseException:
        if optional(store, JOURNAL) is not None:
            raise live.LiveError('rekey_pending', 'Password change interrupted. Run --change-password and enter the NEW password to finish it. Renewal is stopped; encrypted recovery data is preserved.') from None
        # A directory fsync can fail after the final unlink succeeded.
        current = store.read('vault.json')
        is_new = current == pending['files']['vault.json'] if pending else False
        if wanted:
            try:
                renewal.start(store, data_dir, key=new_key if committed or is_new else old_key)
            except Exception:
                message = 'Password changed; use the NEW password.' if is_new else 'Password was not changed.'
                raise live.LiveError('restart_failed', message + ' Background restart failed; run --background start.') from None
        if is_new:
            raise live.LiveError('rekey_finish', 'Encrypted files now use the NEW password, but final cleanup reported an error. Check --background status; no credentials were deleted.') from None
        raise
    import login_keyring
    login_keyring.rekey(store, envelope, files['vault.json'], new_key)
    return restart(store, data_dir, new_key, wanted)
