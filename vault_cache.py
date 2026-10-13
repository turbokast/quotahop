"""Five-minute derived-key cache in the Linux user keyring; no files or daemon.

Only this application's exact, vault-specific key is searched or removed. Keys
are readable by their owning UID, never by group/other users. Same-UID processes
can use the cached unlock while it is valid, just as this CLI can.
"""
import ctypes
import hashlib
import json
import math
import os
import struct
import time

TTL = 300
USER_KEYRING = -4
PROCESS_KEYRING = -2
OWNER_ONLY = 0x003F0000
PREFIX = b"private-claude-usage:vault-unlock:v1:"


def _api():
    lib = ctypes.CDLL("libkeyutils.so.1", use_errno=True)
    serial = ctypes.c_int32
    declarations = {
        "add_key": ([ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, serial], serial),
        "keyctl_search": ([serial, ctypes.c_char_p, ctypes.c_char_p, serial], serial),
        "keyctl_read": ([serial, ctypes.c_void_p, ctypes.c_size_t], ctypes.c_long),
        "keyctl_setperm": ([serial, ctypes.c_uint32], ctypes.c_long),
        "keyctl_set_timeout": ([serial, ctypes.c_uint], ctypes.c_long),
        "keyctl_link": ([serial, serial], ctypes.c_long),
        "keyctl_unlink": ([serial, serial], ctypes.c_long),
        "keyctl_revoke": ([serial], ctypes.c_long),
    }
    for name, (args, result) in declarations.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
    return lib


def _checked(result):
    if result < 0:
        raise OSError(ctypes.get_errno(), "OS unlock cache unavailable")
    return result


def _description(fd, envelope):
    info = os.fstat(fd)
    salt = envelope.get("salt")
    if not isinstance(salt, str):
        raise ValueError("Invalid vault salt")
    identity = json.dumps([os.getuid(), info.st_dev, info.st_ino, salt]).encode()
    return PREFIX + hashlib.sha256(identity).hexdigest().encode()


def _now():
    # Includes suspend time, so suspending the machine cannot extend the TTL.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _find(lib, description):
    serial = lib.keyctl_search(USER_KEYRING, b"user", description, 0)
    if serial < 0 and ctypes.get_errno() in (126, 127, 128):  # ENOKEY / EKEYEXPIRED / EKEYREVOKED
        return None
    return _checked(serial)


def get(fd, envelope):
    """Return a 32-byte key or a cache miss. Never renew a timeout on a hit."""
    try:
        lib = _api()
        serial = _find(lib, _description(fd, envelope))
        if serial is None:
            return None
        payload = ctypes.create_string_buffer(40)
        if _checked(lib.keyctl_read(serial, payload, 40)) != 40:
            return None
        expires = struct.unpack("!d", payload.raw[:8])[0]
        remaining = expires - _now()
        if not math.isfinite(remaining) or not 0 < remaining <= TTL:
            return None
        return payload.raw[8:]
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def put(fd, envelope, key):
    """Publish only after timeout/permissions are set. Never cache a password."""
    serial = None
    linked = False
    try:
        if not isinstance(key, bytes) or len(key) != 32:
            return False
        lib = _api()
        description = _description(fd, envelope)
        payload = struct.pack("!d", _now() + TTL) + key
        buffer = ctypes.create_string_buffer(payload)
        # A crash before publishing leaves the key only in this process's ring,
        # which dies with the process. Never publish an unexpiring secret.
        serial = _checked(lib.add_key(b"user", description, buffer, len(payload), PROCESS_KEYRING))
        _checked(lib.keyctl_setperm(serial, OWNER_ONLY))
        _checked(lib.keyctl_set_timeout(serial, TTL))
        _checked(lib.keyctl_link(serial, USER_KEYRING))
        linked = True
        return True
    except (OSError, AttributeError, ValueError, TypeError):
        return False
    finally:
        if serial is not None:
            if not linked:
                lib.keyctl_revoke(serial)
            lib.keyctl_unlink(serial, PROCESS_KEYRING)


def clear(fd, envelope):
    """Revoke/unlink only this vault's cached key; never clear a shared ring."""
    try:
        lib = _api()
        serial = _find(lib, _description(fd, envelope))
        if serial is not None:
            _checked(lib.keyctl_revoke(serial))
            _checked(lib.keyctl_unlink(serial, USER_KEYRING))
        return True
    except (OSError, AttributeError, ValueError, TypeError):
        return False
