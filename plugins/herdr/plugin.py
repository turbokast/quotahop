"""Herdr adapter. Launch the existing QuotaHop installation; never copy its vault."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


PLUGIN = "turbokast.quotahop"


def config():
    directory = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    value = {}
    if directory:
        path = Path(directory) / "config.json"
        if path.exists():
            if path.stat().st_size > 8192:
                raise ValueError("Plugin config is too large.")
            value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) - {"quotahop_path", "data_dir", "interval_seconds"}:
        raise ValueError("Config accepts only quotahop_path, data_dir and interval_seconds; no credentials.")
    for field in ("quotahop_path", "data_dir"):
        if field in value and (not isinstance(value[field], str) or not Path(value[field]).is_absolute()):
            raise ValueError("Plugin paths must be absolute.")
    interval = value.get("interval_seconds", 60)
    if type(interval) is not int or not 30 <= interval <= 3600:
        raise ValueError("interval_seconds must be 30–3600.")
    executable = value.get("quotahop_path") or shutil.which("quotahop")
    if not executable or not os.access(executable, os.X_OK):
        raise ValueError("Install QuotaHop on PATH or set quotahop_path in this plugin's config.json.")
    command = [executable, "--panel", "--panel-interval", str(interval)]
    if "data_dir" in value:
        command.extend(["--data-dir", value["data_dir"]])
    return command


def herdr(*args):
    result = subprocess.run([os.environ.get("HERDR_BIN_PATH", "herdr"), *args],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise ValueError("Herdr could not open or resize the panel. Check the plugin log and current pane.")
    return json.loads(result.stdout)["result"]


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        command = config()
        if args == ["panel"]:
            os.execv(command[0], command)  # Same PID; no wrapper or detached worker survives.
        if args != ["open"]:
            raise ValueError("Expected open or panel.")
        target = os.environ.get("HERDR_PANE_ID")
        if os.environ.get("HERDR_ENV") != "1" or not target:
            raise ValueError("Open this action from a Herdr workspace with a current pane.")
        layout = herdr("pane", "layout", "--pane", target)["layout"]
        rectangle = next(p["rect"] for p in layout["panes"] if p["pane_id"] == target)
        # A right split keeps quota visible beside the caller and never steals focus.
        result = herdr("plugin", "pane", "open", "--plugin", PLUGIN,
                       "--entrypoint", "usage", "--target-pane", target,
                       "--direction", "right", "--no-focus")
        pane = result["plugin_pane"]["pane"]["pane_id"]
        # Resize only this newly created split. Small terminals retain equal halves.
        if rectangle["width"] > 90:
            amount = max(0, 0.5 - 32 / rectangle["width"])
            try:
                herdr("pane", "resize", "--pane", pane, "--direction", "right",
                      "--amount", f"{amount:.4f}")
            except (ValueError, OSError, subprocess.TimeoutExpired):
                print("Panel opened; resize it with Herdr's normal pane controls.", file=sys.stderr)
        return 0
    except (ValueError, KeyError, StopIteration, OSError, subprocess.TimeoutExpired):
        print("QuotaHop plugin: check the installed quotahop command, plugin config and Herdr context. See plugins/herdr/README.md.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
