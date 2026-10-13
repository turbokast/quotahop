"""Short, private Claude usage cache shared by reports, switches and panels.

No credentials or raw response bodies on disk. Per-account flock prevents
concurrent requests; HTTP 429 cooldowns survive process exits. Never renews auth.
"""
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
import stat
import time
import uuid

import live_usage as live

TTL = 60
DIRECTORY = "quota-cache"


def private(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        raise live.LiveError("cache_error", "Usage cache permissions are unsafe; no request made.")


@contextmanager
def locked(store, ident):
    import claude_usage as app
    store.check_rekey()
    app.Store.check_name(ident + ".json")
    try:
        os.mkdir(DIRECTORY, 0o700, dir_fd=store.fd)
    except FileExistsError:
        pass
    directory = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store.fd)
    lock = None
    try:
        private(os.fstat(directory), directory=True)
        lock = os.open(ident + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        private(os.fstat(lock))
        try:
            # A bounded wait lets a simultaneous panel/report reuse the result.
            with live.deadline(live.TIMEOUT + 1):
                fcntl.flock(lock, fcntl.LOCK_EX)
        except TimeoutError:
            raise live.LiveError("check_in_progress", "Another usage check is still running. Retry shortly.") from None
        yield directory
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def read(directory, name):
    import claude_usage as app
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as stream:
        private(os.fstat(stream.fileno()))
        value = app.decode(stream.read(app.LIMIT + 1))
    if not isinstance(value, dict):
        raise app.UsageError("Invalid usage cache.")
    return value


def write(directory, name, value):
    import claude_usage as app
    app.reject_secrets(value)
    temp = ".tmp-" + uuid.uuid4().hex
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass


def fetch(store, ident, entry, seen):
    """Return parsed usage, original check time and whether it was reused."""
    import claude_usage as app
    if entry.get("provider", "claude") != "claude":
        raise live.LiveError("provider_mismatch", "Saved credential belongs to a different provider; no request made.")
    live.validate_token(entry.get("access_token"))
    digest = hashlib.sha256(entry["access_token"].encode()).digest()
    if digest in seen:
        raise live.LiveError("duplicate_account", "Duplicate saved token; refusing to label one account as two accounts.")
    seen.add(digest)
    if entry.get("expires_at") is not None and entry["expires_at"] <= time.time():
        raise live.LiveError("authentication_error", "Saved access token has expired. No refresh attempted.")
    with locked(store, ident) as directory:
        name = ident + ".json"
        saved = read(directory, name)
        now = time.time()
        if saved.get("binding") == digest.hex():
            until = app.number(saved.get("retry_at"))
            if until and until > now:
                raise live.LiveError("rate_limited", "Usage endpoint cooldown after HTTP 429; no request made. Subscription quota is unknown, not exhausted.", retry_after=until - now)
            checked = saved.get("checked_at")
            if checked and 0 <= now - app.timestamp(checked).timestamp() < TTL:
                usage = validate_usage(saved.get("usage"))
                # Never reuse a window across its reset boundary.
                windows = [usage[k] for k in ("five_hour", "seven_day")]
                if all(not w["resets_at"] or app.timestamp(w["resets_at"]).timestamp() > now for w in windows):
                    return usage, checked, True
        try:
            raw = live.fetch(entry, set(), "claude")
            if not any(k in raw for k in ("five_hour", "seven_day", "extra_usage")):
                raise app.UsageError("Usage endpoint response does not contain recognized subscription metrics.")
            usage = app.parse_usage(raw)
        except live.LiveError as exc:
            if exc.code == "rate_limited":
                write(directory, name, {"binding": digest.hex(), "retry_at": time.time() + (exc.retry_after or 300)})
            else:
                # A failed request must never resurrect a previous success.
                write(directory, name, {"binding": digest.hex()})
            raise
        checked = dt.datetime.now(app.UTC).isoformat()
        write(directory, name, {"binding": digest.hex(), "checked_at": checked, "usage": usage})
        return usage, checked, False


def validate_usage(value):
    import claude_usage as app
    if not isinstance(value, dict) or not isinstance(value.get("model_windows"), dict):
        raise app.UsageError("Invalid cached usage data.")
    return app.parse_usage({**{k: value.get(k) for k in ("five_hour", "seven_day", "extra_usage")},
                            **value["model_windows"]})
