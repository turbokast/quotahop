"""Opt-in automatic Claude switch when a Claude Code session hits a usage limit.

Claude Code's StopFailure hook (errors rate_limit and billing_error) runs
`quotahop --auto-switch hook`. The hook only records the stopped session and
starts at most one detached worker; it never prompts, switches or blocks Claude.

The worker waits a few seconds so sessions that stop together are handled
together, then:
  1. unlocks the vault without prompting (five-minute unlock cache or the
     opt-in login keyring); a locked vault leaves the request queued for
     `quotahop --auto-switch run`,
  2. confirms with a fresh usage check that the current login really is at
     (or within 5% of) its limit; a transient HTTP 429 never switches,
  3. runs the same recommended switch as --switch-next, never choosing the
     login that just hit its limit,
  4. asks each stopped session to resume through Herdr, and raises a desktop
     notification for sessions it cannot reach.

State (auto-switch.json) holds session IDs, times and account labels only;
never credentials. Switches are capped per half hour so a run of failures
cannot rotate through every subscription.
"""
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import live_usage as live

STATE = "auto-switch.json"
QUEUE_LOCK = "auto-switch.lock"
WORKER_LOCK = "auto-switch-worker.lock"
HOOK_EVENT = "StopFailure"
HOOK_ERRORS = ("rate_limit", "billing_error")
MATCHER = "|".join(HOOK_ERRORS)
HOOK_MARKER = "--auto-switch hook"
SETTLE_SECONDS = 8          # gather sessions that stop at the same moment
PENDING_TTL = 6 * 3600      # forget stopped sessions after six hours
RECENT_SWITCH = 15 * 60     # a session stopped on the outgoing login just before a switch
MAX_SWITCHES = 3            # automatic switches per SWITCH_WINDOW
SWITCH_WINDOW = 30 * 60
MAX_NUDGES = 2              # resume prompts per session per SWITCH_WINDOW
MAX_PENDING = 50
MAX_HOOK_INPUT = 256 * 1024


# ----- state ---------------------------------------------------------------

def empty_state():
    return {"pending": [], "switches": [], "nudges": {}, "last_switch": None, "last_result": None}


def load(store):
    try:
        value = store.read(STATE)
    except FileNotFoundError:
        return empty_state()
    if not isinstance(value, dict):
        return empty_state()
    state = empty_state()
    pending = value.get("pending")
    if isinstance(pending, list):
        state["pending"] = [p for p in pending if isinstance(p, dict) and valid_session(p.get("session_id"))
                            and isinstance(p.get("at"), (int, float))][:MAX_PENDING]
    if isinstance(value.get("switches"), list):
        state["switches"] = [t for t in value["switches"] if isinstance(t, (int, float))]
    if isinstance(value.get("nudges"), dict):
        state["nudges"] = {k: [t for t in v if isinstance(t, (int, float))]
                           for k, v in value["nudges"].items() if valid_session(k) and isinstance(v, list)}
    for field in ("last_switch", "last_result"):
        if isinstance(value.get(field), dict):
            state[field] = value[field]
    return state


def save(store, state, now):
    state["pending"] = [p for p in state["pending"] if now - p["at"] < PENDING_TTL][-MAX_PENDING:]
    state["switches"] = [t for t in state["switches"] if now - t < SWITCH_WINDOW]
    state["nudges"] = {k: kept for k, v in state["nudges"].items()
                       if (kept := [t for t in v if now - t < SWITCH_WINDOW])}
    store.write(STATE, state, replace=True)


def valid_session(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _lock_file(store, name):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=store.fd)
    import quota_cache
    try:
        quota_cache.private(os.fstat(fd))
    except BaseException:
        os.close(fd)
        raise
    return fd


