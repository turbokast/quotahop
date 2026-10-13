"""Explicit Claude Code subscription switch; no login/logout/refresh machinery.

Only claudeAiOauth and oauthAccount may change. Design/MCP authentication and
all other JSON text are preserved. Only explicit switch actions call switch().
"""
import base64
from contextlib import contextmanager, ExitStack
import fcntl
import json
import os
from pathlib import Path
import stat
import time
import uuid

import account_identity
import live_usage as live

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
BACKUP = "switch-backup.json"
NOTICE = "Explicit shared-auth switch using Claude Code's local file format and read-only profile endpoint; neither is a documented public switching API."


class ProfileClient(live.UsageClient):
    # Deliberately separate from the normal usage client's endpoint allowlist.
    endpoints = frozenset({("GET", PROFILE_URL)})
    routes = {PROFILE_URL: ("api.anthropic.com", "/api/oauth/profile")}


def fail(message):
    raise live.LiveError("switch_refused", message)


def paths():
    """Read existing path overrides; never change environment or create links."""
    home = Path.home()
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
    secure_dir = Path(os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(config_dir)) or home / ".claude").expanduser()
    if (config_dir / ".config.json").exists():
        fail("Legacy Claude configuration layout is unsupported for switching; no authentication changed.")
    metadata = config_dir / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    return Path(os.path.abspath(secure_dir / ".credentials.json")), Path(os.path.abspath(metadata))


def check_environment():
    overrides = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
                 "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                 "ANTHROPIC_PROFILE", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_ACCOUNT_UUID",
                 "CLAUDE_CODE_ORGANIZATION_UUID", "CLAUDE_CODE_USER_EMAIL")
    if any(os.environ.get(key) for key in overrides):
        fail("An authentication/provider override is set in this command's environment. Switching the shared file would not reliably select the subscription. No environment variables changed.")


def select(accounts, entries, selector, plan):
    from claude_usage import clean_text, reject_secrets
    selector = clean_text(selector)
    reject_secrets(selector)
    if plan not in (None, "max_20x", "team_premium"):
        fail("--switch supports Claude subscriptions only; Codex and Muse authentication is never changed.")
    candidates = [a for a in accounts if plan is None or a["plan"] == plan]
    matches = [a for a in candidates if a["id"] == selector] or [a for a in candidates if selector.casefold() in [
        a["name"].casefold(), *[v.casefold() for v in account_identity.saved(entries.get(a["id"], {})).values()]]]
    if len(matches) != 1:
        fail("Select one saved account by name/email or ID. For a shared email, add --plan max_20x or --plan team_premium.")
    account = matches[0]
    entry = entries.get(account["id"])
    if account["plan"] not in ("max_20x", "team_premium"):
        fail("Only Claude Code subscriptions can be switched.")
    if not entry or entry.get("provider", "claude") != "claude":
        fail("Capture this Claude subscription first; no saved Claude access token is available.")
    expiry = entry.get("expires_at")
    if expiry is None or expiry <= time.time() + 60:
        fail("Saved token has expired, is near expiry, or has no known expiry. Recapture after your normal sign-in; no refresh attempted.")
    if "scopes" in entry and "user:inference" not in entry["scopes"]:
        fail("Saved token's scope metadata does not include Claude inference access; no authentication changed.")
    return account, entry


def read_profile(entry):
    from claude_usage import reject_secrets
    token = live.validate_token(entry["access_token"])
    profile = ProfileClient().request("GET", PROFILE_URL, token)
    if token in json.dumps(profile):
        fail("Credential-like content in profile response; content suppressed.")
    reject_secrets(profile)
    return profile


