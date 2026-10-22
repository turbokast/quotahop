"""Meta portal subscription reads; no Muse CLI, OAuth, key minting or inference."""
import getpass
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import warnings

import live_usage as live
import account_identity

HOST = "dev.meta.ai"
BASE = "https://" + HOST
ME = "/api/auth/me"
TEAMS = "/api/portal/teams"
QUOTA = r"/api/portal/teams/[0-9]{1,40}/subscription-quota"
NOTICE = ("Live Meta portal subscription quota for the team you selected; "
          "undocumented read-only interface. Its link to a Muse CLI login is not verified.")


def numeric_id(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,40}", value):
        raise live.LiveError("invalid_data", "Missing or invalid Meta portal identity/team ID.")
    return value


def cookie_value(value):
    # RFC cookie-octet, excluding separators, whitespace and header injection.
    if (not isinstance(value, str) or value.startswith("llama_dev_sess=")
            or not re.fullmatch(r"[!#-+\--:<-\[\]-~]{10,8192}", value)):
        raise live.LiveError("invalid_credentials", "Paste only the llama_dev_sess cookie value, without a header or other cookies.")
    return value


def validate_entry(entry):
    expected = {"provider", "session_cookie", "subject_id", "team_id", "team_name"}
    if "identity" in entry:
        account_identity.validate(entry["identity"])
        expected.add("identity")
    if set(entry) != expected or entry["provider"] != "muse_code":
        raise ValueError()
    cookie_value(entry["session_cookie"])
    numeric_id(entry["subject_id"])
    numeric_id(entry["team_id"])
    safe_label(entry["team_name"], entry["session_cookie"])


def safe_label(value, secret):
    # Import only when called; avoids a module-import cycle with the CLI.
    from claude_usage import clean_text, reject_secrets
    if secret in str(value):
        raise live.LiveError("invalid_data", "Credential-like content in Meta response; suppressed.")
    reject_secrets(value)
    return clean_text(value)


def hidden_cookie():
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        os.close(fd)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass("Meta llama_dev_sess cookie value (hidden): ")
    except (OSError, EOFError, getpass.GetPassWarning):
        raise live.LiveError("tty_required", "A terminal with hidden input is required; never send cookies in chat or command arguments.") from None
    return cookie_value(value)


class MuseClient:
    def request(self, method, url, session_cookie):
        if (method != "GET" or not url.startswith(BASE)
                or (url[len(BASE):] not in (ME, TEAMS) and not re.fullmatch(QUOTA, url[len(BASE):]))):
            raise live.LiveError("prohibited_endpoint", "Meta request prohibited by the read-only endpoint allowlist.")
        cookie_value(session_cookie)
        path = url[len(BASE):]
        connection = response = None
        try:
            with live.deadline(live.TIMEOUT):
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_default_certs()
                context.set_alpn_protocols(["http/1.1"])
                connection = http.client.HTTPSConnection(HOST, timeout=live.TIMEOUT, context=context)
                connection.set_debuglevel(0)
                connection.request("GET", path, headers={
                    "Cookie": "llama_dev_sess=" + session_cookie,
                    "Accept": "application/json", "Content-Type": "application/json", "Connection": "close",
                    "User-Agent": "quotahop/1.0",
                })
                response = connection.getresponse()
                if response.status in (401, 403):
                    raise live.LiveError("authentication_error", "Meta portal session expired or team access denied. No renewal attempted; capture a valid browser session again.")
                if 300 <= response.status < 400:
                    raise live.LiveError("redirect_blocked", "Meta portal redirected; credentials were not forwarded.")
                if response.status == 429:
                    raise live.LiveError("rate_limited", "Meta portal rate limited the read; no retry attempted.")
                if response.status != 200:
                    raise live.LiveError("http_error", f"Meta portal returned HTTP {response.status}; body suppressed.")
                raw = response.read(live.MAX_BYTES + 1)
                if session_cookie.encode() in raw:
                    raise live.LiveError("invalid_data", "Credential-like content in Meta response; suppressed.")
                # Set-Cookie is deliberately ignored; no cookie jar or session refresh.
                result = live.json_object(raw)
                if session_cookie in json.dumps(result):
                    raise live.LiveError("invalid_data", "Credential-like content in Meta response; suppressed.")
                return result
        except TimeoutError:
            raise live.LiveError("timeout", "Meta portal request timed out; no retry or refresh.") from None
        except ssl.SSLError:
            raise live.LiveError("tls_error", "Meta portal TLS verification/connection failed; no insecure fallback.") from None
        except (OSError, http.client.HTTPException):
            raise live.LiveError("network_error", "Meta portal network request failed; no retry or refresh.") from None
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()