class queue_lock:
    """Short exclusive lock around auto-switch.json; never the vault lock."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.fd = _lock_file(self.store, QUEUE_LOCK)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        os.close(self.fd)


def worker_running(store):
    fd = _lock_file(store, WORKER_LOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def record(state, outcome, message, now):
    state["last_result"] = {"at": now, "outcome": outcome, "message": message}


# ----- hook ----------------------------------------------------------------

def parse_event(raw):
    """Session ID of a usage-limit stop, or None for anything else."""
    if len(raw) > MAX_HOOK_INPUT:
        return None
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if (not isinstance(event, dict) or event.get("hook_event_name") != HOOK_EVENT
            or event.get("error") not in HOOK_ERRORS or not isinstance(event.get("session_id"), str)):
        return None
    session = event["session_id"].lower()
    return session if valid_session(session) else None


def hook(store, data_dir, raw, *, spawn=None, now=None):
    """Queue the stopped session and make sure one worker will handle it."""
    session = parse_event(raw)
    if session is None:
        return "ignored"
    now = time.time() if now is None else now
    with queue_lock(store):
        state = load(store)
        state["pending"] = [p for p in state["pending"] if p["session_id"] != session]
        state["pending"].append({"session_id": session, "at": now})
        save(store, state, now)
        # Checked under the queue lock: a finishing worker releases its lock
        # only while holding this one with an empty queue, so a request is
        # either seen by the running worker or starts a new one.
        if worker_running(store):
            return "queued"
        (spawn or spawn_worker)(data_dir)
    return "started"


def spawn_worker(data_dir):
    script = Path(__file__).absolute().parent / "quotahop"
    subprocess.Popen([sys.executable, "-B", str(script), "--data-dir", str(Path(data_dir).absolute()),
                      "--auto-switch", "worker"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, umask=0o077, close_fds=True)


# ----- worker --------------------------------------------------------------

def run(store, accounts, *, interactive=False, settle=0, sleep=time.sleep, clock=time.time,
        notify=None, switcher=None):
    """Process queued sessions until none remain. Returns the last result."""
    fd = _lock_file(store, WORKER_LOCK)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"outcome": "busy", "changed": False,
                    "message": "An automatic switch is already in progress; it will resume the queued sessions."}
        if settle:
            sleep(settle)
        result = {"outcome": "idle", "changed": False, "message": "No stopped sessions are waiting."}
        while True:
            with queue_lock(store):
                state = load(store)
                batch = [p["session_id"] for p in state["pending"] if clock() - p["at"] < PENDING_TTL]
                if not batch:
                    save(store, state, clock())
                    os.close(fd)  # Released under the queue lock; see hook().
                    fd = -1
                    return result
            result = process(store, accounts, batch, state, interactive=interactive, clock=clock,
                             notify=notify, switcher=switcher)
            with queue_lock(store):
                latest = load(store)
                for field in ("switches", "last_switch", "last_result"):
                    latest[field] = state[field]
                for session, times in state["nudges"].items():
                    latest["nudges"][session] = sorted(set(latest["nudges"].get(session, []) + times))
                if result["outcome"] in ("locked", "check_failed", "switch_failed"):
                    # Keep the sessions for a later hook or a manual
                    # `--auto-switch run`; never spin on a failure.
                    save(store, latest, clock())
                    return result
                latest["pending"] = [p for p in latest["pending"] if p["session_id"] not in batch]
                save(store, latest, clock())
    finally:
        if fd >= 0:
            os.close(fd)


def vault_key(store, interactive):
    envelope = store.read("vault.json")
    import vault_cache
    key = vault_cache.get(store.fd, envelope)
    if key is None:
        import login_keyring
        key = login_keyring.get(store, envelope)
    if key is None and interactive:
        key = live.unlock_cached(store, envelope)[1]
    if key is None:
        raise live.LiveError("vault_locked", "QuotaHop's vault is locked.")
    live.unlock_vault(envelope, None, key=key)
    return key


def current_account(store, accounts, key):
    """Sync the CLI's latest token, then the saved row it matches, or None."""
    import cli_ownership
    import credential_state
    fcntl.flock(store.fd, fcntl.LOCK_EX)
    try:
        cli_ownership.sync(store, key, force=True)
        entries, _, _ = live.unlock_vault(store.read("vault.json"), None, key=key)
    finally:
        fcntl.flock(store.fd, fcntl.LOCK_UN)
    claude = [a for a in accounts if a["plan"] in ("max_20x", "team_premium")]
    selected = credential_state.current(claude, entries).get("claude")
    if selected is None:
        return None, entries
    matches = [a for a in claude if a["id"] in entries
               and credential_state.fingerprint(entries[a["id"]], "claude") == selected]
    return (matches[0] if len(matches) == 1 else None), entries


