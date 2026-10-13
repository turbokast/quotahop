"""Synchronize already-renewed access tokens; no OAuth grant or auth-file writes.

Only --refresh establishes server-verified account/organization bindings.
--switch-next may reuse them to pick up the current CLI's newer access token.
Reports also follow newer CLI access tokens for grants handed to Claude Code.
Inactive expired grants cannot be renewed safely from the captured credentials.
"""
import fcntl
import time
import uuid

import account_identity
import claude_switch
import live_usage as live


def validate_binding(value):
    if not isinstance(value, dict) or set(value) != {"account_uuid", "organization_uuid"}:
        raise ValueError("Invalid Claude account binding")
    for item in value.values():
        if not isinstance(item, str) or str(uuid.UUID(item)) != item:
            raise ValueError("Invalid Claude account binding")
    return value


def binding(metadata):
    return validate_binding({"account_uuid": metadata["accountUuid"],
                             "organization_uuid": metadata["organizationUuid"]})


def synchronize(store, accounts, entries, key, salt, *, establish=False):
    """Caller holds the vault lock. Only the tool-owned encrypted vault changes."""
    import auth_renewal
    managed = auth_renewal.records(store, key)
    updated = dict(entries)
    notes = []
    candidates = [a for a in accounts if a["plan"] in ("max_20x", "team_premium") and a["id"] in entries
                  and (a["id"] not in managed or managed[a["id"]]['state'] == 'cli_owned')]
    if establish:
        # Run once to bind each existing token to its server-reported account
        # and organization. Never guess from email or mutable local metadata.
        for account in candidates:
            entry = updated[account["id"]]
            if entry.get("claude_identity"):
                continue
            try:
                claude_switch.select([account], updated, account["id"], None)
                _, metadata = claude_switch.target_auth(account, entry)
                updated[account["id"]] = {**entry, "claude_identity": binding(metadata)}
            except live.LiveError:
                notes.append({"account_id": account["id"], "state": "binding_unavailable",
                              "message": "Could not verify this saved Claude identity. If expired, recapture it after your normal sign-in."})
    bound = [a for a in candidates if updated[a["id"]].get("claude_identity")]
    if bound:
        try:
            claude_switch.check_environment()
            source = claude_switch.paths()[0]
            fresh = live.read_access_token(source, "claude", include_identity=False)
            if fresh.get("expires_at") is None or fresh["expires_at"] <= time.time() + 60:
                raise live.LiveError("not_fresh", "Current CLI token needs renewal by Claude Code.")
            # A known token needs no additional profile request or vault write.
            if not any(e.get("access_token") == fresh["access_token"] for e in updated.values()):
                profile = claude_switch.read_profile(fresh)
                kind = (profile.get("organization") or {}).get("organization_type")
                plan = {"claude_max": "max_20x", "claude_team": "team_premium"}.get(kind)
                if plan is None:
                    raise live.LiveError("not_supported", "Current CLI plan cannot be synchronized.")
                _, metadata = claude_switch.target_auth({"plan": plan}, fresh, profile=profile)
                matches = [a for a in bound if a["plan"] == plan
                           and updated[a["id"]]["claude_identity"] == binding(metadata)]
                if len(matches) != 1:
                    raise live.LiveError("unmatched", "Current CLI identity has no unique saved account/organization binding. Capture it explicitly.")
                account = matches[0]
                previous = updated[account["id"]]
                if fresh["expires_at"] <= (previous.get("expires_at") or 0):
                    raise live.LiveError("older_copy", "Current CLI token is not newer than the saved copy; saved copy retained.")
                again = live.read_access_token(source, "claude", include_identity=False)
                if again != fresh:
                    raise live.LiveError("account_changed", "CLI credentials changed during synchronization; saved copy retained.")
                fresh["claude_identity"] = binding(metadata)
                identity = account_identity.extract(metadata.get("emailAddress"), metadata.get("displayName"))
                if identity:
                    fresh["identity"] = identity
                updated[account["id"]] = fresh
                notes.append({"account_id": account["id"], "state": "synchronized",
                              "message": "Copied the CLI's already-renewed access token; source credentials unchanged."})
        except live.LiveError as exc:
            notes.append({"state": "source_unavailable", "message": str(exc)})
        except (OSError, ValueError, TypeError, AttributeError):
            notes.append({"state": "source_unavailable", "message": "Could not safely verify the current CLI credential; saved copies retained."})
    if updated != entries:
        store.write("vault.json", live.encrypt_vault(updated, key, salt), replace=True)
    return updated, notes


def follow_cli(store, accounts, entries, key):
    """Follow only grants explicitly handed to Claude; no renewal or CLI writes."""
    import auth_renewal
    saved = auth_renewal.records(store, key)
    owned = [a for a in accounts if saved.get(a['id'], {}).get('state') == 'cli_owned']
    if not owned:
        return entries
    try:
        with auth_renewal.vault_lock(store):
            current, _, salt = live.unlock_vault(store.read('vault.json'), None, key=key)
            return synchronize(store, owned, current, key, salt)[0]
    except live.LiveError as exc:
        if exc.code != 'vault_busy':
            raise
        return entries


def refresh(store, accounts):
    try:
        fcntl.flock(store.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise live.LiveError("vault_busy", "Another capture/switch is active; retry when it finishes.") from None
    try:
        try:
            envelope = store.read("vault.json")
        except FileNotFoundError:
            return {}, []
        entries, key, salt = live.unlock_cached(store, envelope)
        return synchronize(store, accounts, entries, key, salt, establish=True)
    finally:
        fcntl.flock(store.fd, fcntl.LOCK_UN)
