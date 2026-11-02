"""Best-effort desktop notifications through notify-send; never raises.

Messages are fixed, non-secret text (labels, states, times). Missing
notify-send or a missing desktop session simply means no notification.
"""
import shutil
import subprocess


def send(title, message):
    binary = shutil.which("notify-send")
    if not binary:
        return False
    try:
        result = subprocess.run([binary, "--app-name=QuotaHop", title, message],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=5, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
