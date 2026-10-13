"""Opt-in foreground quota panel. No detached processes or credential writes."""
import ctypes
import curses
import datetime as dt
import json
import os
import signal
import sys
import time
import unicodedata
import uuid

import claude_usage as app
import credential_state
import live_usage as live
import vault_cache


LOCK_FILE = "panel-lock.json"


def lock_panels(store):
    """Invalidate open panels without PID files or signalling unrelated processes."""
    store.write(LOCK_FILE, {"generation": uuid.uuid4().hex}, replace=True)


def generation(store):
    try:
        value = store.read(LOCK_FILE)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or set(value) != {"generation"} or not isinstance(value["generation"], str):
        raise app.UsageError("Invalid panel lock marker.")
    return value["generation"]


class Source:
    """An explicit panel session may retain a key, never a password, in memory."""

    def __init__(self, path):
        self.path = path
        self.key = None
        with app.Store(path) as store:
            self.generation = generation(store)
        self.locked = False

    def lock(self):
        self.key = None
        self.locked = True

    def check(self):
        with app.Store(self.path) as store:
            if generation(store) != self.generation:
                self.lock()
                return False
        return True

    def load(self, *, prompt=False):
        with app.Store(self.path) as store:
            current = generation(store)
            if current != self.generation:
                self.lock()
            accounts = app.validate_accounts(store.read("accounts.json"))
            try:
                envelope = store.read("vault.json")
            except FileNotFoundError:
                if self.locked and not prompt:
                    raise live.LiveError("vault_locked", "Panel locked. Press u to unlock.")
                self.key = None
                self.generation, self.locked = current, False
                return accounts, {}
            if prompt:
                # An explicit unlock may reuse the normal five-minute cache.
                entries, key, _ = live.unlock_cached(store, envelope)
            else:
                key = self.key or (vault_cache.get(store.fd, envelope) if not self.locked else None)
                if key is None:
                    raise live.LiveError("vault_locked", "Press u to unlock this panel.")
                try:
                    entries, key, _ = live.unlock_vault(envelope, None, key=key)
                except live.LiveError:
                    self.lock()
                    raise
            self.key, self.generation, self.locked = key, current, False
            return accounts, app.account_identity.for_report(entries)


def collect(path, accounts, entries, jobs, sort, reverse):
    with app.Store(path) as store:
        before = credential_state.current(accounts, entries)
        data = app.concurrent_report(accounts, store, dt.datetime.now(app.UTC), entries, jobs)
        data = credential_state.annotate(data, entries, before,
                                         credential_state.current(accounts, entries), dt.datetime.now(app.UTC))
        return app.sort_report(data, sort, reverse)


