"""Opt-in: keep the vault key in the desktop login keyring for renewal at login.

Trade-off (see USAGE.md): the key becomes readable by processes running as you
while you are logged in, and at rest it is protected by your login password via
the keyring instead of the vault passphrase. Processes running as you can
already read Claude Code's and Codex's active logins in plaintext, so this
mainly removes the need to unlock after every reboot.

The secret travels over an encrypted Secret Service session, never argv/env.
Never prompts: a locked or missing keyring simply yields no key.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

import live_usage as live

LABEL = "QuotaHop vault key (background renewal)"
APP = "quotahop"
DESKTOP = "quotahop-renewal.desktop"


def _vault_id(store, envelope):
    info = os.fstat(store.fd)
    salt = envelope.get("salt")
    if not isinstance(salt, str):
        raise live.LiveError("invalid_vault", "Invalid vault salt.")
    return hashlib.sha256(json.dumps([os.getuid(), info.st_dev, info.st_ino, salt]).encode()).hexdigest()


def _collection():
    try:
        import secretstorage
    except ImportError:
        raise live.LiveError("dependency_missing", "Install requirements.txt into .venv to use the login keyring.") from None
    try:
        connection = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(connection)
    except Exception:
        raise live.LiveError("keyring_unavailable", "Desktop login keyring (Secret Service) is unavailable.") from None
    if collection.is_locked():
        raise live.LiveError("keyring_locked", "Desktop login keyring is locked; log in to the desktop session first.")
    return collection


def get(store, envelope):
    """The stored key for this exact vault, verified against it, or None."""
    try:
        items = list(_collection().search_items({"application": APP, "vault": _vault_id(store, envelope)}))
        for item in items:
            key = bytes(item.get_secret())
            try:
                live.unlock_vault(envelope, None, key=key)
                return key
            except live.LiveError:
                continue
    except live.LiveError:
        return None
    except Exception:
        return None
    return None


def put(store, envelope, key):
    live.unlock_vault(envelope, None, key=key)  # never store an unverified key
    collection = _collection()
    vault = _vault_id(store, envelope)
    remove(collection)
    collection.create_item(LABEL, {"application": APP, "vault": vault}, key, replace=True, content_type="application/octet-stream")


def remove(collection=None):
    try:
        collection = collection or _collection()
        for item in list(collection.search_items({"application": APP})):
            item.delete()
        return True
    except live.LiveError:
        return False


def enabled():
    return autostart_path().exists()


def autostart_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / DESKTOP


def command(data_dir):
    script = Path(__file__).resolve().parent / "quotahop"
    return [sys.executable, "-B", str(script), "--data-dir", str(Path(data_dir).resolve()),
            "--background", "start", "--from-keyring"]


def write_autostart(data_dir):
    def quote(arg):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("[Desktop Entry]\nType=Application\nName=QuotaHop renewal\n"
                    "Comment=Restart QuotaHop background login renewal after you log in\n"
                    f"Exec={' '.join(quote(a) for a in command(data_dir))}\n"
                    "NoDisplay=true\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def enable(store, data_dir):
    import auth_renewal
    envelope = store.read("vault.json")
    _, key, _ = live.unlock_cached(store, envelope)
    put(store, envelope, key)
    write_autostart(data_dir)
    started = auth_renewal.ensure_running(store, key)
    return {"message": "Autostart enabled: the vault key is in your login keyring and renewal starts when you log in."
            + (" Background renewal started now." if started else ""), "running": auth_renewal.running(store)}


def disable():
    removed = remove()
    try:
        autostart_path().unlink()
    except FileNotFoundError:
        pass
    return {"message": "Autostart disabled: login item and stored vault key removed." if removed else
            "Autostart login item removed. The keyring was unavailable; check it for a 'QuotaHop vault key' item."}


def start_from_keyring(store, data_dir, wait=60):
    """Login-item entry point: never prompts; quietly does nothing without a key."""
    import auth_renewal
    envelope = store.read("vault.json")
    end = time.monotonic() + wait
    key = get(store, envelope)
    while key is None and time.monotonic() < end:
        time.sleep(2)  # The keyring may unlock moments after the session starts.
        key = get(store, envelope)
    if key is None:
        return {"message": "No usable vault key in the login keyring; renewal not started. Run quotahop --background enable-autostart.", "running": False}
    if auth_renewal.running(store):
        return {"message": "Background renewal already running.", "running": True}
    return auth_renewal.start(store, data_dir, key=key)


def rekey(store, old_envelope, new_envelope, key):
    """After a password change, keep autostart working with the new key."""
    if not enabled():
        return
    try:
        put(store, new_envelope, key)
    except Exception:
        pass
