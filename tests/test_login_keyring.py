"""Opt-in login-keyring autostart with a fake Secret Service; no real keyring."""
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import auth_renewal as renewal
import claude_usage as app
import live_usage as live
import login_keyring as keyring

KEY, OTHER, SALT = b"k" * 32, b"x" * 32, b"s" * 16


class FakeItem:
    def __init__(self, collection, attributes, secret):
        self.collection, self.attributes, self.secret = collection, attributes, secret

    def get_secret(self):
        return self.secret

    def delete(self):
        self.collection.items.remove(self)


class FakeCollection:
    def __init__(self):
        self.items, self.locked = [], False

    def is_locked(self):
        return self.locked

    def search_items(self, attributes):
        return [i for i in self.items if all(i.attributes.get(k) == v for k, v in attributes.items())]

    def create_item(self, label, attributes, secret, replace=False, content_type=None):
        self.items.append(FakeItem(self, dict(attributes), bytes(secret)))


class KeyringTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = app.Store(self.root / "tool", create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.envelope = live.encrypt_vault({}, KEY, SALT)
        self.store.write("vault.json", self.envelope)
        self.collection = FakeCollection()
        module = types.SimpleNamespace(dbus_init=lambda: object(), get_default_collection=lambda c: self.collection)
        patches = [patch.dict(sys.modules, {"secretstorage": module}),
                   patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}),
                   patch.object(live.vault_cache, "get", return_value=KEY),
                   patch.object(live, "passphrase", side_effect=AssertionError("prompted"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_only_verified_key_for_this_vault_is_stored_or_returned(self):
        with self.assertRaises(live.LiveError):
            keyring.put(self.store, self.envelope, OTHER)
        self.assertEqual(self.collection.items, [])
        keyring.put(self.store, self.envelope, KEY)
        self.assertEqual(keyring.get(self.store, self.envelope), KEY)
        self.collection.items[0].secret = OTHER  # tampered or stale
        self.assertIsNone(keyring.get(self.store, self.envelope))
        other_vault = live.encrypt_vault({}, KEY, b"t" * 16)
        self.assertIsNone(keyring.get(self.store, other_vault))

    def test_locked_or_missing_keyring_never_prompts(self):
        keyring.put(self.store, self.envelope, KEY)
        self.collection.locked = True
        self.assertIsNone(keyring.get(self.store, self.envelope))
        result = keyring.start_from_keyring(self.store, self.root / "tool", wait=0)
        self.assertFalse(result["running"])

    def test_enable_writes_login_item_and_disable_removes_key_and_item(self):
        with patch.object(renewal, "ensure_running", return_value=True), patch.object(renewal, "running", return_value=True):
            result = keyring.enable(self.store, self.root / "tool")
        self.assertTrue(result["running"])
        desktop = keyring.autostart_path().read_text()
        self.assertIn("--from-keyring", desktop)
        self.assertIn(str((self.root / "tool").resolve()), desktop)
        self.assertNotIn(KEY.decode(), desktop)
        self.assertEqual(keyring.autostart_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.collection.items), 1)
        keyring.disable()
        self.assertFalse(keyring.autostart_path().exists())
        self.assertEqual(self.collection.items, [])

    def test_login_item_starts_worker_with_stored_key(self):
        keyring.put(self.store, self.envelope, KEY)
        with patch.object(renewal, "running", return_value=False), \
             patch.object(renewal, "start", return_value={"running": True}) as start:
            self.assertTrue(keyring.start_from_keyring(self.store, self.root / "tool", wait=0)["running"])
        start.assert_called_once_with(self.store, self.root / "tool", key=KEY)


if __name__ == "__main__":
    unittest.main()
