"""Display-only account identity; never an authentication or refresh mechanism."""
import base64
import hashlib
import os
from pathlib import Path
import re
import stat
import unicodedata


def validate(identity):
    if not isinstance(identity, dict) or not identity or set(identity) - {"email", "name"}:
        raise ValueError("Invalid account identity")
    from claude_usage import reject_secrets
    for field, value in identity.items():
        if (not isinstance(value, str) or not value.strip() or len(value) > 254
                or any(unicodedata.category(c).startswith("C") for c in value)
                or "dca:" in value):
            raise ValueError("Invalid account identity")
        reject_secrets(value)
        if field == "email" and not re.fullmatch(r"[^\s@]+@[^\s@]+", value):
            raise ValueError("Invalid account email")
    return identity


def extract(email=None, name=None, secrets=()):
    from claude_usage import UsageError
    result = {}
    for field, value in (("email", email), ("name", name)):
        if value is None or value == "":
            continue
        try:
            validate({field: value})
            if any(secret and secret in value for secret in secrets):
                continue
            result[field] = value.strip()
        except (ValueError, UsageError):
            continue
    return result


def label(identity):
    return identity.get("email") or identity.get("name")


def codex(token):
    """Unverified JWT display claims only; do not retain an ID/refresh token."""
    import live_usage as live
    try:
        if not isinstance(token, str) or token.count(".") != 2:
            return {}
        part = token.split(".")[1]
        claims = live.json_object(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        profile = claims.get("https://api.openai.com/profile", claims)
        return extract(profile.get("email"), profile.get("name"), (token,)) if isinstance(profile, dict) else {}
    except (ValueError, live.LiveError):
        return {}


def claude(source, access_token):
    """Read only the CLI's local account label; it is not verified token identity.

    Metadata may be publicly readable (unlike credentials). Never chmod or write
    it. Custom sources use only adjacent metadata, never another login's home
    metadata. Verify the selected credential is still the same after reading.
    """
    import live_usage as live
    path = Path(source).expanduser().absolute()
    metadata = path.parent / ".claude.json"
    if path == Path.home() / ".claude/.credentials.json" and not os.environ.get("CLAUDE_CONFIG_DIR"):
        metadata = Path.home() / ".claude.json"
    try:
        fd = os.open(metadata, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                return {}
            obj = live.json_object(stream.read(live.MAX_BYTES + 1)).get("oauthAccount")
        if not isinstance(obj, dict):
            return {}
        # This is still a display hint: local metadata can be stale even when
        # the credential stayed constant. It never determines network routing.
        again = live.read_access_token(path, "claude", include_identity=False)
        if again["access_token"] != access_token:
            raise live.LiveError("account_changed", "Claude login changed during capture. Nothing saved; retry when account switching is finished.")
        return extract(obj.get("emailAddress"), obj.get("displayName") or obj.get("fullName"), (access_token,))
    except (OSError, live.LiveError) as exc:
        if isinstance(exc, live.LiveError) and exc.code == "account_changed":
            raise
        return {}


def saved(entry):
    identity = entry.get("identity") or {}
    if not identity and entry.get("provider") == "codex":
        identity = codex(entry.get("access_token"))
    return identity


def for_report(entries):
    """Identify legacy Claude captures only if their token matches the current one.

    No writes, network, refresh, or identity attribution to a different saved
    token. New captures already carry their label and need no source-file read.
    """
    import live_usage as live
    missing = [ident for ident, entry in entries.items() if entry.get("provider", "claude") == "claude"
               and entry.get("access_token") and not entry.get("identity")]
    if not missing:
        return entries
    try:
        current = live.read_access_token(live.default_source("claude"), "claude")
    except live.LiveError:
        return entries
    if not current.get("identity"):
        return entries
    result = dict(entries)
    for ident in missing:
        if entries[ident]["access_token"] == current["access_token"]:
            result[ident] = {**entries[ident], "identity": current["identity"]}
    return result


def same_login(left, right, provider):
    if left.get("provider", "claude") != provider:
        return False
    if provider == "codex" and left.get("account_id") != right.get("account_id"):
        return False
    if provider == "muse_code":
        if left.get("kind") == "cli_owned" or right.get("kind") == "cli_owned":
            return left.get("kind") == right.get("kind") == "cli_owned" and left.get("access_token") == right.get("access_token")
        return left.get("subject_id") == right.get("subject_id") and left.get("team_id") == right.get("team_id")
    if left.get("access_token") == right.get("access_token"):
        return True
    a, b = saved(left).get("email"), saved(right).get("email")
    return bool(a and b and a.casefold() == b.casefold())


def choose(accounts, entries, selector, provider, entry, plan=None, name=None):
    """Resolve a named capture to stable storage IDs without guessing a seat."""
    from claude_usage import UsageError, PROVIDERS, validate_accounts, clean_text
    aliases = {"claude", "codex", "muse"}
    exact = [a for a in accounts if a["id"] == selector]
    if exact and plan and exact[0]["plan"] != plan:
        raise UsageError("Selected account has a different subscription type. Capture by email with --plan to save a separate subscription.")
    scope = plan or (exact[0]["plan"] if exact else None)
    candidates = [a for a in accounts if scope is None or a["plan"] == scope]
    matching = exact or [a for a in candidates if (a["name"].casefold() == selector.casefold()
                        or selector.casefold() in [v.casefold() for v in saved(entries.get(a["id"], {})).values()])
                        and PROVIDERS[a["plan"]] == provider]
    if len(matching) == 1:
        scope = matching[0]["plan"]
    existing = [a for a in accounts if (scope is None or a["plan"] == scope)
                and a["id"] in entries and same_login(entries[a["id"]], entry, provider)]
    if len(matching) > 1 or len(existing) > 1:
        raise UsageError("Account name/email is ambiguous; specify --plan or use a unique saved ID.")
    if matching and existing and matching[0]["id"] != existing[0]["id"]:
        raise UsageError("This login is already captured under another account; use its name/email.")
    account = dict((matching or existing or [{}])[0])
    if account and PROVIDERS[account["plan"]] != provider:
        raise UsageError("Selected account belongs to a different provider.")
    if account.get("source") == "cli_current":
        raise UsageError("This Muse row already follows the current CLI login; capture is unnecessary.")
    identity = saved(entry)
    if "@" in selector and identity.get("email") and selector.casefold() != identity["email"].casefold():
        raise UsageError("The supplied email differs from the current login's local identity. Nothing saved.")
    previous = entries.get(account.get("id"), {})
    if previous:
        old_email, new_email = saved(previous).get("email"), identity.get("email")
        if ((old_email and new_email and old_email.casefold() != new_email.casefold())
                or (provider == "codex" and previous.get("account_id") != entry.get("account_id"))):
            raise UsageError("This capture would replace a different saved account. Use the current account's email/name instead.")
    chosen_name = label(identity) or name or (selector if selector not in aliases and not exact else None) or account.get("name")
    if not chosen_name:
        raise UsageError("No account name/email is available locally. Supply --name or capture an email/name with --plan.")
    chosen_name = clean_text(chosen_name)
    chosen_plan = plan or account.get("plan") or {"codex": "codex", "muse_code": "muse_code"}.get(provider)
    if chosen_plan is None:
        raise UsageError("For a new Claude account, add --plan max_20x or --plan team_premium.")
    if PROVIDERS[chosen_plan] != provider:
        raise UsageError("Plan does not match the selected provider.")
    if not account:
        # Reuse an empty template slot of the right plan; never steal a saved slot.
        empty = [a for a in accounts if a["plan"] == chosen_plan and a["id"] not in entries
                 and not a.get("source") and re.fullmatch(r"(?:Max|Team|Codex|Muse Code) [0-9]+", a["name"])]
        account = dict(empty[0]) if empty else {"id": provider.replace("_", "-") + "-" + hashlib.sha256(
            (chosen_name.casefold() + "\0" + chosen_plan + "\0" + entry.get("account_id", entry.get("team_id", ""))).encode()).hexdigest()[:16]}
        if any(a["id"] == account["id"] for a in accounts) and not empty:
            raise UsageError("Generated account ID already exists. Add a unique account ID and capture into it.")
    account.update(name=chosen_name, plan=chosen_plan)
    if not identity:
        manual = extract(email=chosen_name) if "@" in chosen_name else extract(name=chosen_name)
        if not manual:
            raise UsageError("Supply a valid account email or display name.")
        entry["identity"] = manual
    updated = [account if a["id"] == account["id"] else a for a in accounts]
    if not any(a["id"] == account["id"] for a in accounts):
        updated.append(account)
    validate_accounts({"accounts": updated})
    return account, updated