def target_auth(account, entry, *, profile=None):
    token = live.validate_token(entry["access_token"])
    profile = read_profile(entry) if profile is None else profile
    user, org = profile.get("account"), profile.get("organization")
    if not isinstance(user, dict) or not isinstance(org, dict):
        fail("Profile response has no account/organization identity; no authentication changed.")
    identity = account_identity.extract(user.get("email"), user.get("display_name"))
    if not identity.get("email"):
        fail("Profile response has no valid email; no authentication changed.")
    old_email = account_identity.saved(entry).get("email")
    if old_email and old_email.casefold() != identity["email"].casefold():
        fail("Saved label and token profile belong to different accounts; recapture the intended account.")
    expected = {"max_20x": "claude_max", "team_premium": "claude_team"}[account["plan"]]
    if org.get("organization_type") != expected:
        fail("Token's subscription type does not match the selected Max/Team row. Recapture it with the correct --plan.")
    for value in (user.get("uuid"), org.get("uuid")):
        try:
            uuid.UUID(value)
        except (ValueError, TypeError, AttributeError):
            fail("Profile response has an invalid account/organization ID; no authentication changed.")
    if entry.get("claude_identity") is not None and entry["claude_identity"] != {
            "account_uuid": user["uuid"], "organization_uuid": org["uuid"]}:
        fail("Token profile differs from the saved account/organization binding; no authentication changed.")
    metadata = {"accountUuid": user["uuid"], "organizationUuid": org["uuid"],
                "emailAddress": identity["email"], "profileFetchedAt": int(time.time() * 1000)}
    mappings = ((user, {"display_name": "displayName", "full_name": "fullName", "created_at": "accountCreatedAt"}),
                (org, {"name": "organizationName", "organization_type": "organizationType",
                       "rate_limit_tier": "organizationRateLimitTier", "seat_tier": "seatTier",
                       "has_extra_usage_enabled": "hasExtraUsageEnabled", "billing_type": "billingType",
                       "subscription_created_at": "subscriptionCreatedAt", "cc_onboarding_flags": "ccOnboardingFlags",
                       "claude_code_trial_ends_at": "claudeCodeTrialEndsAt",
                       "claude_code_trial_duration_days": "claudeCodeTrialDurationDays"}))
    for source, mapping in mappings:
        for source_key, target_key in mapping.items():
            if source_key in source:
                metadata[target_key] = source[source_key]
    # This is an access-only installation. Never retain the outgoing account's
    # refresh token. null also prevents the CLI's old-token CAS refresh save
    # from treating the new credential as an empty refresh-token placeholder.
    oauth = {"accessToken": token, "refreshToken": None, "expiresAt": int(entry["expires_at"] * 1000),
             "scopes": entry.get("scopes", ["user:inference", "user:profile"]),
             "subscriptionType": "max" if account["plan"] == "max_20x" else "team",
             "rateLimitTier": org.get("rate_limit_tier")}
    return oauth, metadata


def replace_value(raw, key, value):
    """Replace just one existing top-level JSON value, preserving all other bytes."""
    live.json_object(raw)  # Reject duplicate keys and malformed/oversized JSON.
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    pos = text.index("{") + 1
    while True:
        while text[pos].isspace() or text[pos] == ",":
            pos += 1
        if text[pos] == "}":
            fail("Required Claude authentication field is missing; no configuration is synthesized.")
        name, pos = decoder.raw_decode(text, pos)
        while text[pos].isspace():
            pos += 1
        pos += 1  # Colon; full JSON already validated.
        while text[pos].isspace():
            pos += 1
        start = pos
        _, pos = decoder.raw_decode(text, pos)
        if name == key:
            return (text[:start] + json.dumps(value, ensure_ascii=False, allow_nan=False) + text[pos:]).encode()