def at_limit(store, account, entries):
    """True/False from a fresh check; None when usage cannot be verified."""
    import datetime as dt
    from claude_usage import UTC, recommendation, report
    row = report([account], store, dt.datetime.now(UTC), entries, fresh={account["id"]})["accounts"][0]
    tier = recommendation(row, dt.datetime.now(UTC))[0]
    return None if tier == 3 else tier in (1, 2)


def process(store, accounts, batch, state, *, interactive, clock, notify, switcher):
    import claude_switch
    now = clock()
    try:
        key = vault_key(store, interactive)
    except (live.LiveError, OSError, ValueError):
        message = ("A Claude session hit its usage limit but QuotaHop's vault is locked, so no switch was made. "
                   "Run: quotahop --auto-switch run")
        record(state, "locked", message, now)
        (notify or desktop_notify)(message)
        return {"outcome": "locked", "changed": False, "message": message}
    try:
        current, entries = current_account(store, accounts, key)
        verdict = at_limit(store, current, entries) if current else None
    except (live.LiveError, OSError, ValueError) as exc:
        current, verdict = None, None
        detail = str(exc) if isinstance(exc, live.LiveError) else "local data unreadable"
    else:
        detail = "current login not matched to a saved subscription" if current is None else "usage check failed"
    recent = state["last_switch"] and now - state["last_switch"].get("at", 0) < RECENT_SWITCH
    if verdict is False and recent:
        # Stopped on the outgoing login just before or during a switch.
        label = state["last_switch"].get("account", "another subscription")
        return finish(state, batch, "resumed", label, now, notify, already=True)
    if verdict is False:
        message = "A session reported a usage limit, but the current subscription still has verified headroom (likely a temporary rate limit). No switch made."
        record(state, "not_at_limit", message, now)
        return {"outcome": "not_at_limit", "changed": False, "message": message}
    if verdict is None:
        message = f"A session hit a usage limit but QuotaHop could not verify the current subscription ({detail}). No switch made; failed checks never count as exhausted. Run: quotahop --auto-switch run"
        record(state, "check_failed", message, now)
        (notify or desktop_notify)(message)
        return {"outcome": "check_failed", "changed": False, "message": message}
    if len([t for t in state["switches"] if now - t < SWITCH_WINDOW]) >= MAX_SWITCHES:
        message = f"Automatic switching paused: {MAX_SWITCHES} switches in the last {SWITCH_WINDOW // 60} minutes. Check quotas with quotahop, then use --switch-next."
        record(state, "throttled", message, now)
        (notify or desktop_notify)(message)
        return {"outcome": "throttled", "changed": False, "message": message}
    try:
        result = (switcher or claude_switch.switch)(store, accounts, automatic=True, vault_key=key,
                                                    exclude={current["id"]})
    except live.LiveError as exc:
        exhausted = str(exc).startswith("Cannot verify a switchable")
        outcome = "no_candidate" if exhausted else "switch_failed"
        message = f"A session hit its usage limit; automatic switch not made: {exc}"
        record(state, outcome, message, now)
        (notify or desktop_notify)(message)
        return {"outcome": outcome, "changed": False, "message": message}
    from claude_usage import PLANS
    label = result["account_name"] + (" · " + PLANS[result["plan"]] if result.get("plan") in PLANS else "")
    if result.get("changed"):
        state["switches"].append(now)
        state["last_switch"] = {"at": now, "account": label, "from": current["id"], "to": result.get("account_id")}
    return finish(state, batch, "switched" if result.get("changed") else "resumed", label, now, notify,
                  already=not result.get("changed"))