class Fetch:
    """One cancellable foreground batch; only sanitized report JSON crosses the pipe."""

    def __init__(self, task, *, timeout=90):
        self.pid = None
        self.fd = None
        self.buffer = bytearray()
        self.deadline = time.monotonic() + timeout
        read_fd, write_fd = os.pipe()
        parent = os.getpid()
        try:
            pid = os.fork()
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        if pid == 0:
            try:
                os.close(read_fd)
                os.setpgid(0, 0)
                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    signal.signal(sig, signal.SIG_DFL)
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
                    os._exit(1)
                data = json.dumps(task(), allow_nan=False).encode()
                if len(data) > app.LIMIT:
                    os._exit(1)
                while data:
                    data = data[os.write(write_fd, data):]
                os._exit(0)
            except BaseException:
                os._exit(1)  # No exception/credential text in plugin logs.
        os.close(write_fd)
        self.pid, self.fd = pid, read_fd
        try:
            os.setpgid(pid, pid)
        except (ProcessLookupError, PermissionError):
            pass
        os.set_blocking(read_fd, False)

    def close(self):
        if self.pid is not None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            while True:
                try:
                    os.waitpid(self.pid, 0)
                    break
                except InterruptedError:
                    continue
            self.pid = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def poll(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            data = None
        if data:
            self.buffer.extend(data)
        if len(self.buffer) > app.LIMIT or time.monotonic() >= self.deadline:
            self.close()
            return {"error": "Usage check timed out. Press r to retry."}
        if data == b"":
            self.close()
            try:
                value = live.json_object(self.buffer)
                if not isinstance(value, dict) or not isinstance(value.get("accounts"), list):
                    raise ValueError()
                return value
            except (ValueError, live.LiveError):
                return {"error": "Usage check failed. Press r to retry."}
        return None


def clip(value, width):
    text = "".join(c for c in str(value) if not unicodedata.category(c).startswith("C"))
    if app.display_width(text) <= width:
        return text
    result = ""
    for char in text:
        if app.display_width(result + char) > max(0, width - 1):
            break
        result += char
    return result + ("…" if width else "")


def countdown(reset, now):
    if reset is None:
        return "—"
    seconds = (app.timestamp(reset) - now).total_seconds()
    if seconds <= 0:
        return "due"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{mins:02}"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02}"


def cell(row, key, now):
    item = (row.get("usage") or {}).get(key) or {}
    value = item.get("utilization")
    used = "—" if value is None else f"{value:.0f}%"
    return f"{used:>4} {countdown(item.get('resets_at'), now):>5}"


def remaining(row, key):
    value = ((row.get("usage") or {}).get(key) or {}).get("utilization")
    if value is None:
        return "—"
    left = max(0, 100 - value)
    return "<1%" if 0 < left < 1 else f"{left:.0f}%"


def next_account(rows, active, now):
    """Same ranking as the CLI, restricted to verified usable alternatives."""
    candidates = []
    for row in rows:
        if row["id"] == active["id"] or row.get("selected") is not False:
            continue
        if app.recommendation(row, now)[0] not in (0, 1):
            continue
        expiry = row.get("credential_expires_at")
        if expiry is not None and expiry <= now.timestamp() + 60:
            continue
        if row["provider"] == "claude" and row.get("switch_readiness") != "ready":
            continue
        candidates.append(row)
    return min(candidates, key=lambda row: app.recommendation(row, now), default=None)


def summary_lines(data, width, now):
    result = []
    for provider, title in (("codex", "Codex"), ("claude", "Claude"), ("muse_code", "Muse")):
        rows = [r for r in data["accounts"] if r["provider"] == provider]
        if not rows:
            continue
        if result:
            result.append(("", "normal"))
        result.append((title, "group"))
        selected = [r for r in rows if r.get("selected") is True]
        active = selected[0] if len(selected) == 1 else None
        upcoming = next_account(rows, active, now) if active else None
        # One saved subscription can still show useful quota without falsely
        # claiming that its credentials match the current CLI login.
        saved = rows[0] if active is None and len(rows) == 1 else None
        slots = [("Active", active), ("Saved?" if saved else "Next", saved or upcoming)]
        for label, row in slots:
            if row is not None and row.get("live") and row.get("status") == "live":
                values = [remaining(row, key) for key in ("five_hour", "seven_day")]
                usage = row.get("usage") or {}
                known = [w["utilization"] for w in (usage.get("five_hour"), usage.get("seven_day"))
                         if isinstance(w, dict) and w.get("utilization") is not None]
                style = "high" if known and max(known) >= 90 else "warn" if known and max(known) >= 70 else "selected" if label == "Active" else "normal"
                result.append((f"  {label:<8}{values[0]:>6}{values[1]:>8}", style))
            else:
                text = ("Unavailable" if row else "Unverified" if active is None else
                        "No other sub" if len(rows) == 1 else "None available")
                result.append((f"  {label:<8}{text}", "muted"))
    return [(clip(text, max(0, width)), style) for text, style in result]


def lines(data, width, now, *, details=False):
    if data and data.get("accounts") and not details:
        return summary_lines(data, width, now)
    return detail_lines(data, width, now, details=details)


def detail_lines(data, width, now, *, details=False):
    """Pure, width-bounded rendering. Rows retain the backend's recommendation order."""
    result = []
    if not data or not data.get("accounts"):
        return [("No report yet", "muted"), ("u unlock · r refresh", "muted")]
    for provider, title in app.PROVIDER_GROUPS.items():
        rows = [r for r in data["accounts"] if r["provider"] == provider]
        if not rows:
            continue
        result.append((title.upper() + " · used % / reset", "group"))
        for row in rows:
            marker = "▶" if row.get("selected") is True else "○" if row.get("selected") is False else "?"
            plan = {"max_20x": "Max", "team_premium": "Team", "codex": "Codex", "muse_code": "Muse"}[row["plan"]]
            name = f"{marker} {row['name']}"
            usage = row.get("usage") or {}
            used = [w.get("utilization") for w in (usage.get("five_hour"), usage.get("seven_day")) if w]
            used = [v for v in used if v is not None]
            style = "selected" if row.get("selected") is True else "high" if used and max(used) >= 90 else "warn" if used and max(used) >= 70 else "normal"
            status = row.get("status")
            if status not in ("live", "snapshot"):
                label = "SIGN IN" if row.get("error_code") == "authentication_error" else "ERROR" if status == "error" else "SETUP" if status == "not_configured" else "UNSUPPORTED"
                result.append((f"{name} · {plan}", "high"))
                result.append(("  " + label, "high"))
            else:
                prefix = "◷ " if status == "snapshot" else ""
                # Two short lines keep full subscription labels and more of the
                # email visible. At 44 columns nine accounts fit in ~30 rows.
                suffix = " · " + plan + (" ✓" if row.get("switch_readiness") == "ready" else "")
                label = clip(prefix + name, max(1, width - app.display_width(suffix))) + suffix
                result.append((label, style))
                result.append(("  5h " + cell(row, "five_hour", now) + "  7d " + cell(row, "seven_day", now), style))
            if details:
                result.extend((s, "normal") for s in app.wrap_cells(row["name"], max(1, width)))
                result.append(("  " + (row.get("reported_plan") or app.PLANS[row["plan"]]), "muted"))
                for key, title_ in (("five_hour", "5h"), ("seven_day", "7d")):
                    reset = (usage.get(key) or {}).get("resets_at")
                    if reset:
                        result.append((f"  {title_} ↻ {app.timestamp(reset).astimezone():%d %b %H:%M %Z}", "muted"))
                if row.get("message") and status != "live":
                    result.extend((s, "warn") for s in app.wrap_cells(row["message"], max(1, width)))
    return [(clip(text, max(0, width)), style) for text, style in result]


def run(path, *, interval=60, jobs=4, sort="recommended", reverse=False, color=True):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise app.UsageError("--panel requires an interactive terminal. Use --json for scripts.")
    if not 30 <= interval <= 3600:
        raise app.UsageError("Panel interval must be 30–3600 seconds.")
    source = Source(path)
    previous = {}

    def interrupted(*_):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, interrupted)
        return curses.wrapper(_screen, source, interval, jobs, sort, reverse, color)
    except curses.error:
        raise app.UsageError("Cannot initialize the panel terminal. Check TERM and terminal size.") from None
    finally:
        source.lock()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _screen(screen, source, interval, jobs, sort, reverse, color):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(200)
    styles = {"normal": 0, "muted": curses.A_DIM, "group": curses.A_BOLD,
              "selected": curses.A_BOLD, "warn": 0, "high": curses.A_BOLD}
    if color and curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            for i, (role, fg) in enumerate((("group", curses.COLOR_MAGENTA), ("selected", curses.COLOR_CYAN),
                                            ("warn", curses.COLOR_YELLOW), ("high", curses.COLOR_RED)), 1):
                curses.init_pair(i, fg, -1)
                styles[role] |= curses.color_pair(i)
        except curses.error:
            pass
    data, fetch = None, None
    due, last_start, checked = 0, -100, 0
    paused, details, scroll = False, False, 0
    message = "Opening vault…"
    try:
        while True:
            now = time.monotonic()
            # Check a local lock marker even while a network batch is active.
            if now - checked >= 1:
                checked = now
                try:
                    if not source.check():
                        if fetch:
                            fetch.close()
                            fetch = None
                        data, due, message = None, float("inf"), "Locked · u unlock"
                except (app.UsageError, live.LiveError, OSError, ValueError):
                    source.lock()
                    if fetch:
                        fetch.close()
                        fetch = None
                    data, due, message = None, float("inf"), "Vault unavailable · u retry"
            if fetch:
                result = fetch.poll()
                if result is not None:
                    fetch = None
                    due = now + interval
                    if result.get("error"):
                        message = result["error"]
                    else:
                        data, message = result, ""
            if not paused and fetch is None and now >= due:
                try:
                    accounts, entries = source.load()
                    fetch = Fetch(lambda: collect(source.path, accounts, entries, jobs, sort, reverse))
                    del entries  # Only this batch's child retains the credentials.
                    last_start, message = now, "Checking…"
                except (app.UsageError, live.LiveError, OSError, ValueError):
                    source.lock()
                    data, due, message = None, float("inf"), "Locked / unavailable · u unlock"
            screen.erase()
            height, width = screen.getmaxyx()
            width = max(0, width - 1)  # Never write the terminal's wrap column.
            utc = dt.datetime.now(app.UTC)
            body = lines(data, width, utc, details=details)
            footer_count = 3 if details else 1
            visible = max(0, height - 2 - footer_count)
            scroll = min(max(0, scroll), max(0, len(body) - visible))
            title = "QUOTAHOP · remaining" if not details else "QUOTAHOP · all accounts"
            age = (utc - app.timestamp(data["generated_at"])).total_seconds() if data else None
            status = message if message != "Checking…" or not data else ""
            status = status or ("Paused" if paused else "Locked · u unlock" if due == float("inf")
                               else "              5h      7d" if not details
                               else f"Next check {max(0, int(due - now))}s")
            if age is not None and (details or paused or message not in ("", "Checking…")):
                status += " · checked " + app.duration(age) + " ago"
            elif age is not None and age > max(120, interval * 2):
                status = "Stale · checked " + app.duration(age) + " ago"
            content = [(title, "group"), (status, "warn" if message or paused else "muted")]
            content += body[scroll:scroll + visible]
            for y, (text, style) in enumerate(content[:max(0, height - footer_count)]):
                try:
                    screen.addstr(y, 0, clip(text, width), styles[style])
                except curses.error:
                    pass
            footers = ["▶ selected ○ standby ? unknown ✓ ready", "r refresh  p pause  d details  ↑↓ scroll",
                       "u unlock · l lock · q close"]
            if not details:
                footers = ["d details · r refresh"]
            if details and len(body) > visible:
                footers[1] = f"↑↓ {scroll + 1}–{min(len(body), scroll + visible)}/{len(body)} · r refresh · d details"
            for y, text in enumerate(footers, max(0, height - footer_count)):
                try:
                    screen.addstr(y, 0, clip(text, width), curses.A_DIM)
                except curses.error:
                    pass
            screen.refresh()
            key = screen.getch()
            if key in (ord('q'), 27):
                return 0
            if key in (ord('j'), curses.KEY_DOWN):
                scroll += 1
            elif key in (ord('k'), curses.KEY_UP):
                scroll -= 1
            elif key == curses.KEY_NPAGE:
                scroll += max(1, visible)
            elif key == curses.KEY_PPAGE:
                scroll -= max(1, visible)
            elif key == ord('d'):
                details, scroll = not details, 0
            elif key == ord('p'):
                paused = not paused
                if paused and fetch:
                    fetch.close()
                    fetch = None
                message = "Paused" if paused else ""
            elif key == ord('r') and fetch is None and now - last_start >= 5:
                due, paused = 0, False
            elif key == ord('l'):
                if fetch:
                    fetch.close()
                    fetch = None
                source.lock()
                data, due, message = None, float("inf"), "Locked · u unlock"
            elif key == ord('u'):
                if fetch:
                    fetch.close()
                    fetch = None
                curses.def_prog_mode()
                curses.endwin()
                try:
                    print("Panel keeps an unlock in memory until l, q, or quotahop --lock. No auth renewal.")
                    source.load(prompt=True)
                    due, paused, message = 0, False, ""
                except (app.UsageError, live.LiveError, OSError, ValueError):
                    source.lock()
                    data, due, message = None, float("inf"), "Unlock failed · u retry"
                finally:
                    curses.reset_prog_mode()
                    screen.clear()
                    screen.timeout(200)
    finally:
        if fetch:
            fetch.close()
        source.lock()