class JsonFile:
    def __init__(self, path, private=False):
        self.path = Path(os.path.abspath(path))
        self.private = private
        self.fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in self.path.parent.parts[1:]:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                os.close(self.fd)
                self.fd = child
            if os.fstat(self.fd).st_uid != os.getuid():
                fail("Claude authentication directory is not owned by you.")
            self.read()
        except BaseException:
            self.close()
            raise

    def close(self):
        os.close(self.fd)

    def read(self):
        fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                    or (self.private and stat.S_IMODE(info.st_mode) != 0o600)):
                fail("Claude auth files must be owned regular files without links; credentials must be 0600.")
            raw = stream.read(live.MAX_BYTES + 1)
        live.json_object(raw)
        return raw, info

    def write(self, expected, payload, guard=lambda: None):
        guard()
        current, info = self.read()
        if current != expected:
            fail("Claude auth/config changed concurrently; switch refused to protect those changes.")
        temp = ".cc-usage-" + uuid.uuid4().hex
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchown(stream.fileno(), -1, info.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if self.read()[0] != expected:
                fail("Claude auth/config changed concurrently; no stale write performed.")
            guard()
            os.replace(temp, self.path.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass


@contextmanager
def locks(credentials, metadata):
    # Same mkdir-based locks used by Claude Code 2.1.296. Never remove somebody
    # else's lock or attempt stale-lock recovery. Hold only during local writes.
    acquired = []
    started = time.monotonic()

    def guard():
        if time.monotonic() - started >= 5:
            fail("Claude authentication write locks have been held too long; refusing a potentially stale write.")
        for file, name, inode in acquired:
            if os.stat(name, dir_fd=file.fd, follow_symlinks=False).st_ino != inode:
                fail("Claude authentication write lock changed; no stale write performed.")
    try:
        for file, name in ((credentials, ".storage-write.lock"), (metadata, metadata.path.name + ".lock")):
            try:
                os.mkdir(name, 0o700, dir_fd=file.fd)
            except FileExistsError:
                fail("Claude is writing its credentials/configuration; retry the switch when it finishes.")
            acquired.append((file, name, os.stat(name, dir_fd=file.fd, follow_symlinks=False).st_ino))
        yield guard
    finally:
        for file, name, inode in reversed(acquired):
            try:
                if os.stat(name, dir_fd=file.fd, follow_symlinks=False).st_ino == inode:
                    os.rmdir(name, dir_fd=file.fd)
            except FileNotFoundError:
                pass


def apply(credentials, metadata, oauth, account, before_creds, before_meta, guard=lambda: None):
    updated_creds = replace_value(before_creds, "claudeAiOauth", oauth)
    updated_meta = replace_value(before_meta, "oauthAccount", account)
    try:
        credentials.write(before_creds, updated_creds, guard)
        metadata.write(before_meta, updated_meta, guard)
    except BaseException:
        # Roll back only our two fields, and only while they still equal the
        # values we installed. Preserve any unrelated concurrent changes.
        try:
            for file, key, installed, before, after in ((metadata, "oauthAccount", account, before_meta, updated_meta),
                                                      (credentials, "claudeAiOauth", oauth, before_creds, updated_creds)):
                current = file.read()[0]
                obj = live.json_object(current)
                if obj.get(key) == installed:
                    restored = before if current == after else replace_value(current, key, live.json_object(before)[key])
                    file.write(current, restored, guard)
        except BaseException:
            fail("Switch failed and automatic rollback could not finish. The encrypted switch-backup.json retains the previous Claude auth. Use --undo-switch; unrelated authentication fields were not replaced.")
        raise


def recommended_account(store, accounts, entries, plan=None, jobs=4):
    """One live usage cycle; same ranking as the table, usable Claude rows only."""
    import datetime as dt
    from claude_usage import UTC, concurrent_report, recommendation, sort_report
    if plan not in (None, "max_20x", "team_premium"):
        fail("Automatic switching supports Claude subscriptions only.")
    candidates = []
    for account in accounts:
        if account["plan"] not in ("max_20x", "team_premium") or (plan and account["plan"] != plan):
            continue
        try:
            select([account], entries, account["id"], plan)
        except live.LiveError:
            continue
        candidates.append(account)
    now = dt.datetime.now(UTC)
    data = concurrent_report(candidates, store, now, entries, jobs)
    now = dt.datetime.now(UTC)
    for row in sort_report(data, now=now)["accounts"]:
        if recommendation(row, now)[0] >= 2:
            continue
        # Expiry can pass during the network checks.
        try:
            select(candidates, entries, row["id"], plan)
        except live.LiveError:
            continue
        return row["id"]
    fail("No switchable Claude subscription has live available usage and a token valid for more than one minute. Check --verbose, or recapture expired accounts. No Claude authentication changed.")


def switch(store, accounts, selector=None, plan=None, *, dry_run=False, undo=False, automatic=False, jobs=4,
           vault_key=None, expected_current=None, expected_target=None, require_available=False,
           authorize=None):
    check_environment()
    if automatic and plan not in (None, "max_20x", "team_premium"):
        fail("Automatic switching supports Claude subscriptions only.")
    try:
        fcntl.flock(store.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fail("Another capture/switch is active; retry when it finishes.")
    try:
        envelope = store.read("vault.json")
        entries, key, salt = (live.unlock_vault(envelope, None, key=vault_key) if vault_key is not None
                              else live.unlock_cached(store, envelope))
        if automatic:
            if not dry_run:
                import saved_refresh
                entries, _ = saved_refresh.synchronize(store, accounts, entries, key, salt)
            selector = recommended_account(store, accounts, entries, plan, jobs)
        credential_path, metadata_path = paths()
        backup = None
        if undo:
            try:
                backup_envelope = store.read(BACKUP)
                AESGCM, _ = live.crypto()
                backup = live.json_object(AESGCM(key).decrypt(base64.b64decode(backup_envelope["nonce"]),
                    base64.b64decode(backup_envelope["ciphertext"]), live.AAD))
                if backup["paths"] != [str(credential_path.absolute()), str(metadata_path.absolute())]:
                    fail("Undo record belongs to different Claude configuration paths.")
                oauth, account = backup["claudeAiOauth"], backup["oauthAccount"]
                if not isinstance(oauth, dict) or not isinstance(account, dict):
                    raise ValueError()
            except live.LiveError:
                raise
            except Exception:
                fail("No readable encrypted switch undo record is available.")
            label = account_identity.extract(email=account.get("emailAddress")).get("email", "previous Claude account")
        else:
            selected, entry = select(accounts, entries, selector, plan)
            if expected_target is not None:
                from credential_state import fingerprint
                if fingerprint(entry, "claude") != expected_target:
                    fail("The target credential changed. Refresh and confirm the switch again.")
            if require_available:
                import datetime as dt
                from claude_usage import UTC, report, recommendation
                usage = report([selected], store, dt.datetime.now(UTC), entries)["accounts"][0]
                if recommendation(usage, dt.datetime.now(UTC))[0] >= 2:
                    fail("The confirmed subscription no longer has verified available usage. Refresh and choose again.")
            oauth, account = target_auth(selected, entry)
            label = account["emailAddress"]
        selection = {} if undo else {"account_id": selected["id"], "plan": selected["plan"],
                                    "selection": "recommended" if automatic else "explicit"}
        with ExitStack() as stack:
            credentials = JsonFile(credential_path, private=True)
            stack.callback(credentials.close)
            metadata = JsonFile(metadata_path)
            stack.callback(metadata.close)
            guard = stack.enter_context(locks(credentials, metadata)) if not dry_run else lambda: None
            before_creds, before_meta = credentials.read()[0], metadata.read()[0]
            current = live.json_object(before_creds)
            config = live.json_object(before_meta)
            if not isinstance(current.get("claudeAiOauth"), dict) or not isinstance(config.get("oauthAccount"), dict):
                fail("Existing Claude authentication fields are missing; switch refused.")
            if current.get("enterpriseGateway") or config.get("primaryApiKey"):
                fail("Gateway or Console API authentication is also configured; switching would not reliably select a Claude subscription.")
            if expected_current is not None:
                from credential_state import fingerprint
                current_token = current["claudeAiOauth"].get("accessToken")
                if not isinstance(current_token, str) or fingerprint({"access_token": current_token}, "claude") != expected_current:
                    fail("The current Claude login changed while confirming. Refresh and choose again; no auth changed.")
            if undo and current["claudeAiOauth"].get("accessToken") != backup["installed_token"]:
                fail("Claude authentication changed since the switch; undo refused to avoid overwriting it.")
            if not undo and current["claudeAiOauth"].get("accessToken") == oauth["accessToken"]:
                return {"message": "This Claude access token is already selected; no files changed.", "account_name": label, "changed": False, **selection}
            # Preflight replacement before storing backup or touching live files.
            replace_value(before_creds, "claudeAiOauth", oauth)
            replace_value(before_meta, "oauthAccount", account)
            if dry_run:
                return {"message": "Dry run: would replace only claudeAiOauth and oauthAccount; no Claude files changed.",
                        "account_name": label, "changed": False, **selection}
            if authorize is not None and not authorize():
                fail("Panel was locked before the switch. No authentication changed.")
            if not undo:
                backup = {"paths": [str(credentials.path), str(metadata.path)], "installed_token": oauth["accessToken"],
                          "claudeAiOauth": current["claudeAiOauth"], "oauthAccount": config["oauthAccount"]}
                store.write(BACKUP, live.encrypt_vault(backup, key, salt), replace=True)
            apply(credentials, metadata, oauth, account, before_creds, before_meta, guard)
            if undo:
                store.delete(BACKUP)
        return {"message": ("Restored the previous Claude Code authentication." if undo else
                "Switched shared Claude Code subscription. Background renewal updates enrolled vault credentials; "
                "this global access token is not updated automatically. Re-run --switch or --switch-next to apply "
                "a renewed token. Existing sessions may need to reload their credentials."),
                "account_name": label, "changed": True, **selection}
    except OSError:
        fail("Cannot read/write the Claude authentication files safely. Check access and permissions; no login or refresh was attempted.")
    finally:
        fcntl.flock(store.fd, fcntl.LOCK_UN)
