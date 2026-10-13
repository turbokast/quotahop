"""Read-only CLI selection and saved-token readiness. Never renews a token."""
import hashlib
import os

import live_usage as live


def fingerprint(entry, provider):
    token = entry.get("access_token")
    if not token:
        return None
    return hashlib.sha256(token.encode()).digest(), entry.get("account_id") if provider == "codex" else None


def current(accounts, entries=None):
    """Read only configured providers. Results remain in memory, never in JSON."""
    from claude_usage import PROVIDERS
    if entries is not None:
        accounts = [a for a in accounts if a["id"] in entries or a.get("source") == "cli_current"]
    result = {}
    for provider in {PROVIDERS[a["plan"]] for a in accounts}:
        try:
            if provider == "claude":
                import claude_switch
                claude_switch.check_environment()
                source = claude_switch.paths()[0]
            elif provider == "codex":
                if os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
                    result[provider] = None
                    continue
                source = live.default_source(provider)
            else:
                if not any(a.get("source") == "cli_current" or (entries or {}).get(a["id"], {}).get("kind") == "cli_owned" for a in accounts):
                    result[provider] = None
                    continue
                import muse_cli_usage
                token, key = muse_cli_usage.read_credentials()
                result[provider] = hashlib.sha256((token + "\0" + key).encode()).digest()
                continue
            result[provider] = fingerprint(live.read_access_token(source, provider, include_identity=False), provider)
        except (live.LiveError, OSError, ValueError):
            result[provider] = None
    return result


def annotate(data, entries, before, after, now):
    """Exact credential matching, never email guessing or per-process claims."""
    from claude_usage import recommendation
    rows = []
    for original in data["accounts"]:
        row = dict(original)
        provider = row["provider"]
        entry = entries.get(row["id"], {})
        stable = before.get(provider) is not None and before.get(provider) == after.get(provider)
        selected = None
        if stable and provider in ("claude", "codex"):
            selected = fingerprint(entry, provider) == after[provider] if entry else None
        elif stable and row.get("quota_scope") == "current_muse_cli_login":
            selected = True
        elif stable and row.get("quota_scope") == "owned_muse_cli_login":
            selected = hashlib.sha256((entry['access_token'] + "\0" + entry['api_key']).encode()).digest() == after[provider]
        row["selected"] = selected
        expiry = entry.get("expires_at")
        row["credential_expires_at"] = expiry
        if provider != "claude":
            state = "not_switchable"
        elif not entry:
            state = "not_captured"
        elif expiry is None:
            state = "expiry_unknown"
        elif expiry <= now.timestamp():
            state = "expired"
        elif expiry <= now.timestamp() + 60:
            state = "expiring"
        elif "scopes" in entry and "user:inference" not in entry["scopes"]:
            state = "scope_missing"
        elif not row.get("live") or row["status"] != "live":
            state = "unverified"
        elif recommendation(row, now)[0] == 2:
            state = "exhausted"
        elif recommendation(row, now)[0] == 3:
            state = "usage_unknown"
        else:
            state = "ready"
        row["switch_readiness"] = state
        rows.append(row)
    # Without an exact match, a token may have rotated for a saved account.
    # Do not assert that all saved accounts are standby in that case.
    for provider in ("claude", "codex"):
        matched = [r for r in rows if r["provider"] == provider and r["selected"] is True]
        if len(matched) != 1:
            for row in rows:
                if row["provider"] == provider:
                    row["selected"] = None
    return {**data, "accounts": rows, "selection_checked": True,
            "selection_scope": "Current CLI credential files; running sessions may retain another login."}
