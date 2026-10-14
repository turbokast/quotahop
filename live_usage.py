"""One GET per saved access token. No OAuth client, login, refresh, or SDK."""
import base64
import datetime as dt
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
import getpass
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import ssl
import stat
import time
import warnings
import sys

import vault_cache
import account_identity

HOST = "api.anthropic.com"
PATH = "/api/oauth/usage"
URL = "https://" + HOST + PATH
CODEX_HOST = "chatgpt.com"
CODEX_PATH = "/backend-api/wham/usage"
CODEX_URL = "https://" + CODEX_HOST + CODEX_PATH
READ_ONLY_ENDPOINTS = frozenset({("GET", URL), ("GET", CODEX_URL)})
MAX_BYTES = 1024 * 1024
TIMEOUT = 8
AAD = b"private-claude-usage-vault-v1"
NOTICE = "Uses Anthropic's undocumented subscription usage endpoint; not an officially supported integration."
CODEX_NOTICE = "Uses Codex's first-party quota endpoint; not a documented public HTTP API."
MUSE_NOTICE = "Muse Code uses a separately captured Meta portal session and selected subscription team; CLI credential files are not supported."


class LiveError(Exception):
    def __init__(self, code, message, *, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def retry_delay(value):
    """Only a bounded numeric delay crosses the network boundary, never header text."""
    try:
        if not isinstance(value, str) or len(value) > 128:
            return 300
        seconds = int(value) if value.isdecimal() else (
            parsedate_to_datetime(value) - dt.datetime.now(dt.timezone.utc)).total_seconds()
        return max(300, min(86400, math.ceil(seconds)))
    except (ValueError, TypeError, OverflowError):
        return 300


def json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        if len(raw) > MAX_BYTES:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise LiveError("invalid_data", "Invalid or oversized JSON data; content suppressed.") from None


def validate_token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{10,8192}", value):
        raise LiveError("invalid_credentials", "No valid OAuth access token found.")
    return value


def validate_scopes(value):
    if (not isinstance(value, list) or len(value) > 100 or any(
            not isinstance(scope, str) or not re.fullmatch(r"[a-z][a-z0-9_:.-]{0,100}", scope) for scope in value)):
        raise LiveError("invalid_credentials", "Invalid Claude scope metadata; content suppressed.")
    return value


def read_access_token(path, provider="claude", *, include_identity=True):
    """O_RDONLY plus O_NOATIME: neither contents nor access time are changed.

    Parse only a file explicitly selected by --capture; never search other files,
    invoke credential helpers, or use the refreshToken field.
    """
    if provider not in ("claude", "codex"):
        raise LiveError("unsupported", MUSE_NOTICE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOATIME)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                raise LiveError("unsafe_credentials", "Credential source must be an owned private regular file without links.")
            source = json_object(stream.read(MAX_BYTES + 1))
        if provider == "codex":
            if source.get("auth_mode") not in (None, "chatgpt", "chatgptAuthTokens"):
                raise LiveError("invalid_credentials", "Codex is not configured for a ChatGPT subscription login.")
            oauth = source.get("tokens")
            if not isinstance(oauth, dict):
                raise LiveError("invalid_credentials", "No ChatGPT subscription access token in this file. API keys and keyring-only logins are unsupported.")
            token = validate_token(oauth.get("access_token"))
            account_id = validate_account_id(oauth.get("account_id"))
            # JWT expiry is only a local preflight hint, never identity verification.
            expiry = None
            if token.count(".") == 2:
                try:
                    part = token.split(".")[1]
                    claims = json_object(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                    expiry = claims.get("exp")
                    if expiry is not None and (type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0):
                        raise ValueError()
                except (ValueError, LiveError, OverflowError):
                    raise LiveError("invalid_credentials", "Invalid Codex access-token expiry metadata.") from None
            entry = {"provider": "codex", "access_token": token, "expires_at": expiry, "account_id": account_id}
            identity = account_identity.codex(token) if include_identity else {}
            if identity:
                entry["identity"] = identity
            return entry
        oauth = source.get("claudeAiOauth", source)
        if not isinstance(oauth, dict):
            raise LiveError("invalid_credentials", "No Claude OAuth credentials found.")
        token = validate_token(oauth.get("accessToken", oauth.get("access_token")))
        expiry = oauth.get("expiresAt")  # Claude Code uses epoch milliseconds.
        if expiry is not None:
            try:
                valid = type(expiry) in (int, float) and math.isfinite(expiry) and expiry > 0
            except OverflowError:
                valid = False
            if not valid:
                raise LiveError("invalid_credentials", "Invalid access-token expiry metadata.")
            expiry /= 1000
        entry = {"access_token": token, "expires_at": expiry}
        if "scopes" in oauth:
            entry["scopes"] = validate_scopes(oauth["scopes"])
        identity = account_identity.claude(path, token) if include_identity else {}
        if identity:
            entry["identity"] = identity
        return entry
    except OSError:
        raise LiveError("credential_read_error", "Cannot read credential source safely; no files were changed.") from None


def validate_account_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise LiveError("invalid_credentials", "Missing or invalid Codex account routing ID.")
    return value


def crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        return AESGCM, Scrypt
    except ImportError:
        raise LiveError("dependency_missing", "Install requirements.txt into .venv to use the encrypted vault.") from None


def passphrase(confirm=False):
    # Never fall back to echoed stdin (e.g. a redirected pipeline).
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        os.close(fd)
    except OSError:
        raise LiveError("tty_required", "A terminal is required to unlock the vault; never pass a password in arguments.") from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            prompt = "Create vault passphrase (12+ characters): " if confirm else "Unlock vault — passphrase: "
            phrase = getpass.getpass(prompt)
            if not phrase:
                raise LiveError("vault_locked", "An empty vault passphrase is not allowed.")
            if confirm and (len(phrase) < 12 or phrase != getpass.getpass("Confirm passphrase (12+ characters): ")):
                raise LiveError("vault_locked", "Passphrases must match and contain at least 12 characters.")
    except (getpass.GetPassWarning, EOFError):
        raise LiveError("tty_required", "Cannot read a hidden passphrase from this terminal; no echoed-input fallback.") from None
    return phrase.encode("utf-8")


def vault_key(password, salt):
    _, Scrypt = crypto()
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password)


def unlock_vault(envelope, password, *, key=None):
    AESGCM, _ = crypto()
    try:
        if set(envelope) != {"version", "salt", "nonce", "ciphertext"} or envelope["version"] != 1:
            raise ValueError()
        salt, nonce, encrypted = [base64.b64decode(envelope[key], validate=True)
                                  for key in ("salt", "nonce", "ciphertext")]
        if len(salt) != 16 or len(nonce) != 12 or not 16 <= len(encrypted) <= MAX_BYTES:
            raise ValueError()
        if key is None:
            key = vault_key(password, salt)
        plaintext = AESGCM(key).decrypt(nonce, encrypted, AAD)
        entries = json_object(plaintext)
        for ident, entry in entries.items():
            if (not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", ident) or not isinstance(entry, dict)
                    or entry.get("provider", "claude") not in ("claude", "codex", "muse_code")):
                raise ValueError()
            if entry.get("provider") == "muse_code":
                import muse_usage
                if entry.get("kind") == "cli_owned":
                    import owned_muse
                    owned_muse.validate_entry(entry)
                else:
                    muse_usage.validate_entry(entry)
                continue
            expected = {"access_token", "expires_at"}
            if "claude_identity" in entry and entry.get("provider", "claude") == "claude":
                import saved_refresh
                saved_refresh.validate_binding(entry["claude_identity"])
                expected.add("claude_identity")
            if "scopes" in entry and entry.get("provider", "claude") == "claude":
                validate_scopes(entry["scopes"])
                expected.add("scopes")
            if "identity" in entry:
                account_identity.validate(entry["identity"])
                expected.add("identity")
            if entry.get("provider") == "codex":
                expected |= {"provider", "account_id"}
                validate_account_id(entry.get("account_id"))
            if set(entry) != expected:
                raise ValueError()
            validate_token(entry["access_token"])
            expiry = entry["expires_at"]
            if expiry is not None and (type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0):
                raise ValueError()
        return entries, key, salt
    except Exception:
        raise LiveError("vault_locked", "Cannot unlock vault: incorrect passphrase or damaged vault.") from None


def encrypt_vault(entries, key, salt):
    AESGCM, _ = crypto()
    nonce = os.urandom(12)
    raw = json.dumps(entries, allow_nan=False).encode()
    encrypted = AESGCM(key).encrypt(nonce, raw, AAD)
    return {"version": 1, "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(encrypted).decode()}


def load_vault(store, *, verbose=False, accounts=None):
    try:
        envelope = store.read("vault.json")
    except FileNotFoundError:
        return {}
    entries, key, _ = unlock_cached(store, envelope, verbose=verbose)
    if accounts is not None:
        import saved_refresh
        entries = saved_refresh.follow_cli(store, accounts, entries, key)
    return entries


def remember_unlock(store, envelope, key, *, verbose=False):
    if not vault_cache.put(store.fd, envelope, key) and verbose:
        print("OS unlock cache unavailable; this run is unlocked, but the next command will ask again.", file=sys.stderr)


def unlock_cached(store, envelope, *, verbose=False):
    key = vault_cache.get(store.fd, envelope)
    if key is not None:
        try:
            return unlock_vault(envelope, None, key=key)
        except LiveError:
            vault_cache.clear(store.fd, envelope)
    result = unlock_vault(envelope, passphrase())
    remember_unlock(store, envelope, result[1], verbose=verbose)
    return result


def lock_vault(store):
    try:
        envelope = store.read("vault.json")
    except FileNotFoundError:
        return
    if not vault_cache.clear(store.fd, envelope):
        raise LiveError("cache_unavailable", "Cannot clear the OS unlock cache; any existing unlock still expires after five minutes.")


def remove_account(store, selector, *, plan=None, verbose=False):
    """Delete this tool's entry only. No provider requests or credential-source reads."""
    import fcntl
    from claude_usage import UsageError, clean_text, reject_secrets, validate_accounts
    selector = clean_text(selector)
    reject_secrets(selector)
    try:
        fcntl.flock(store.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise LiveError("vault_busy", "Another capture or removal is active; try again when it finishes.") from None
    try:
        accounts = validate_accounts(store.read("accounts.json"))
        try:
            envelope = store.read("vault.json")
        except FileNotFoundError:
            envelope, entries = None, {}
        else:
            entries, key, salt = unlock_cached(store, envelope, verbose=verbose)
        candidates = [a for a in accounts if plan is None or a["plan"] == plan]
        exact = [a for a in candidates if a["id"] == selector]
        matches = exact or [a for a in candidates if selector.casefold() in [
            a["name"].casefold(), *[v.casefold() for v in account_identity.saved(entries.get(a["id"], {})).values()]]]
        if not matches:
            raise UsageError("No saved account matches. Use its configured name or ID from --json.")
        if len(matches) != 1:
            raise UsageError("Account name/email is ambiguous; specify --plan or use its unique ID from --json.")
        account = matches[0]
        ident = account["id"]
        # Purge credentials before hiding the row. Failures leave the row available
        # for a retry by ID, rather than silently orphaning a saved credential.
        if envelope is not None:
            import auth_renewal
            auth_renewal.forget_locked(store, key, ident)
        if ident in entries:
            del entries[ident]
            store.write("vault.json", encrypt_vault(entries, key, salt), replace=True)
        store.delete(ident + ".json")
        store.write("accounts.json", {"accounts": [a for a in accounts if a["id"] != ident]}, replace=True)
        return account
    finally:
        fcntl.flock(store.fd, fcntl.LOCK_UN)


def capture(store, ident, source, provider="claude", muse_team=None, *, named=False, plan=None, name=None):
    import fcntl
    try:
        fcntl.flock(store.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise LiveError("vault_busy", "Another capture is active; try again when it finishes.") from None
    try:
        # Read-only capture never contacts Anthropic, even to test authentication.
        if provider == "muse_code":
            import muse_usage
            entry = muse_usage.prepare_capture(muse_team)
        else:
            entry = read_access_token(source, provider)
        try:
            envelope = store.read("vault.json")
        except FileNotFoundError:
            salt = os.urandom(16)
            key = vault_key(passphrase(confirm=True), salt)
            entries = {}
            newly_created = True
        else:
            entries, key, salt = unlock_cached(store, envelope)
            newly_created = False
        selected = updated = None
        if named:
            from claude_usage import validate_accounts
            accounts = validate_accounts(store.read("accounts.json"))
            selected, updated = account_identity.choose(accounts, entries, ident, provider, entry, plan, name)
            ident = selected["id"]
        if any(other != ident and (
                (provider == "muse_code" and saved.get("provider") == "muse_code"
                 and saved.get("kind") != "cli_owned"
                 and saved["subject_id"] == entry["subject_id"] and saved["team_id"] == entry["team_id"])
                or (provider != "muse_code" and saved.get("access_token") == entry["access_token"]))
               for other, saved in entries.items()):
            raise LiveError("duplicate_account", "This login/team is already saved under another account. Select the intended account before capturing.")
        import auth_renewal
        if auth_renewal.enrolled(store, key, ident):
            raise LiveError("managed_account", "This account has an independent renewal grant. Use --auth-forget ACCOUNT before replacing it with a captured CLI token.")
        entries[ident] = entry
        envelope = encrypt_vault(entries, key, salt)
        store.write("vault.json", envelope, replace=True)
        if updated is not None:
            # Save token and identity together before changing fallback labels.
            # If configuration writing fails, retrying capture can complete it;
            # an old token is never left under a newly assigned account name.
            store.write("accounts.json", {"accounts": updated}, replace=True)
        if newly_created:
            remember_unlock(store, envelope, key)
        if selected is not None:
            return {"account_id": ident, "account_name": selected["name"], "expires_at": entry.get("expires_at")}
        return entry.get("expires_at")
    finally:
        fcntl.flock(store.fd, fcntl.LOCK_UN)


@contextmanager
def deadline(seconds):
    # A foreground deadline also bounds DNS and slow response bodies; no thread
    # or child survives it. Never replace a pre-existing process timer.
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise LiveError("timer_busy", "Cannot install a safe request deadline.")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(*_):
        raise TimeoutError()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class UsageClient:
    endpoints = READ_ONLY_ENDPOINTS
    routes = {URL: (HOST, PATH), CODEX_URL: (CODEX_HOST, CODEX_PATH)}

    def request(self, method, url, access_token, account_id=None):
        if (method, url) not in self.endpoints:
            raise LiveError("prohibited_endpoint", "Network request prohibited by the endpoint allowlist.")
        validate_token(access_token)
        if url == CODEX_URL:
            validate_account_id(account_id)
        elif account_id is not None:
            raise LiveError("prohibited_endpoint", "Account routing header is only allowed for Codex.")
        host, path = self.routes[url]
        headers = {"Authorization": "Bearer " + access_token,
                   "User-Agent": "private-claude-usage/1.0",
                   "Accept": "application/json", "Connection": "close"}
        if url == CODEX_URL:
            headers["ChatGPT-Account-Id"] = account_id
        else:
            headers["anthropic-beta"] = "oauth-2025-04-20"
        connection = response = None
        try:
            with deadline(TIMEOUT):
                # Build context explicitly: create_default_context can honor
                # SSLKEYLOGFILE and write TLS secrets. This context never does.
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_default_certs()
                context.set_alpn_protocols(["http/1.1"])
                connection = http.client.HTTPSConnection(host, timeout=TIMEOUT, context=context)
                connection.set_debuglevel(0)
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                status = response.status
                if status in (401, 403):
                    raise LiveError("authentication_error", "Access token expired, revoked, or not authorized. No refresh attempted; capture again after your normal login.")
                if status == 429:
                    delay = retry_delay(response.getheader("Retry-After"))
                    raise LiveError("rate_limited", "Usage endpoint rate limited this request (HTTP 429). Subscription quota is unknown, not exhausted. Try again after the cooldown.", retry_after=delay)
                if 300 <= status < 400:
                    raise LiveError("redirect_blocked", "Usage endpoint redirected; redirect blocked to protect credentials.")
                if status != 200:
                    raise LiveError("http_error", f"Usage endpoint returned HTTP {status}; response body suppressed.")
                return json_object(response.read(MAX_BYTES + 1))
        except TimeoutError:
            raise LiveError("timeout", "Usage request timed out; no retry or refresh attempted.") from None
        except ssl.SSLError:
            raise LiveError("tls_error", "TLS verification or connection failed; no insecure fallback.") from None
        except (OSError, http.client.HTTPException):
            raise LiveError("network_error", "Usage request failed (DNS, connection, or HTTP transport); no retry or refresh attempted.") from None
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()


def fetch(entry, seen, provider="claude"):
    if entry.get("provider", "claude") != provider:
        raise LiveError("provider_mismatch", "Saved credential belongs to a different provider; no request made.")
    if provider == "muse_code":
        import muse_usage
        if entry.get("kind") == "cli_owned":
            import owned_muse
            return owned_muse.fetch(entry, seen)
        return muse_usage.fetch(entry, seen)
    if provider not in ("claude", "codex"):
        raise LiveError("unsupported", "Unsupported credential provider.")
    token = entry["access_token"]
    digest = hashlib.sha256(token.encode()).digest()
    if digest in seen:
        raise LiveError("duplicate_account", "Duplicate saved token; refusing to label one account as two accounts.")
    seen.add(digest)
    if entry["expires_at"] is not None and entry["expires_at"] <= time.time():
        raise LiveError("token_expired", "Saved access token has expired. No refresh attempted; capture again after your normal login.")
    if provider == "codex":
        result = UsageClient().request("GET", CODEX_URL, token, entry.get("account_id"))
        if result.get("account_id") is not None and result["account_id"] != entry.get("account_id"):
            raise LiveError("account_mismatch", "Codex returned usage for a different account; data suppressed.")
    else:
        result = UsageClient().request("GET", URL, token)
    # A malformed or hostile response must not reflect our token into a label.
    if token in json.dumps(result):
        raise LiveError("invalid_data", "Credential-like content in usage response; content suppressed.")
    return result


def default_source(provider="claude"):
    if provider == "codex":
        return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "auth.json"
    if provider != "claude":
        raise LiveError("unsupported", MUSE_NOTICE)
    directory = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(directory).expanduser() / ".credentials.json" if directory else Path.home() / ".claude/.credentials.json"