def prepare_capture(team_id=None):
    print(NOTICE, file=sys.stderr)
    print("In your browser at dev.meta.ai, copy only the llama_dev_sess cookie value from Developer Tools → Application/Storage → Cookies. It is encrypted locally; never paste it into chat.", file=sys.stderr)
    cookie = hidden_cookie()
    client = MuseClient()
    me = client.request("GET", BASE + ME, cookie)
    subject = numeric_id(me.get("userId"))
    raw = client.request("GET", BASE + TEAMS, cookie)
    listed = raw.get("teams")
    if not isinstance(listed, list) or not 1 <= len(listed) <= 100:
        raise live.LiveError("invalid_data", "No usable Meta teams returned for this browser session.")
    teams = {}
    for item in listed:
        if not isinstance(item, dict):
            raise live.LiveError("invalid_data", "Malformed Meta team list.")
        ident = numeric_id(item.get("team_id"))
        if ident in teams:
            raise live.LiveError("invalid_data", "Duplicate Meta team IDs; capture refused.")
        teams[ident] = safe_label(item.get("team_name") or ident, cookie)
    for ident, name in teams.items():
        print(f"  {ident}  {name}", file=sys.stderr)
    if team_id is None:
        print("Choose the team whose Muse subscription you want to report (no default): ", end="", file=sys.stderr, flush=True)
        try:
            with open("/dev/tty", "r") as terminal:
                team_id = terminal.readline(100).strip()
        except OSError:
            raise live.LiveError("tty_required", "Choose a team using --muse-team TEAM_ID or a terminal.") from None
    team_id = numeric_id(team_id)
    if team_id not in teams:
        raise live.LiveError("account_mismatch", "Selected team is not visible to this browser session; no credential saved.")
    entry = {"provider": "muse_code", "session_cookie": cookie,
             "subject_id": subject, "team_id": team_id, "team_name": teams[team_id]}
    identity = account_identity.extract(me.get("email"), me.get("name"), (cookie,))
    if identity:
        entry["identity"] = identity
    # One quota read verifies the selected team's subscription before saving.
    quota = client.request("GET", BASE + f"/api/portal/teams/{team_id}/subscription-quota", cookie)
    from claude_usage import parse_muse_portal
    parsed = parse_muse_portal(quota)
    print(f"Selected {teams[team_id]} · {parsed['reported_plan'] or 'plan unknown'}; subscription quota verified.", file=sys.stderr)
    return entry


def fetch(entry, seen):
    validate_entry(entry)
    digest = hashlib.sha256((entry["session_cookie"] + "\0" + entry["team_id"]).encode()).digest()
    if digest in seen:
        raise live.LiveError("duplicate_account", "Duplicate Meta session/team; refusing to report it as two accounts.")
    seen.add(digest)
    client = MuseClient()
    me = client.request("GET", BASE + ME, entry["session_cookie"])
    if numeric_id(me.get("userId")) != entry["subject_id"]:
        raise live.LiveError("account_mismatch", "Meta browser identity differs from the saved account; no quota request made.")
    result = client.request("GET", BASE + f"/api/portal/teams/{entry['team_id']}/subscription-quota", entry["session_cookie"])
    identity = account_identity.extract(me.get("email"), me.get("name"), (entry["session_cookie"],))
    if identity:
        result["identity"] = identity
    return result