def resume_message(label, already):
    lead = ("QuotaHop: this session stopped at a Claude usage limit. Claude Code is now on another subscription"
            if already else
            "QuotaHop: this session stopped at a Claude usage limit, so Claude Code has been switched to another subscription")
    return (f"{lead} ({label}). Please resume everything you were doing before the limit stopped you, "
            "continuing from where you left off.")


def finish(state, batch, outcome, label, now, notify, *, already):
    text = resume_message(label, already)
    allowed = [s for s in batch if len([t for t in state["nudges"].get(s, []) if now - t < SWITCH_WINDOW]) < MAX_NUDGES]
    resumed, unreachable = nudge(allowed, text)
    for session in resumed:
        state["nudges"].setdefault(session, []).append(now)
    skipped = len(batch) - len(allowed)
    head = (f"Switched Claude Code to {label}." if outcome == "switched" else f"Claude Code is on {label}.")
    parts = [head, f"Asked {len(resumed)} stopped session(s) to resume."]
    if unreachable:
        parts.append(f"{len(unreachable)} session(s) are not in Herdr: type 'continue' in them to resume.")
    if skipped:
        parts.append(f"{skipped} session(s) were not prompted again (already asked {MAX_NUDGES} times recently).")
    message = " ".join(parts)
    record(state, outcome, message, now)
    if unreachable or skipped or outcome == "switched":
        (notify or desktop_notify)(message)
    return {"outcome": outcome, "changed": outcome == "switched", "message": message,
            "resumed_sessions": len(resumed), "unreachable_sessions": len(unreachable)}


# ----- delivery ------------------------------------------------------------

