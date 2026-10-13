"""No real passwords, account tokens, or system-keyring entries in these tests."""
import contextlib
import ctypes
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import claude_usage as app
import live_usage as live
import vault_cache as cache

KEY = b"k" * 32
SALT = b"s" * 16
PASSWORD = b"fake-cache-test-passphrase"
ENTRY = {"access_token": "FAKE-ACCESS-TOKEN-ONLY", "expires_at": None}


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = app.Store(Path(self.tmp.name) / "data", create=True)
        self.addCleanup(self.store.__exit__, None, None, None)
        self.envelope = live.encrypt_vault({"max-1": ENTRY}, KEY, SALT)
        self.lib = MagicMock()
        self.lib.add_key.return_value = 123
        self.lib.keyctl_search.return_value = 123
        for name in ("keyctl_setperm", "keyctl_set_timeout", "keyctl_link", "keyctl_unlink", "keyctl_revoke"):
            getattr(self.lib, name).return_value = 0
        api = patch.object(cache, "_api", return_value=self.lib)
        api.start()
        self.addCleanup(api.stop)
        now = patch.object(cache, "_now", return_value=100)
        self.now = now.start()
        self.addCleanup(now.stop)

    def cached_payload(self, expires=400, key=KEY):
        payload = struct.pack("!d", expires) + key
        def read(serial, buffer, size):
            ctypes.memmove(buffer, payload, min(size, len(payload)))
            return len(payload)
        self.lib.keyctl_read.side_effect = read

    def test_key_published_only_after_owner_permissions_and_timeout(self):
        self.assertTrue(cache.put(self.store.fd, self.envelope, KEY))
        calls = self.lib.mock_calls
        self.assertEqual([c[0] for c in calls], ["add_key", "keyctl_setperm", "keyctl_set_timeout", "keyctl_link", "keyctl_unlink"])
        args = self.lib.add_key.call_args.args
        self.assertEqual(args[0], b"user")
        self.assertTrue(args[1].startswith(cache.PREFIX))
        self.assertEqual(args[-1], cache.PROCESS_KEYRING)
        payload = ctypes.string_at(args[2], args[3])
        self.assertEqual(payload, struct.pack("!d", 400) + KEY)
        self.assertNotIn(PASSWORD, payload)
        self.lib.keyctl_setperm.assert_called_once_with(123, 0x003F0000)
        self.lib.keyctl_set_timeout.assert_called_once_with(123, 300)
        self.lib.keyctl_link.assert_called_once_with(123, cache.USER_KEYRING)

    def test_cache_hit_never_extends_ttl(self):
        self.cached_payload()
        for when in (100, 200, 399.9):
            self.now.return_value = when
            self.assertEqual(cache.get(self.store.fd, self.envelope), KEY)
        self.lib.keyctl_set_timeout.assert_not_called()
        self.lib.add_key.assert_not_called()

    def test_expired_invalid_or_oversized_payload_rejected(self):
        for expiry in (100, 99, 401, float("nan"), float("inf")):
            self.cached_payload(expiry)
            self.assertIsNone(cache.get(self.store.fd, self.envelope))
        for key in (b"short", KEY + b"extra"):
            self.cached_payload(key=key)
            self.assertIsNone(cache.get(self.store.fd, self.envelope))
        self.lib.keyctl_read.side_effect = None
        self.lib.keyctl_read.return_value = -1
        self.assertIsNone(cache.get(self.store.fd, self.envelope))

    def test_timeout_failure_never_publishes_key(self):
        self.lib.keyctl_set_timeout.return_value = -1
        self.assertFalse(cache.put(self.store.fd, self.envelope, KEY))
        self.lib.keyctl_link.assert_not_called()
        self.lib.keyctl_revoke.assert_called_once_with(123)
        self.lib.keyctl_unlink.assert_called_once_with(123, cache.PROCESS_KEYRING)

    def test_clear_touches_only_exact_app_vault_key(self):
        self.assertTrue(cache.clear(self.store.fd, self.envelope))
        description = cache._description(self.store.fd, self.envelope)
        self.lib.keyctl_search.assert_called_once_with(cache.USER_KEYRING, b"user", description, 0)
        self.lib.keyctl_revoke.assert_called_once_with(123)
        self.lib.keyctl_unlink.assert_called_once_with(123, cache.USER_KEYRING)

    def test_vault_directory_and_salt_bind_the_cache(self):
        original = cache._description(self.store.fd, self.envelope)
        with app.Store(Path(self.tmp.name) / "other", create=True) as other:
            self.assertNotEqual(original, cache._description(other.fd, self.envelope))
        self.assertNotEqual(original, cache._description(self.store.fd, {"salt": "changed"}))
        updated = live.encrypt_vault({"max-2": ENTRY}, KEY, SALT)
        self.assertEqual(original, cache._description(self.store.fd, updated))

    def test_password_once_then_hit_then_expiry_and_wrong_key(self):
        self.store.write("vault.json", self.envelope)
        with patch.object(live, "passphrase", return_value=PASSWORD) as prompt, patch.object(live, "vault_key", return_value=KEY):
            self.lib.keyctl_read.return_value = -1
            self.assertEqual(live.load_vault(self.store), {"max-1": ENTRY})
            self.cached_payload()
            self.assertEqual(live.load_vault(self.store), {"max-1": ENTRY})
            self.assertEqual(prompt.call_count, 1)
            self.now.return_value = 400
            self.assertEqual(live.load_vault(self.store), {"max-1": ENTRY})
            self.assertEqual(prompt.call_count, 2)
            self.cached_payload(expires=700, key=b"x"*32)
            self.assertEqual(live.load_vault(self.store), {"max-1": ENTRY})
            self.assertEqual(prompt.call_count, 3)
        self.lib.keyctl_revoke.assert_called_once_with(123)

    def test_wrong_password_never_cached_and_unavailable_cache_falls_back(self):
        self.store.write("vault.json", self.envelope)
        with patch.object(cache, "_api", side_effect=OSError()), patch.object(live, "passphrase", return_value=PASSWORD), patch.object(live, "vault_key", return_value=b"x"*32), patch.object(cache, "put") as put:
            with self.assertRaises(live.LiveError):
                live.load_vault(self.store)
            put.assert_not_called()
        with patch.object(cache, "_api", side_effect=OSError()), patch.object(live, "passphrase", return_value=PASSWORD) as prompt, patch.object(live, "vault_key", return_value=KEY), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(live.load_vault(self.store), {"max-1": ENTRY})
            self.assertEqual(error.getvalue(), "")
            self.assertEqual(live.load_vault(self.store, verbose=True), {"max-1": ENTRY})
            self.assertEqual(prompt.call_count, 2)
            self.assertIn("OS unlock cache unavailable", error.getvalue())
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name, "data").iterdir()), ["vault.json"])

    def test_captures_reuse_cache_without_restarting_timer(self):
        self.store.write("vault.json", self.envelope)
        self.cached_payload()
        with patch.object(live, "passphrase", side_effect=AssertionError("Unexpected prompt")), patch.object(live, "read_access_token", return_value={**ENTRY, "access_token": "FAKE-OTHER-ACCESS-TOKEN"}):
            live.capture(self.store, "max-2", Path("fake-source"))
            self.assertEqual(len(live.load_vault(self.store)), 2)
        self.lib.add_key.assert_not_called()
        self.lib.keyctl_set_timeout.assert_not_called()

    def test_new_vault_caches_only_after_successful_write(self):
        with patch.object(live, "passphrase", return_value=PASSWORD) as prompt, patch.object(live, "vault_key", return_value=KEY), patch.object(live, "read_access_token", return_value=ENTRY):
            live.capture(self.store, "max-1", Path("fake-source"))
        prompt.assert_called_once_with(confirm=True)
        self.lib.add_key.assert_called_once()
        self.assertTrue(Path(self.tmp.name, "data", "vault.json").exists())
        self.lib.reset_mock()
        with app.Store(Path(self.tmp.name) / "failed", create=True) as failed, patch.object(live, "passphrase", return_value=PASSWORD), patch.object(live, "vault_key", return_value=KEY), patch.object(live, "read_access_token", return_value=ENTRY), patch.object(failed, "write", side_effect=OSError()):
            with self.assertRaises(OSError):
                live.capture(failed, "max-1", Path("fake-source"))
        self.lib.add_key.assert_not_called()

    def test_lock_action_no_password_network_or_background_work(self):
        self.store.write("vault.json", self.envelope)
        with contextlib.ExitStack() as stack:
            for target in ("live_usage.passphrase", "live_usage.load_vault", "socket.socket", "subprocess.Popen", "os.fork", "threading.Thread.start", "time.sleep"):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(app.main(["--data-dir", str(Path(self.tmp.name) / "data"), "--lock", "--json"]), 0)
            self.assertIn("Vault locked", json.loads(stdout.getvalue())["message"])
            self.assertNotIn(KEY.decode(), stdout.getvalue())
        self.lib.keyctl_revoke.assert_called_once_with(123)

    def test_cache_hit_has_no_password_disk_writes_or_background_work(self):
        self.store.write("vault.json", self.envelope)
        self.cached_payload()
        with contextlib.ExitStack() as stack:
            for target in ("live_usage.passphrase", "claude_usage.Store.write", "socket.socket", "subprocess.Popen", "os.fork", "threading.Thread.start", "time.sleep"):
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
            self.assertEqual(live.load_vault(self.store), {"max-1": ENTRY})
