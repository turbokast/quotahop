"""Explicit, one-shot Muse CLI bootstrap probe. Never invoked by default reports.

The first-party CLI obtains quota through its key bootstrap, not a read-only
usage API. Callers must explicitly select --muse-cli-current. No credentials
returned by the server are adopted or saved, and no CLI process is launched.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import stat

import live_usage as live
import account_identity

HOST = "api.meta.ai"
PATH = "/muse-code/key"
URL = "https://" + HOST + PATH
BODY = b'{"onboard":false}'
NOTICE = ("Explicit Muse CLI bootstrap check (POST /muse-code/key, onboarding disabled). "
          "Not a read-only API contract; returned credentials are discarded. "
          "No login, refresh, inference, or local credential writes.")


def read_credentials():
    source = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "muse/auth.json"
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOATIME)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
                raise live.LiveError("unsafe_credentials", "Muse credentials must be an owned private regular file without links.")
            value = live.json_object(stream.read(live.MAX_BYTES + 1))
        providers = value.get("providers")
        meta = providers.get("meta") if isinstance(providers, dict) else None
        if not isinstance(meta, dict):
            raise live.LiveError("invalid_credentials", "No existing Muse Meta login found; no login attempted.")
        token, key = meta.get("access_token"), meta.get("api_key")
        if (not isinstance(token, str) or not re.fullmatch(r"dca:[A-Za-z0-9._~+/=-]{10,8192}", token)
                or not isinstance(key, str) or not 10 <= len(key) <= 8192):
            raise live.LiveError("invalid_credentials", "An existing Muse device login and API key are required; no provisioning attempted.")
        return token, key
    except OSError:
        raise live.LiveError("credential_read_error", "Cannot read current Muse credentials safely; no files were changed.") from None


class MuseCliClient:
    def request(self, method, url, token, existing_key):
        if (method, url) != ("POST", URL):
            raise live.LiveError("prohibited_endpoint", "Request prohibited by the explicit Muse bootstrap allowlist.")
        if not isinstance(token, str) or not re.fullmatch(r"dca:[A-Za-z0-9._~+/=-]{10,8192}", token):
            raise live.LiveError("invalid_credentials", "Invalid Muse device access token.")
        if not isinstance(existing_key, str) or not 10 <= len(existing_key) <= 8192:
            raise live.LiveError("invalid_credentials", "Existing Muse API key required; no provisioning attempted.")
        response = connection = None
        try:
            with live.deadline(live.TIMEOUT):
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_default_certs()
                context.set_alpn_protocols(["http/1.1"])
                connection = http.client.HTTPSConnection(HOST, timeout=live.TIMEOUT, context=context)
                connection.set_debuglevel(0)
                connection.request("POST", PATH, body=BODY, headers={
                    "Authorization": "Bearer " + token, "x-api-version": "1.0.0",
                    "Content-Type": "application/json", "Accept": "application/json",
                    "Connection": "close", "User-Agent": "private-claude-usage/1.0",
                })
                response = connection.getresponse()
                if response.status in (401, 403):
                    raise live.LiveError("authentication_error", "Muse login was rejected. No login or refresh attempted.")
                if 300 <= response.status < 400:
                    raise live.LiveError("redirect_blocked", "Muse redirected; credentials were not forwarded.")
                if response.status == 429:
                    raise live.LiveError("rate_limited", "Muse rate limited this check; no retry attempted.")
                if response.status != 200:
                    raise live.LiveError("http_error", f"Muse returned HTTP {response.status}; body suppressed.")
                raw = live.json_object(response.read(live.MAX_BYTES + 1))
                returned_key = raw.get("api_key")
                if not isinstance(returned_key, str) or not hashlib.sha256(returned_key.encode()).digest() == hashlib.sha256(existing_key.encode()).digest():
                    # Detection only: cannot roll back or guarantee server behavior.
                    raise live.LiveError("key_changed", "Muse returned a different or missing API key. It was discarded; no local credentials changed. Server-side key behavior cannot be guaranteed.")
                if raw.get("is_subs_active") is not True:
                    raise live.LiveError("no_subscription", "Muse did not report an active subscription; API spending is not substituted.")
                # Project only quota and plan. Never expose the credential-bearing
                # bootstrap response, billing details or action URLs.
                result = {"subs_usage": raw.get("subs_usage"), "subs_tier_name": raw.get("subs_tier_name")}
                identity = account_identity.extract(raw.get("user_email"), raw.get("user_full_name"), (token, existing_key))
                if identity:
                    result["identity"] = identity
                encoded = json.dumps(result)
                if token in encoded or existing_key in encoded:
                    raise live.LiveError("invalid_data", "Credential-like content in Muse usage; suppressed.")
                return result
        except TimeoutError:
            raise live.LiveError("timeout", "Muse check timed out; no retry or refresh.") from None
        except ssl.SSLError:
            raise live.LiveError("tls_error", "Muse TLS verification/connection failed; no insecure fallback.") from None
        except (OSError, http.client.HTTPException):
            raise live.LiveError("network_error", "Muse network request failed; no retry or refresh.") from None
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()


def parse(value):
    from claude_usage import UsageError, clean_text, epoch, number, parse_usage, reject_secrets
    reject_secrets(value)
    quota = value.get("subs_usage")
    if quota is None:
        raise live.LiveError("usage_unavailable", "Muse authenticated but omitted subscription usage. No model call made to populate it; try again after your normal Muse activity.")
    if not isinstance(quota, dict):
        raise UsageError("Invalid Muse subscription usage.")
    result = parse_usage({})
    for source, dest in (("window", "five_hour"), ("weekly", "seven_day")):
        item = quota.get(source)
        if item is None:
            continue
        if not isinstance(item, dict):
            raise UsageError("Invalid Muse subscription window.")
        # Muse may report usage beyond the allowance. Preserve its percentage
        # rather than rejecting overage or silently clamping it to 100%.
        parsed = {"utilization": number(item.get("used_percent")),
                  "resets_at": epoch(item.get("resets_at"))}
        if source == "window" and item.get("window_duration_mins") != 300:
            minutes = number(item.get("window_duration_mins"))
            label = f"{minutes:g}m" if minutes is not None else "duration unknown"
            result["model_windows"]["Muse subscription (" + label + ")"] = parsed
        else:
            result[dest] = parsed
    if not any(isinstance(quota.get(k), dict) for k in ("window", "weekly")):
        raise live.LiveError("usage_unavailable", "Muse returned no subscription windows; usage is unknown.")
    plan = value.get("subs_tier_name")
    return result, clean_text(plan) if plan is not None else None


def report(now):
    import datetime as dt
    from claude_usage import UsageError, UTC
    row = {"id": "muse-current", "name": "Muse (current CLI login)", "plan": "muse_code",
           "provider": "muse_code", "plan_verified": False, "reported_plan": None,
           "quota_scope": "current_muse_cli_login", "cli_account_verified": True,
           "status": "error", "live": False, "captured_at": None, "usage": None,
           "message": NOTICE}
    try:
        token, key = read_credentials()
        raw = MuseCliClient().request("POST", URL, token, key)
        if raw.get("identity"):
            account_identity.validate(raw["identity"])
            row.update(identity=raw["identity"], name=account_identity.label(raw["identity"]))
        usage, plan = parse(raw)
        row.update(status="live", live=True, usage=usage, reported_plan=plan,
                   plan_verified=plan is not None, captured_at=dt.datetime.now(UTC).isoformat())
    except (live.LiveError, UsageError) as exc:
        row.update(error_code=getattr(exc, "code", "invalid_data"), message=str(exc))
    return {"schema_version": 1, "generated_at": now.isoformat(), "live": row["live"],
            "live_accounts": int(row["live"]), "accounts": [row]}
