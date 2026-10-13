"""One-shot subscription report with encrypted, access-token-only captures."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import unicodedata
import uuid

import live_usage
import muse_usage
import account_identity

UTC = dt.timezone.utc
LIMIT = 1024 * 1024
PLANS = {"max_20x": "Max 20×", "team_premium": "Team Premium",
         "codex": "Codex subscription", "muse_code": "Muse Code"}
PROVIDERS = {"max_20x": "claude", "team_premium": "claude", "codex": "codex", "muse_code": "muse_code"}
PROVIDER_GROUPS = {"codex": "OpenAI", "claude": "Anthropic", "muse_code": "Meta"}
SORT_COLUMNS = ("recommended", "name", "plan", "5h", "7d", "5h-reset", "7d-reset", "status")
UNSUPPORTED = (
    "No saved access token for this account. While it is your current Claude Code or Codex "
    "account, run --capture claude --plan team_premium (or max_20x), or --capture codex. No login or refresh is performed."
)
READ_ONLY_ENDPOINTS = live_usage.READ_ONLY_ENDPOINTS
UsageClient = live_usage.UsageClient


class UsageError(Exception):
    """Only fixed, non-sensitive messages may be supplied."""


def clean_text(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise UsageError("Names must contain 1–80 characters.")
    if any(unicodedata.category(c).startswith("C") for c in value):
        raise UsageError("Control characters are not allowed in names.")
    return value.strip()


def reject_secrets(value):
    """Reject credential-shaped input, never echo offending keys or values."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z]", "", key.lower())
            if any(s in normalized for s in (
                "token", "password", "secret", "cookie", "authorization",
                "credential", "sessionkey", "apikey",
            )):
                raise UsageError("Credential fields are prohibited; supply usage data only.")
            reject_secrets(key)
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str):
        if re.search(r"sk-ant-|\bBearer\s|\beyJ[A-Za-z0-9_-]+\.", value, re.I):
            raise UsageError("Credential-like content is prohibited.")


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        if len(raw) > LIMIT:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        reject_secrets(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise UsageError("Invalid JSON, duplicate fields, or input larger than 1 MiB.") from None


def timestamp(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise UsageError("Reset and capture timestamps must be ISO 8601 with a timezone.") from None


def number(value, percentage=False):
    if value is None:
        return None
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise UsageError("Usage values must be finite, non-negative numbers or null.")
    if percentage and value > 100:
        raise UsageError("Usage percentages must be between 0 and 100.")
    return value


def window(value):
    if value is None:
        return {"utilization": None, "resets_at": None}
    if not isinstance(value, dict):
        raise UsageError("A usage window must be an object or null.")
    reset = value.get("resets_at")
    return {"utilization": number(value.get("utilization"), percentage=True),
            "resets_at": timestamp(reset).isoformat() if reset is not None else None}


def parse_usage(value):
    """Parse a manually supplied usage response; never interpret tokens as limits."""
    if not isinstance(value, dict):
        raise UsageError("Usage must be a JSON object.")
    reject_secrets(value)
    result = {name: window(value.get(name)) for name in ("five_hour", "seven_day")}
    result["model_windows"] = {
        key: window(item) for key, item in value.items()
        if re.fullmatch(r"(?:five_hour|seven_day)_[a-z0-9_]{1,40}", key)
    }
    extra = value.get("extra_usage")
    result["extra_usage"] = None
    if extra is not None:
        if not isinstance(extra, dict) or type(extra.get("is_enabled")) not in (bool, type(None)):
            raise UsageError("Extra usage must contain a boolean is_enabled or null.")
        result["extra_usage"] = {
            "is_enabled": extra.get("is_enabled"),
            "utilization": number(extra.get("utilization")),
            "used_credits": number(extra.get("used_credits")),
            "monthly_limit": number(extra.get("monthly_limit")),
        }
    return result


def epoch(value, milliseconds=False):
    if value is None:
        return None
    value = number(value)
    try:
        return dt.datetime.fromtimestamp(value / (1000 if milliseconds else 1), UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        raise UsageError("Invalid epoch timestamp in usage data.") from None


def parse_codex(value):
    if not isinstance(value, dict):
        raise UsageError("Codex usage must be an object.")
    reject_secrets(value)
    if not any(k in value for k in ("rate_limit", "additional_rate_limits", "credits", "code_review_rate_limit")):
        raise UsageError("No recognized Codex subscription limits returned.")
    result = parse_usage({})
    result["credits"] = None

    def limits(raw, label, main=False):
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise UsageError("Invalid Codex rate-limit block.")
        for slot in ("primary_window", "secondary_window"):
            item = raw.get(slot)
            if item is None:
                continue
            if not isinstance(item, dict):
                raise UsageError("Invalid Codex usage window.")
            seconds = number(item.get("limit_window_seconds"))
            parsed = {"utilization": number(item.get("used_percent"), percentage=True),
                      "resets_at": epoch(item.get("reset_at"))}
            target = {18000: "five_hour", 604800: "seven_day"}.get(seconds)
            if main and target and result[target] == window(None):
                result[target] = parsed
            else:
                span = "duration unknown" if seconds is None else f"{seconds / 60:g}m"
                result["model_windows"][f"{label} {slot} ({span})"] = parsed

    limits(value.get("rate_limit"), "Codex", main=True)
    limits(value.get("code_review_rate_limit"), "Code review")
    additional = value.get("additional_rate_limits") or []
    if not isinstance(additional, list) or len(additional) > 100:
        raise UsageError("Invalid additional Codex limits.")
    for i, item in enumerate(additional):
        if not isinstance(item, dict):
            raise UsageError("Invalid additional Codex limit.")
        label = clean_text(item.get("limit_name") or item.get("metered_feature") or f"Allowance {i + 1}")
        limits(item.get("rate_limit"), f"{label} [{i + 1}]")
    credits = value.get("credits")
    if credits is not None:
        if not isinstance(credits, dict):
            raise UsageError("Invalid Codex credits.")
        result["credits"] = {}
        for key in ("has_credits", "unlimited"):
            flag = credits.get(key)
            if flag is not None and type(flag) is not bool:
                raise UsageError("Invalid Codex credit flag.")
            result["credits"][key] = flag
        balance = credits.get("balance")
        if isinstance(balance, str):
            if not re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,10})?", balance):
                raise UsageError("Invalid Codex credit balance.")
        else:
            number(balance)
        result["credits"]["balance"] = balance
    return result


def muse_snapshot(value):
    """Accept an already-exported MSP usage/read result, never launch a host."""
    if not isinstance(value, dict):
        raise UsageError("Expected a Muse usage/read result object.")
    reject_secrets(value)
    usage = value.get("usage")
    if not isinstance(usage, dict) or usage.get("observedAtMs") is None:
        raise UsageError("Muse has no timestamped subscription observation in this result.")
    raw = {}
    for key, dest in (("window", "five_hour"), ("weekly", "seven_day")):
        item = usage.get(key)
        if item is None:
            continue
        if not isinstance(item, dict):
            raise UsageError("Invalid Muse usage window.")
        if key == "window" and item.get("windowDurationMins") != 300:
            raise UsageError("Muse window is not five hours; cannot label it as a 5h snapshot.")
        raw[dest] = {"utilization": item.get("usedPercent"),
                     "resets_at": epoch(item.get("resetsAtMs"), milliseconds=True)}
    if not raw:
        raise UsageError("Muse observation contains no usage windows.")
    snapshot = {"captured_at": epoch(usage["observedAtMs"], milliseconds=True), "usage": raw}
    if usage.get("tier") is not None:
        snapshot["reported_plan"] = clean_text(usage["tier"])
    return validate_snapshot(snapshot)


def parse_muse_portal(value, now=None):
    """Weighted subscription counters from the selected Meta portal team."""
    from decimal import Decimal
    now = now or dt.datetime.now(UTC)
    if not isinstance(value, dict):
        raise UsageError("Meta subscription quota must be an object.")
    reject_secrets(value)
    quota = value.get("subscription_quota")
    if quota is None:
        raise live_usage.LiveError("no_subscription", "Selected Meta team has no subscription quota; API spending is not substituted.")
    if not isinstance(quota, dict):
        raise UsageError("Invalid Meta subscription-quota response.")

    def amount(raw):
        if raw is None:
            return None
        if isinstance(raw, str) and re.fullmatch(r"[0-9]{1,80}", raw):
            return Decimal(raw)
        if type(raw) is int and 0 <= raw < 10**80:
            return Decimal(raw)
        raise UsageError("Meta weighted counters must be non-negative integers or decimal integer strings.")

    result = parse_usage({})
    result["reported_plan"] = clean_text(quota["tier"]) if quota.get("tier") is not None else None
    result["notes"] = []
    for prefix, target in (("window", "five_hour"), ("weekly", "seven_day")):
        used = amount(quota.get(prefix + "_weighted_used"))
        limit = amount(quota.get(prefix + "_weighted_limit"))
        reset = amount(quota.get(prefix + "_resets_at"))
        parsed = window(None)
        if used is not None and limit is not None and limit > 0:
            # Preserve the reported ratio, including overage; never clamp to 100.
            parsed["utilization"] = float(used * 100 / limit)
        elif limit == 0:
            result["notes"].append(prefix + ": zero allowance; percentage unknown.")
        if reset is not None:
            parsed["resets_at"] = epoch(int(reset))
            if timestamp(parsed["resets_at"]) <= now:
                # Do not assume stale nonzero counters have reset to zero.
                parsed = window(None)
                result["notes"].append(prefix + ": expired reset; current usage withheld.")
        elif used not in (None, Decimal(0)):
            parsed = window(None)
            result["notes"].append(prefix + ": nonzero usage without a reset; current usage withheld.")
        if prefix == "window":
            seconds = amount(quota.get("window_duration_secs"))
            if seconds != 18000:
                span = "duration unknown" if seconds is None else f"{seconds / 60:g}m"
                result["model_windows"][f"Muse subscription ({span})"] = parsed
                continue
        result[target] = parsed
    return result


def validate_snapshot(value):
    if (not isinstance(value, dict) or not {"captured_at", "usage"} <= set(value)
            or set(value) - {"captured_at", "usage", "reported_plan"}):
        raise UsageError("Snapshot requires captured_at and usage; only reported_plan is optional.")
    captured = timestamp(value["captured_at"])
    if captured > dt.datetime.now(UTC) + dt.timedelta(minutes=1):
        raise UsageError("Snapshot capture time is in the future.")
    parsed = parse_usage(value["usage"])
    # Store only accepted, non-sensitive metrics, not the original response.
    usage = {k: parsed[k] for k in ("five_hour", "seven_day", "extra_usage")}
    usage.update(parsed["model_windows"])
    result = {"captured_at": captured.isoformat(), "usage": usage}
    if value.get("reported_plan") is not None:
        reject_secrets(value["reported_plan"])
        result["reported_plan"] = clean_text(value["reported_plan"])
    return result


def default_accounts():
    return [{"id": f"max-{i}", "name": f"Max {i}", "plan": "max_20x"}
            for i in range(1, 3)] + [
        {"id": f"team-{i}", "name": f"Team {i}", "plan": "team_premium"}
        for i in range(1, 6)]


def validate_accounts(value):
    if not isinstance(value, dict) or set(value) != {"accounts"}:
        raise UsageError("Configuration must contain only an accounts array.")
    accounts = value["accounts"]
    if not isinstance(accounts, list) or len(accounts) > 100:
        raise UsageError("Configure up to 100 accounts.")
    seen = set()
    current_muse = False
    for account in accounts:
        if (not isinstance(account, dict) or not {"id", "name", "plan"} <= set(account)
                or set(account) - {"id", "name", "plan", "source"}):
            raise UsageError("Each account requires id, name, and plan; only source is optional, never credentials.")
        ident = account["id"]
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", ident):
            raise UsageError("Account IDs must use lowercase letters, digits, and hyphens.")
        if ident in {"accounts", "vault", "vault-rekey", "switch-backup", "renewal", "renewal-status", "renewal-stop", "renewal-lock"} or ident in seen:
            raise UsageError("Account IDs must be unique and cannot use reserved storage names.")
        seen.add(ident)
        account["name"] = clean_text(account["name"])
        if not isinstance(account["plan"], str) or account["plan"] not in PLANS:
            raise UsageError("Plan must be max_20x, team_premium, codex, or muse_code.")
        if "source" in account:
            if account["plan"] != "muse_code" or account["source"] != "cli_current":
                raise UsageError("Only Muse Code supports source: cli_current.")
            if current_muse:
                raise UsageError("Only one Muse row may use the current CLI login; it cannot represent multiple accounts.")
            current_muse = True
    return accounts


class Store:
    """Owned private directory, descriptor-relative access, no symlink following."""

    def __init__(self, path, create=False, *, maintenance=False):
        self.maintenance = maintenance
        path = Path(os.path.abspath(path))
        if any(p in {".claude", "claude", ".aws", ".codex", "codex", "muse", ".muse", ".git"} for p in path.parts):
            raise UsageError("Data directory cannot be inside a protected application directory.")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, component in enumerate(path.parts[1:]):
                final = index == len(path.parts) - 2
                if final and create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise UsageError("Data directory must be owned by you with permissions 0700.")
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    @staticmethod
    def check_name(name):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}\.json", name):
            raise UsageError("Invalid storage filename.")

    def read(self, name):
        self.check_rekey()
        self.check_name(name)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise UsageError("Data files must be private regular files (0600), without hard links.")
            return decode(stream.read(LIMIT + 1))

    def write(self, name, value, replace=False):
        self.check_rekey()
        self.check_name(name)
        temp = ".tmp-" + uuid.uuid4().hex
        payload = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                # Replacing a link changes only our directory entry, never its target.
                os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            else:
                os.link(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                        follow_symlinks=False)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def delete(self, name):
        self.check_rekey()
        self.check_name(name)
        try:
            # Unlink only our directory entry; never follow a symlink to its target.
            os.unlink(name, dir_fd=self.fd)
            os.fsync(self.fd)
        except FileNotFoundError:
            pass

    def check_rekey(self):
        if self.maintenance:
            return
        try:
            os.stat("vault-rekey.json", dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise UsageError("A password change is unfinished. Run --change-password and enter the NEW password to finish it; other vault operations are blocked.")


def duration(seconds):
    if seconds <= 0:
        return "elapsed"
    if seconds < 60:
        return "<1m"
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def reset_text(value, now):
    if value is None:
        return "—"
    reset = timestamp(value)
    return f"{duration((reset - now).total_seconds())} · {reset.astimezone():%d %b %H:%M %Z}"


def percent(value):
    return "—" if value is None else f"{value:g}%"


def report(accounts, store, now, entries=None):
    results = []
    entries = entries or {}
    seen = set()
    for account in accounts:
        provider = PROVIDERS[account["plan"]]
        if provider == "muse_code" and account.get("source") == "cli_current" and entries.get(account["id"], {}).get("kind") != "cli_owned":
            import muse_cli_usage
            current = muse_cli_usage.report(now)["accounts"][0]
            current.update({k: v for k, v in account.items() if k != "name"})
            if not current.get("identity"):
                current["name"] = account["name"]
            results.append(current)
            continue
        row = {**account, "plan_verified": False, "live": False, "captured_at": None,
               "provider": provider, "reported_plan": None,
               "status": "not_configured", "message": UNSUPPORTED, "usage": None}
        identity = account_identity.saved(entries.get(account["id"], {}))
        if identity:
            row.update(identity=identity, name=account_identity.label(identity))
        owned_muse = provider == "muse_code" and entries.get(account["id"], {}).get("kind") == "cli_owned"
        if owned_muse:
            row.update(quota_scope="owned_muse_cli_login", cli_account_verified=True)
        elif provider == "muse_code":
            row.update(message="Run --capture with this account ID to save a Meta portal session and explicitly select its subscription team.",
                       quota_scope="selected_meta_portal_team", cli_account_verified=False)
        try:
            if account["id"] in entries:
                raw = live_usage.fetch(entries[account["id"]], seen, provider)
                if owned_muse:
                    import muse_cli_usage
                    parsed, reported_plan = muse_cli_usage.parse(raw)
                else:
                    parsed = (parse_muse_portal(raw) if provider == "muse_code" else
                          parse_codex(raw) if provider == "codex" else parse_usage(raw))
                if provider == "claude" and not any(key in raw for key in ("five_hour", "seven_day", "extra_usage")):
                    raise UsageError("Usage endpoint response does not contain recognized subscription metrics.")
                if provider == "codex" and raw.get("plan_type") is not None:
                    row.update(reported_plan=clean_text(raw["plan_type"]), plan_verified=True)
                if provider == "muse_code":
                    if owned_muse:
                        row.update(reported_plan=reported_plan, quota_scope="owned_muse_cli_login", cli_account_verified=True)
                    else:
                        row.update(reported_plan=parsed.pop("reported_plan"),
                                   portal_team=entries[account["id"]]["team_name"])
                    if raw.get("identity"):
                        row.update(identity=raw["identity"], name=account_identity.label(raw["identity"]))
                    row["plan_verified"] = row["reported_plan"] is not None
                row.update(status="live", live=True,
                           message=(muse_cli_usage.NOTICE if owned_muse else muse_usage.NOTICE if provider == "muse_code" else
                                    live_usage.CODEX_NOTICE if provider == "codex" else live_usage.NOTICE),
                           captured_at=dt.datetime.now(UTC).isoformat(), usage=parsed)
            else:
                snapshot = validate_snapshot(store.read(account["id"] + ".json"))
                row.update(status="snapshot", message="Manual snapshot; not a live check.",
                           reported_plan=snapshot.get("reported_plan"),
                           captured_at=snapshot["captured_at"], usage=parse_usage(snapshot["usage"]))
        except FileNotFoundError:
            pass
        except UsageError as exc:
            row.update(status="error", message=str(exc))
        except live_usage.LiveError as exc:
            row.update(status="error", error_code=exc.code, message=str(exc))
        except OSError:
            row.update(status="error", message="Unable to read private snapshot.")
        results.append(row)
    return {"schema_version": 1, "generated_at": now.isoformat(),
            "live": bool(results) and all(row["live"] for row in results),
            "live_accounts": sum(row["live"] for row in results),
            "accounts": results}


def concurrent_report(accounts, store, now, entries, jobs=4):
    """Keep duplicate credentials in one worker so existing deduplication applies."""
    groups, local = {}, []
    for account in accounts:
        entry = entries.get(account["id"])
        if account.get("source") == "cli_current" and (entry or {}).get("kind") != "cli_owned":
            key = ("muse-current",)
        elif entry is None:
            local.append(account)
            continue
        elif entry.get("provider") == "muse_code" and entry.get("kind") != "cli_owned":
            key = ("portal", entry["session_cookie"], entry["team_id"])
        else:
            key = ("token", entry.get("access_token"))
        groups.setdefault(key, []).append(account)
    if jobs == 1 or len(groups) < 2:
        return report(accounts, store, now, entries)
    import parallel_usage
    from functools import partial
    rows = report(local, store, now, entries)["accounts"] if local else []
    batches = list(groups.values())
    tasks = [partial(report, batch, store, now, {a["id"]: entries[a["id"]] for a in batch if a["id"] in entries})
             for batch in batches]
    results = parallel_usage.run(tasks, workers=jobs, timeout=2 * live_usage.TIMEOUT + 2)
    for batch, result in zip(batches, results):
        if result is not None:
            rows.extend(result["accounts"])
            continue
        for account in batch:
            identity = account_identity.saved(entries.get(account["id"], {}))
            rows.append({**account, "name": account_identity.label(identity) or account["name"],
                         "provider": PROVIDERS[account["plan"]], "status": "error", "live": False,
                         "plan_verified": False, "reported_plan": None, "captured_at": None, "usage": None,
                         "error_code": "worker_failed", "message": "Usage check timed out or its worker stopped. No retry performed."})
    by_id = {row["id"]: row for row in rows}
    rows = [by_id[account["id"]] for account in accounts]
    return {"schema_version": 1, "generated_at": now.isoformat(), "accounts": rows,
            "live": bool(rows) and all(row["live"] for row in rows),
            "live_accounts": sum(row["live"] for row in rows)}


def recommendation(row, now):
    """Heuristic priority from observed main windows, never inferred quota resets.

    Percentages across plans are relative headroom, not comparable token counts.
    A one-hour offset avoids favouring a nearly empty account just because its
    reset is seconds away. Missing windows are ignored (e.g. weekly-only Codex).
    """
    name = row["name"].casefold()
    if not row.get("live") or row["status"] != "live":
        return (3, 0, 0, name)
    usage = row.get("usage") or {}
    windows = [usage[key] for key in ("five_hour", "seven_day")
               if usage.get(key) and usage[key].get("utilization") is not None]
    if not windows:
        return (3, 0, 0, name)
    remaining = min(max(0, 100 - window["utilization"]) for window in windows)

    def seconds(window):
        reset = window.get("resets_at")
        return (timestamp(reset) - now).total_seconds() if reset is not None else None

    blocked = [window for window in windows if window["utilization"] >= 100]
    if blocked:
        resets = [seconds(window) for window in blocked]
        # Every exhausted window must reset before the account is usable.
        wait = max(resets) if all(s is not None and s > 0 for s in resets) else float("inf")
        return (2, wait, 0, name)
    resets = [seconds(window) for window in windows]
    if not all(s is not None and s > 0 for s in resets):
        # Live headroom is still useful, but its expiry urgency is unknown.
        return (1, -remaining, 0, name)
    soonest_hours = min(resets) / 3600
    score = remaining / (1 + soonest_hours)
    return (0, -score, -remaining, name)


def sort_report(data, column="recommended", reverse=False, *, now=None):
    """Sort within provider groups; missing values stay last in either direction."""
    now = now or (timestamp(data["generated_at"]) if data.get("generated_at") else dt.datetime.now(UTC))
    def value(row):
        if column == "name":
            return row["name"].casefold()
        if column == "plan":
            return (row.get("reported_plan") or PLANS[row["plan"]]).casefold()
        if column == "status":
            return row["status"]
        window_name = "five_hour" if column.startswith("5h") else "seven_day"
        window = (row.get("usage") or {}).get(window_name) or {}
        if column.endswith("-reset"):
            reset = window.get("resets_at")
            return timestamp(reset) if reset is not None else None
        return window.get("utilization")

    ordered = []
    for provider in PROVIDER_GROUPS:
        group = [row for row in data["accounts"] if row["provider"] == provider]
        if column == "recommended":
            ranked = [(recommendation(row, now), row) for row in group]
            for tier in range(4):
                ordered.extend(row for _, row in sorted(
                    [item for item in ranked if item[0][0] == tier],
                    key=lambda item: item[0][1:], reverse=reverse))
            continue
        known = [row for row in group if value(row) is not None]
        missing = [row for row in group if value(row) is None]
        ordered.extend(sorted(known, key=value, reverse=reverse))
        ordered.extend(missing)
    return {**data, "accounts": ordered}


def color_enabled(mode):
    return mode == "always" or (mode == "auto" and sys.stdout.isatty()
                                and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")


def display_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def wrap_cells(text, width):
    """Wrap by terminal cells (including CJK/combining characters), not bytes."""
    result, line, used = [], '', 0
    for char in text:
        size = display_width(char)
        if used + size > width and line:
            space = line.rfind(' ')
            if space >= width // 2:
                result.append(line[:space])
                line = line[space + 1:]
                used = display_width(line)
            else:
                result.append(line)
                line, used = '', 0
        line += char
        used += size
    return result + [line]


def render(data, now, *, verbose=False, color=False, width=None):
    width = max(12, width if width is not None else shutil.get_terminal_size((100, 24)).columns)
    def paint(text, code):
        return f"\033[{code}m{text}\033[0m" if color else text

    def usage_color(value):
        return "90" if value is None else "31" if value >= 80 else "33" if value >= 50 else "32"

    status_styles = {"live": ("●", "32"), "error": ("✕", "31"),
                     "snapshot": ("◷", "36"), "not_configured": ("○", "90"),
                     "unsupported": ("!", "33")}
    rows = [["Account", "Plan*" if verbose else "Plan", "5h Used", "↻ 5h Reset (local)", "7d Used",
             "↻ 7d Reset (local)", "Status"]]
    show_selection = data.get("selection_checked", False)
    if show_selection:
        rows[0] += ["CLI", "Switch"]
    styles = []
    for row in data["accounts"]:
        usage = row["usage"] or parse_usage({})
        five, seven = usage["five_hour"], usage["seven_day"]
        label = PLANS[row["plan"]] + ("*" if verbose else "")
        if row.get("reported_plan"):
            provider_name = {"claude": "Claude", "codex": "Codex", "muse_code": "Muse"}[row["provider"]]
            reported = row["reported_plan"]
            label = (reported if reported.startswith(provider_name + " ") else provider_name + " " + reported) + (" (saved)" if not row["live"] else "")
        icon, status_color = status_styles[row["status"]]
        rows.append([row["name"], label, percent(five["utilization"]),
                     reset_text(five["resets_at"], now), percent(seven["utilization"]),
                     reset_text(seven["resets_at"], now), icon + " " + row["status"].upper()])
        styles.append(["1", "90", usage_color(five["utilization"]), "90",
                       usage_color(seven["utilization"]), "90", status_color])
        if show_selection:
            selected = row.get("selected")
            state = row.get("switch_readiness", "not_switchable")
            readiness = {"ready": "✓ Ready", "expired": "! Expired", "expiring": "! Expiring",
                         "expiry_unknown": "? Expiry", "scope_missing": "! Scope", "unverified": "? Check",
                         "identity_unverified": "? Identity",
                         "exhausted": "◷ At limit", "usage_unknown": "? Usage", "not_captured": "○ Capture",
                         "not_switchable": "—"}[state]
            if state == "ready" and row.get("credential_expires_at"):
                readiness += " · " + duration(row["credential_expires_at"] - now.timestamp())
            rows[-1] += ["▶ SELECTED" if selected is True else "○ Standby" if selected is False else "? Unknown", readiness]
            styles[-1] += ["1;36;7" if selected is True else "90", "32" if state == "ready" else "90" if state == "not_switchable" else "33"]
            if selected is True:
                styles[-1][0] = "1;36"
    widths = [max(display_width(row[i]) for row in rows) for i in range(len(rows[0]))]
    compact = sum(widths) + 3 * len(widths) + 1 > width
    stacked = compact and width < 76
    if compact and not stacked:
        compact_rows = [['Account', 'Plan', '5h · ↻', '7d · ↻', 'State']]
        compact_styles = []
        for account, cells, codes in zip(data['accounts'], rows[1:], styles):
            usage = account['usage'] or parse_usage({})
            windows = []
            for key in ('five_hour', 'seven_day'):
                item = usage[key]
                reset = item['resets_at']
                remaining = duration((timestamp(reset) - now).total_seconds()) if reset else '—'
                windows.append(percent(item['utilization']) + ' · ' + remaining)
            marker = ('▶ ' if account.get('selected') is True else '○ ' if account.get('selected') is False else '? ') if show_selection else ''
            state = cells[6]
            readiness = account.get('switch_readiness')
            if show_selection and account['status'] == 'live' and readiness == 'ready':
                state = '● LIVE ✓'
            elif show_selection and account['status'] == 'error' and readiness in ('expired', 'expiring'):
                state = '✕ ' + readiness.capitalize()
            if account['status'] == 'not_configured':
                state = '○ Sign in'
            compact_rows.append([marker + cells[0], cells[1], *windows, state])
            compact_styles.append(['1;36;7' if account.get('selected') is True else codes[0], codes[1], codes[2], codes[4], codes[6]])
        rows, styles = compact_rows, compact_styles
        # At 80 columns the table uses 24/10/10/10/10 cells plus borders.
        # Wider terminals give identity, plan and reset columns more room.
        widths = [20, 10, 10, 10, 10]
        spare = width - (sum(widths) + 16)
        for index, cap in ((0, 34), (1, 20), (2, 17), (3, 17), (4, 16)):
            extra = min(spare, max(0, min(cap, max(display_width(r[index]) for r in rows)) - widths[index]))
            widths[index] += extra
            spare -= extra
    lines = ["Subscription usage · Claude / Codex / Muse Code", ""] if verbose else []
    def table_row(row, codes):
        wrapped = [wrap_cells(cell, cell_width) for cell, cell_width in zip(row, widths)]
        return '\n'.join(table_line([parts[line] if line < len(parts) else '' for parts in wrapped], codes)
                         for line in range(max(map(len, wrapped))))

    def table_line(row, codes):
        cells = []
        for index, (cell, width, code) in enumerate(zip(row, widths, codes)):
            padding = " " * (width - display_width(cell))
            padded = padding + cell if not compact and index in (2, 4) else cell + padding
            cells.append(" " + paint(padded, code) + " ")
        edge = paint("│", "90")
        return edge + edge.join(cells) + edge

    def border(left, join, right):
        return paint(left + join.join("─" * (width + 2) for width in widths) + right, "90")

    for provider, title in PROVIDER_GROUPS.items():
        group = [(cells, codes) for account, cells, codes in zip(data["accounts"], rows[1:], styles)
                 if account["provider"] == provider]
        if not group:
            continue
        if lines and lines[-1]:
            lines.append("")
        provider_color = {"codex": "1;36", "claude": "1;33", "muse_code": "1;34"}[provider]
        if stacked:
            title_text = ('─ ' + title + ' ')[:width - 2]
            lines.append(paint('╭' + title_text + '─' * (width - 2 - display_width(title_text)) + '╮', provider_color))
            for offset, (cells, codes) in enumerate(group):
                if offset:
                    lines.append(paint('├' + '─' * (width - 2) + '┤', '90'))
                content = [(cells[0], codes[0]), (cells[1], codes[1]),
                           ('5h: ' + cells[2] + ' · ↻ ' + cells[3], codes[2]),
                           ('7d: ' + cells[4] + ' · ↻ ' + cells[5], codes[4]),
                           (cells[6], codes[6])]
                if show_selection:
                    content += [(cells[7] + ' · ' + cells[8], codes[7])]
                for text, code in content:
                    for part in wrap_cells(text, width - 4):
                        lines.append(paint('│ ', '90') + paint(part + ' ' * (width - 4 - display_width(part)), code) + paint(' │', '90'))
            lines.append(paint('╰' + '─' * (width - 2) + '╯', '90'))
            continue
        inner_width = sum(widths) + 3 * len(widths) - 1
        lines.extend([paint("╭─ ", "90") + paint(title, provider_color)
                      + paint(" " + "─" * (inner_width - display_width(title) - 3) + "╮", "90"),
                      table_row(rows[0], ["1"] * len(widths)), border("├", "┼", "┤")])
        lines.extend(table_row(row, codes) for row, codes in group)
        lines.append(border("╰", "┴", "╯"))
    if not data["accounts"]:
        lines.extend(wrap_cells('No accounts configured.', width) if stacked else
                     [border("╭", "┬", "╮"), table_row(rows[0], ["1"] * len(widths)), border("╰", "┴", "╯")])
    if show_selection:
        lines += [''] + [paint(part, '1;36') for part in wrap_cells(
            '▶ Selected CLI login  ·  ○ Standby  ·  ? Unverified' + ('  ·  ↻ Until reset' if compact else ''), width)]
        lines.extend(wrap_cells('● LIVE: usage fetched · ✓ Ready: Claude switching candidate, not renewal status', width))
        lines.extend(paint(part, '90') for part in wrap_cells(
            'Selection follows CLI files; already-running sessions may use another login.', width))
    if not verbose:
        return "\n".join(lines)
    if data.get("credential_sync"):
        lines += ["", data["credential_sync"]["limitation"]]
        for note in data["credential_sync"]["notes"]:
            lines.append((note.get("account_id", "Current Claude CLI") + ": " + note["message"]))
    lines += ["", "* Configured plan label. Account names/emails come from captured identity or your labels. — means unknown, never zero."]
    if any(a["provider"] == "claude" for a in data["accounts"]):
        lines.append(live_usage.NOTICE)
    if any(a["provider"] == "codex" for a in data["accounts"]):
        lines.append(live_usage.CODEX_NOTICE)
    if any(a["provider"] == "muse_code" and a.get("quota_scope") not in ("current_muse_cli_login", "owned_muse_cli_login") for a in data["accounts"]):
        lines.append(muse_usage.NOTICE)
    if any(a.get("quota_scope") in ("current_muse_cli_login", "owned_muse_cli_login") for a in data["accounts"]):
        import muse_cli_usage
        lines.append(muse_cli_usage.NOTICE)
    if any(row["status"] == "not_configured" for row in data["accounts"]):
        lines.append(UNSUPPORTED)
    for row in data["accounts"]:
        if row["status"] in ("error", "unsupported"):
            lines.append(f"{row['name']}: {row['message']}")
        if row["usage"] is None:
            if row["provider"] == "muse_code" and row["status"] == "not_configured":
                lines.append(f"{row['name']}: {row['message']}")
            continue
        captured = timestamp(row["captured_at"])
        lines.append(f"\n{row['name']} — {row['status'].upper()} {captured.astimezone():%Y-%m-%d %H:%M:%S %Z} "
                     f"(age {duration(max(0, (now - captured).total_seconds()))})")
        if compact:
            for key, label in (('five_hour', '5h'), ('seven_day', '7d')):
                item = row['usage'][key]
                lines.append(f"  {label}: {percent(item['utilization'])}; reset {reset_text(item['resets_at'], now)}")
        if row.get("portal_team"):
            lines.append(f"  Meta portal team: {row['portal_team']}. Mapping to a Muse CLI login is not verified.")
        if row.get("quota_scope") == "current_muse_cli_login":
            lines.append("  Uses the current Muse CLI login; switching that login changes the account reported here.")
        for note in row["usage"].get("notes", []):
            lines.append("  " + note)
        for key, item in row["usage"]["model_windows"].items():
            lines.append(f"  {key}: {percent(item['utilization'])}; reset {reset_text(item['resets_at'], now)}")
        credits = row["usage"].get("credits")
        if credits is not None:
            lines.append(f"  Codex credits: balance {credits['balance'] if credits['balance'] is not None else '—'}; "
                         f"available {credits['has_credits']}; unlimited {credits['unlimited']}")
        extra = row["usage"]["extra_usage"]
        if extra is not None:
            enabled = {True: "enabled", False: "disabled", None: "unknown"}[extra["is_enabled"]]
            used = "—" if extra["used_credits"] is None else f"{extra['used_credits']:g}"
            limit = "—" if extra["monthly_limit"] is None else f"{extra['monthly_limit']:g}"
            lines.append(f"  Extra usage: {enabled}; {percent(extra['utilization'])}; "
                         f"{used} / {limit} raw credit units (currency/unit scale unverified)")
    if any(row["status"] == "snapshot" for row in data["accounts"]):
        lines.append("\nSnapshots are historical observations. Elapsed resets do not imply 0% current usage.")
    return "\n".join(lines)


class AlphabeticalHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Sort by the primary long option, keeping aliases beside that option."""

    def __init__(self, prog):
        width = min(100, max(60, shutil.get_terminal_size((100, 24)).columns))
        super().__init__(prog, width=width, max_help_position=31)

    def add_arguments(self, actions):
        super().add_arguments(sorted(actions, key=lambda action: next(
            (option for option in action.option_strings if option.startswith("--")), action.dest)))


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default echoes unrecognized arguments, which could be secrets.
        raise UsageError("Invalid command arguments. Use --help; never pass credentials.")


def main(argv=None):
    parser = SafeParser(
        usage="%(prog)s [ACTION] [OPTIONS]",
        formatter_class=AlphabeticalHelpFormatter,
        description=("QuotaHop — Claude, Codex and Muse Code subscription usage.\n"
                     "No action: fetch each configured account once, show the table, and exit.\n"
                     "Choose at most one action. Reports never run inference, login or renewal."),
        epilog="""Account selectors:
  ACCOUNT is a saved ID, email or name. Use --plan for ambiguous names.
  --capture also accepts claude, codex or muse to detect the current account.
  Plans: codex, max_20x, muse_code, team_premium.

Examples:
  %(prog)s --setup
  %(prog)s --capture claude --plan team_premium
  %(prog)s --capture codex
  %(prog)s --sort 7d --reverse
  %(prog)s --switch-next --dry-run
  %(prog)s --change-password
  %(prog)s --json

Authentication:
  --auth-enroll starts background maintenance after a separate sign-in.
  Claude/Codex: automatic renewal. Muse: expiry tracking, no device-token renewal.
  Renewal updates this vault; --switch installs a Claude access token globally.
  --muse-cli-current uses a bootstrap POST, not a read-only API contract.
  Enter passwords only at hidden prompts, never in arguments or configuration.

Report states:
  LIVE = usage fetched. LIVE + checkmark = Claude switching candidate.
  Readiness is not renewal status; inspect --background status separately.

Exit codes:
  0 success; 1 retrieval/restart error; 2 input/vault error;
  3 unconfigured/unsupported accounts; 130 interrupted.
  Historical snapshots can exit 0: scripts should also check JSON's live field.

Full guide: https://github.com/turbokast/quotahop/blob/main/USAGE.md
""")
    parser.add_argument("--data-dir", type=Path, metavar="DIR", default=Path(__file__).absolute().parent / ".usage-data",
                        help="private storage directory (default: .usage-data beside the checkout)")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--setup", action="store_true", help="create the optional seven-Claude-account template; keep existing settings")
    actions.add_argument("--lock", action="store_true", help="stop this vault's worker and forget its five-minute cached unlock")
    actions.add_argument("--change-password", "--reset-password", action="store_true",
                         help="set a new vault password; preserve credentials (current password or cached unlock required)")
    actions.add_argument("--background", choices=("start", "status", "stop"), help="start, inspect or stop independent auth maintenance; no quota polling")
    actions.add_argument("--auth-enroll", metavar="ACCOUNT", help="sign in separately for a saved account and start background maintenance")
    actions.add_argument("--auth-forget", metavar="ACCOUNT", help="forget an independent renewal grant; retain the last access token; no logout")
    actions.add_argument("--refresh", action="store_true", help="sync an already-renewed current CLI token and report usage; no OAuth renewal")
    actions.add_argument("--import", dest="import_id", metavar="ACCOUNT_ID",
                         help="import a historical usage snapshot from stdin; never treated as live")
    actions.add_argument("--capture", metavar="ACCOUNT",
                         help="save current Claude/Codex access-only credentials, or enter a Muse portal cookie privately")
    actions.add_argument("--add", metavar="ACCOUNT_ID", help="add a subscription row; requires --plan; optional --name")
    actions.add_argument("--remove", metavar="ACCOUNT", help="remove a row, its captured token, renewal grant and snapshot")
    actions.add_argument("--switch", metavar="ACCOUNT", help="switch shared Claude Code subscription auth; preserve Design/MCP and other settings")
    actions.add_argument("--switch-next", action="store_true", help="switch Claude to the best available subscription using the report ranking")
    actions.add_argument("--undo-switch", action="store_true", help="restore previous shared Claude Code auth from the encrypted undo record")
    actions.add_argument("--import-muse", metavar="ACCOUNT_ID", help="import a historical Muse usage/read export from stdin")
    actions.add_argument("--muse-cli-current", action="store_true",
                         help="check the current Muse login with one bootstrap POST; no setup or vault needed")
    parser.add_argument("--plan", choices=tuple(PLANS), metavar="PLAN", help="subscription type for account actions, disambiguation or --switch-next filtering")
    parser.add_argument("--dry-run", action="store_true", help="preview --switch, --switch-next or --undo-switch without changing Claude auth")
    parser.add_argument("--name", metavar="NAME", help="display name for --add or --capture when identity is unavailable")
    parser.add_argument("--credential-file", type=Path, metavar="FILE",
                        help="read-only Claude/Codex source for --capture (default: provider CLI credentials)")
    parser.add_argument("--muse-team", metavar="ID", help="Meta portal team ID for --capture; otherwise select interactively")
    parser.add_argument("--json", action="store_true", help="emit structured JSON with full details and no credentials")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=4, metavar="1-8",
                        help="concurrent foreground account checks (default: 4; use 1 for sequential checks)")
    parser.add_argument("--sort", choices=SORT_COLUMNS, default="recommended", metavar="COLUMN",
                        help="sort within groups: recommended (default), name, plan, 5h, 7d, 5h-reset, 7d-reset, status")
    parser.add_argument("--reverse", action="store_true", help="sort descending within each provider group; unknowns stay last")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="terminal colours (default: auto; respects NO_COLOR and redirected output)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="show notices, exact local resets, model allowances and error details")
    args = None
    try:
        args = parser.parse_args(argv)
        if args.credential_file and not args.capture:
            raise UsageError("--credential-file is only valid with --capture.")
        if args.muse_team and not args.capture:
            raise UsageError("--muse-team is only valid with --capture.")
        if args.plan and not (args.add or args.capture or args.remove or args.switch or args.switch_next or args.auth_enroll or args.auth_forget):
            raise UsageError("--plan is only valid with an account action or --switch-next.")
        if args.dry_run and not (args.switch or args.undo_switch or args.switch_next):
            raise UsageError("--dry-run is only valid with --switch, --switch-next, or --undo-switch.")
        if args.switch_next and (args.sort != "recommended" or args.reverse):
            raise UsageError("--switch-next always uses the recommended ranking; omit --sort and --reverse.")
        if args.name and not (args.add or args.capture):
            raise UsageError("--name is only valid with --add or --capture.")
        if args.muse_cli_current:
            import muse_cli_usage
            data = muse_cli_usage.report(dt.datetime.now(UTC))
            data = sort_report(data, args.sort, args.reverse)
            print(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) if args.json else render(data, dt.datetime.now(UTC), verbose=args.verbose, color=color_enabled(args.color)))
            return 0 if data["live"] else 1
        with Store(args.data_dir, create=args.setup, maintenance=args.change_password) as store:
            if args.change_password:
                import vault_password
                result = vault_password.change(store, args.data_dir)
                print(json.dumps(result) if args.json else result["message"])
                return 1 if result.get("background_error") else 0
            if args.lock:
                import auth_renewal
                auth_renewal.stop(store)
                live_usage.lock_vault(store)
                message = "Vault locked. The next command needing saved credentials will ask for your passphrase."
                print(json.dumps({"message": message}) if args.json else message)
                return 0
            if args.setup:
                try:
                    store.write("accounts.json", {"accounts": default_accounts()})
                except FileExistsError:
                    # Idempotent setup validates existing data without changing it.
                    validate_accounts(store.read("accounts.json"))
                message = "Seven-account template ready (existing config preserved). Use --capture ACCOUNT_ID for each account while it is current."
                print(json.dumps({"message": message, "live": False}) if args.json else message)
                return 0
            accounts = validate_accounts(store.read("accounts.json"))
            if args.background or args.auth_enroll is not None or args.auth_forget is not None:
                import auth_renewal
                if args.background == "status":
                    result = auth_renewal.status(store, accounts)
                elif args.background == "stop":
                    result = auth_renewal.stop(store)
                elif args.background == "start":
                    result = auth_renewal.start(store, args.data_dir)
                elif args.auth_enroll is not None:
                    result = auth_renewal.enroll_and_start(store, args.data_dir, accounts, args.auth_enroll, args.plan)
                else:
                    result = auth_renewal.forget(store, accounts, args.auth_forget, args.plan)
                print(json.dumps(result, ensure_ascii=False) if args.json else auth_renewal.render_status(result))
                return 1 if result.get("background_error") else 0
            if args.switch is not None or args.undo_switch or args.switch_next:
                import claude_switch
                if not args.json:
                    print(claude_switch.NOTICE, file=sys.stderr)
                result = claude_switch.switch(store, accounts, args.switch, args.plan,
                                              dry_run=args.dry_run, undo=args.undo_switch,
                                              automatic=args.switch_next, jobs=args.jobs)
                result["interface_notice"] = claude_switch.NOTICE
                label = result["account_name"] + (" · " + PLANS[result["plan"]] if "plan" in result else "")
                print(json.dumps(result, ensure_ascii=False) if args.json else f"{label}: {result['message']}")
                return 0
            if args.remove is not None:
                removed = live_usage.remove_account(store, args.remove, plan=args.plan,
                                                    verbose=args.verbose and not args.json)
                message = f"Removed {removed['name']} from this tool, including any saved token and snapshot."
                print(json.dumps({"message": message, "account_id": removed["id"]}) if args.json else message)
                return 0
            if args.add is not None:
                if args.plan is None:
                    raise UsageError("--add requires --plan.")
                updated = validate_accounts({"accounts": accounts + [
                    {"id": args.add, "name": args.name or args.add, "plan": args.plan}]})
                store.write("accounts.json", {"accounts": updated}, replace=True)
                message = "Account added. Run --auth-enroll with this account ID for independent sign-in and background maintenance, or --capture for an access-only copy."
                print(json.dumps({"message": message}) if args.json else message)
                return 0
            if args.capture is not None:
                clean_text(args.capture)
                reject_secrets(args.capture)
                matches = [a for a in accounts if a["id"] == args.capture or a["name"].casefold() == args.capture.casefold()]
                if not matches and not args.plan and args.capture not in ("claude", "codex", "muse"):
                    # Older vaults can show decoded identity before their plain
                    # configuration labels have been renamed by a recapture.
                    saved_entries = account_identity.for_report(live_usage.load_vault(store))
                    matches = [a for a in accounts if args.capture.casefold() in [
                        v.casefold() for v in account_identity.saved(saved_entries.get(a["id"], {})).values()]]
                if args.plan:
                    matches = [a for a in matches if a["plan"] == args.plan]
                if len(matches) > 1:
                    raise UsageError("Account name/email is ambiguous; specify --plan or a unique saved name.")
                account = matches[0] if matches else {}
                provider = PROVIDERS[args.plan] if args.plan else PROVIDERS[account["plan"]] if account else {
                    "claude": "claude", "codex": "codex", "muse": "muse_code"}.get(args.capture)
                if provider is None:
                    raise UsageError("For a new email/name, specify --plan max_20x, team_premium, codex, or muse_code.")
                if args.capture in ("claude", "codex", "muse") and provider != {"claude": "claude", "codex": "codex", "muse": "muse_code"}[args.capture]:
                    raise UsageError("Plan does not match the selected provider.")
                if account.get("source") == "cli_current":
                    raise UsageError("This row reads the current Muse CLI login automatically; capture is unnecessary. Remove source from its configuration to use a portal capture instead.")
                if provider == "muse_code":
                    if args.credential_file:
                        raise UsageError("Muse uses a portal cookie entered privately, not a CLI credential file.")
                    captured = live_usage.capture(store, args.capture, None, provider, args.muse_team,
                                                  named=True, plan=args.plan, name=args.name)
                    message = "Meta portal session and selected team saved in encrypted vault. Only portal GETs were performed; no CLI authentication, refresh, or inference."
                else:
                    if args.muse_team:
                        raise UsageError("--muse-team requires a Muse Code account.")
                    print(live_usage.CODEX_NOTICE if provider == "codex" else live_usage.NOTICE, file=sys.stderr)
                    captured = live_usage.capture(store, args.capture,
                                                (args.credential_file or live_usage.default_source(provider)).expanduser(), provider,
                                                named=True, plan=args.plan, name=args.name)
                    message = "Access token captured into encrypted vault. Source credentials unchanged; no login, network request, or refresh performed."
                result = {"message": message, **captured}
                print(json.dumps(result) if args.json else f"Captured {captured['account_name']}. {message}")
                return 0
            if args.import_id is not None or args.import_muse is not None:
                ident = args.import_id or args.import_muse
                if ident not in {a["id"] for a in accounts}:
                    raise UsageError("Unknown account ID; inspect accounts.json.")
                if args.import_muse and next(a["plan"] for a in accounts if a["id"] == ident) != "muse_code":
                    raise UsageError("--import-muse requires a Muse Code account.")
                raw = decode(sys.stdin.buffer.read(LIMIT + 1))
                value = muse_snapshot(raw) if args.import_muse else validate_snapshot(raw)
                store.write(ident + ".json", value, replace=True)
                message = "Manual snapshot saved. It is not a live usage check."
                print(json.dumps({"message": message, "live": False}) if args.json else message)
                return 0
            refresh_notes = None
            if args.refresh:
                import saved_refresh
                entries, refresh_notes = saved_refresh.refresh(store, accounts)
            else:
                entries = live_usage.load_vault(store, verbose=args.verbose and not args.json)
            entries = account_identity.for_report(entries)
            import credential_state
            before = credential_state.current(accounts, entries)
            now = dt.datetime.now(UTC)
            data = concurrent_report(accounts, store, now, entries, args.jobs)
            data = credential_state.annotate(data, entries, before, credential_state.current(accounts, entries), dt.datetime.now(UTC))
            if refresh_notes is not None:
                data["credential_sync"] = {"notes": refresh_notes, "oauth_renewal": False,
                    "limitation": "Only an already-renewed current Claude CLI token can be synchronized. Inactive Claude accounts require recapture. Independent Claude/Codex renewal and Muse expiry tracking are managed separately by --background."}
                unverified = {n["account_id"] for n in refresh_notes if n["state"] == "binding_unavailable"}
                for row in data["accounts"]:
                    if row["id"] in unverified and row["switch_readiness"] == "ready":
                        row["switch_readiness"] = "identity_unverified"
            data = sort_report(data, args.sort, args.reverse)
            now = dt.datetime.now(UTC)
        print(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) if args.json else render(data, now, verbose=args.verbose, color=color_enabled(args.color)))
        statuses = {a["status"] for a in data["accounts"]}
        return 1 if "error" in statuses else 3 if statuses & {"not_configured", "unsupported"} else 0
    except BrokenPipeError:
        return 0
    except (UsageError, live_usage.LiveError, OSError, ValueError, OverflowError) as exc:
        message = str(exc) if isinstance(exc, (UsageError, live_usage.LiveError)) else (
            "Cannot access private data. Run --setup first; check ownership, permissions, and symlinks."
        )
        if args is not None and args.json:
            print(json.dumps({"error": message, "live": False}))
        else:
            print("claude-usage: " + message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