def herdr(*args, timeout=15):
    binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not binary:
        raise OSError("herdr unavailable")
    result = subprocess.run([binary, *args], stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise OSError("herdr command failed")
    return result.stdout


def nudge(sessions, text):
    """Prompt each session's Herdr pane. Returns (resumed, unreachable)."""
    if not sessions:
        return [], []
    try:
        agents = json.loads(herdr("agent", "list"))["result"]["agents"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return [], list(sessions)
    panes = {}
    for agent in agents if isinstance(agents, list) else []:
        identity = agent.get("agent_session") if isinstance(agent, dict) else None
        if (isinstance(identity, dict) and agent.get("agent") == "claude"
                and isinstance(identity.get("value"), str) and isinstance(agent.get("pane_id"), str)):
            panes[identity["value"].lower()] = agent["pane_id"]
    resumed, unreachable = [], []
    for session in sessions:
        pane = panes.get(session)
        if pane is None:
            unreachable.append(session)
            continue
        try:
            herdr("agent", "prompt", pane, text)
        except (OSError, subprocess.TimeoutExpired):
            try:
                # agent prompt refuses a pane Herdr believes is blocked.
                herdr("pane", "send-text", pane, text)
                herdr("pane", "send-keys", pane, "enter")
            except (OSError, subprocess.TimeoutExpired):
                unreachable.append(session)
                continue
        resumed.append(session)
    return resumed, unreachable


def desktop_notify(message):
    import desktop_notify as notifier
    notifier.send("QuotaHop auto-switch", message)


# ----- Claude Code hook installation -----------------------------------------

def settings_path():
    config = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(config).expanduser() / "settings.json"


def hook_command(data_dir):
    script = Path(__file__).absolute().parent / "quotahop"
    return " ".join(shlex.quote(part) for part in (
        sys.executable, "-B", str(script), "--data-dir", str(Path(data_dir).absolute()))) + " " + HOOK_MARKER


def _ours(handler):
    return isinstance(handler, dict) and isinstance(handler.get("command"), str) and handler["command"].endswith(HOOK_MARKER)


def _read_settings(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    value = live.json_object(raw) if raw.strip() else {}
    return raw, value


def _write_settings(path, before, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if before is not None else 0o600
    temp = path.with_name(f".{path.name}.quotahop-{uuid.uuid4().hex}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        current = path.read_bytes() if path.exists() else None
        if current != before:
            raise live.LiveError("settings_changed", "Claude Code settings changed while editing; nothing written. Retry.")
        os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _strip(hooks):
    groups = hooks.get(HOOK_EVENT, [])
    if not isinstance(groups, list):
        raise live.LiveError("settings_invalid", f"Claude Code settings have an unexpected hooks.{HOOK_EVENT} value; edit it by hand.")
    kept = []
    for group in groups:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            handlers = [h for h in group["hooks"] if not _ours(h)]
            if handlers:
                kept.append({**group, "hooks": handlers})
            elif not group["hooks"]:
                kept.append(group)
        else:
            kept.append(group)
    return kept


def installed(path=None):
    try:
        _, value = _read_settings(path or settings_path())
    except (live.LiveError, OSError):
        return False
    groups = (value.get("hooks") or {}).get(HOOK_EVENT) if isinstance(value.get("hooks"), dict) else None
    return any(_ours(h) for g in groups or [] if isinstance(g, dict) for h in g.get("hooks") or [])


def enable(data_dir, path=None):
    path = path or settings_path()
    before, value = _read_settings(path)
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise live.LiveError("settings_invalid", "Claude Code settings have an unexpected hooks value; edit it by hand.")
    groups = _strip(hooks)
    groups.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": hook_command(data_dir), "timeout": 15}]})
    hooks[HOOK_EVENT] = groups
    _write_settings(path, before, value)
    import login_keyring
    message = (f"Automatic switching enabled: a {HOOK_EVENT} hook in {path} runs when a Claude Code session stops at a usage limit. "
               "Sessions started before this may need a restart to load the hook.")
    if value.get("disableAllHooks"):
        message += " Warning: disableAllHooks is set in these settings, so the hook will not run."
    if not login_keyring.enabled():
        message += (" The hook never prompts: without --background enable-autostart it can only use a vault unlocked in the "
                    "last five minutes; otherwise it queues the sessions until you run --auto-switch run.")
    return {"message": message, "enabled": True, "settings": str(path)}


def disable(path=None):
    path = path or settings_path()
    before, value = _read_settings(path)
    hooks = value.get("hooks")
    if before is None or not isinstance(hooks, dict) or HOOK_EVENT not in hooks:
        return {"message": "Automatic switching was not enabled; nothing changed.", "enabled": False, "settings": str(path)}
    groups = _strip(hooks)
    if groups:
        hooks[HOOK_EVENT] = groups
    else:
        del hooks[HOOK_EVENT]
    if not hooks:
        del value["hooks"]
    _write_settings(path, before, value)
    return {"message": f"Automatic switching disabled; removed QuotaHop's {HOOK_EVENT} hook from {path}.",
            "enabled": False, "settings": str(path)}


def status(store):
    import login_keyring
    with queue_lock(store):
        state = load(store)
    path = settings_path()
    return {"enabled": installed(path), "settings": str(path), "unattended_unlock": login_keyring.enabled(),
            "worker_running": worker_running(store), "pending_sessions": len(state["pending"]),
            "recent_switches": len([t for t in state["switches"] if time.time() - t < SWITCH_WINDOW]),
            "last_switch": state["last_switch"], "last_result": state["last_result"]}


def render_status(result):
    import datetime as dt

    def when(value):
        return dt.datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M") if isinstance(value, (int, float)) else "—"
    lines = [f"Automatic switching: {'enabled' if result['enabled'] else 'disabled'} ({result['settings']})",
             f"Unattended unlock (login keyring): {'yes' if result['unattended_unlock'] else 'no — only within five minutes of an unlock'}",
             f"Worker running: {'yes' if result['worker_running'] else 'no'} · stopped sessions waiting: {result['pending_sessions']}",
             f"Automatic switches in the last {SWITCH_WINDOW // 60} min: {result['recent_switches']} of {MAX_SWITCHES}"]
    if result["last_switch"]:
        lines.append(f"Last switch: {when(result['last_switch'].get('at'))} to {result['last_switch'].get('account', '—')}")
    if result["last_result"]:
        lines.append(f"Last result: {when(result['last_result'].get('at'))} {result['last_result'].get('message', '')}")
    return "\n".join(lines)
