"""Explicit panel confirmations. Never switch from a timer or report refresh."""
import datetime as dt

import claude_usage as app
import claude_switch
import credential_state
import live_usage as live


def prepare(source, data, provider, now):
    from usage_panel import next_account, unavailable_reason
    if provider != "claude":
        raise live.LiveError("switch_unavailable", "Claude switching only")
    if not data or not 0 <= (now - app.timestamp(data["generated_at"])).total_seconds() <= 120:
        raise live.LiveError("stale_report", "Refresh before switching")
    if not source.check():
        raise live.LiveError("vault_locked", "Unlock before switching")
    rows = [r for r in data["accounts"] if r["provider"] == provider]
    selected = [r for r in rows if r.get("selected") is True]
    if len(selected) != 1:
        raise live.LiveError("unknown_selection", "Current login unverified")
    target = next_account(rows, selected[0], now)
    if target is None:
        raise live.LiveError("no_candidate", unavailable_reason([r for r in rows if r['id'] != selected[0]['id']], now))
    accounts, entries = source.load()
    current = credential_state.current([a for a in accounts if app.PROVIDERS[a["plan"]] == provider], entries).get(provider)
    expected = credential_state.fingerprint(entries.get(selected[0]["id"], {}), provider)
    if expected is None or current != expected:
        raise live.LiveError("changed_selection", "Login changed; refresh first")
    target_state = credential_state.fingerprint(entries.get(target["id"], {}), provider)
    if target_state is None:
        raise live.LiveError("missing_target", "Target changed; refresh first")
    return {"target": target, "current": current, "target_state": target_state,
            "expires_at": now + dt.timedelta(seconds=60)}


def execute(source, confirmation, now):
    if now >= confirmation["expires_at"]:
        raise live.LiveError("confirmation_expired", "Confirmation expired; retry")
    if not source.check() or source.locked or source.key is None:
        raise live.LiveError("vault_locked", "Unlock before switching")
    target = confirmation["target"]
    with app.Store(source.path) as store:
        accounts = app.validate_accounts(store.read("accounts.json"))
        # Run in the panel process: killing a cancellable report worker must never
        # interrupt this transaction. The switcher owns locks and rollback.
        return claude_switch.switch(store, accounts, target["id"], target["plan"],
                                    vault_key=source.key, expected_current=confirmation["current"],
                                    expected_target=confirmation["target_state"], require_available=True,
                                    authorize=lambda: source.check() and not source.locked)


def confirmation_lines(confirmation, width):
    from usage_panel import remaining
    target = confirmation["target"]
    lines = [("Switch Claude to", "group")]
    lines.extend((line, "normal") for line in app.wrap_cells(target["name"], max(1, width)))
    lines += [(app.PLANS[target["plan"]], "muted"), ("", "normal"),
              (f"5h {remaining(target, 'five_hour')} · 7d {remaining(target, 'seven_day')} left", "selected"),
              ("", "normal"), ("Changes shared Claude login.", "muted"),
              ("Sessions may need to reload.", "muted"), ("", "normal"),
              ("Enter confirm · Esc cancel", "group")]
    return lines
