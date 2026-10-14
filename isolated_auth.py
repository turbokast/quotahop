"""Disposable first-party authentication contexts; never mount the user's home."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile

import live_usage as live

# The no-prompt startup behavior was tested offline on these builds. Fail closed
# after upgrades until that behavior is checked again; never run updater scripts.
BUILDS = {
    'claude': '24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de',
    'muse': '82a495542965a5b6256f7acdb81693451c28640a419d5528ad8340ea93f36f6f',
}
_digests = {}


class PreflightError(live.LiveError):
    """Refused before any process start or network request; safe to retry later."""


def digest(binary):
    info = binary.stat()
    key = (str(binary), info.st_size, info.st_mtime_ns)
    if key not in _digests:
        with binary.open('rb') as stream:
            _digests[key] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return _digests[key]


def native(provider):
    executable = shutil.which(provider)
    if not executable:
        raise PreflightError('dependency_missing', f'Install the first-party {provider} CLI before enrollment.')
    binary = Path(executable).resolve(strict=True)
    if provider == 'claude' and digest(binary) != BUILDS[provider]:
        # Claude auto-updates into sibling version files and keeps a few older
        # builds. Keep using the verified build while it remains installed.
        for sibling in sorted(binary.parent.iterdir(), reverse=True):
            try:
                if sibling.is_file() and not sibling.is_symlink() and digest(sibling) == BUILDS[provider]:
                    return sibling
            except OSError:
                continue
    if provider == 'muse':
        with binary.open('rb') as f:
            wrapper = f.read(4) != b'\x7fELF'
        if wrapper:
            version = (binary.parent / '.muse-version').read_text().strip()
            import re
            if not re.fullmatch(r'[A-Za-z0-9.-]{1,80}', version):
                raise live.LiveError('unsupported_cli', 'Invalid Muse native version selector.')
            binary = (binary.parent / ('muse-bin-' + version)).resolve(strict=True)
    if digest(binary) != BUILDS[provider]:
        raise PreflightError('unsupported_cli', f'This {provider} build has not been verified for isolated, inference-free authentication; renewal refused.')
    return binary


def command(provider, context, args):
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise PreflightError('dependency_missing', 'Isolated authentication requires bubblewrap (apt install bubblewrap).')
    binary = native(provider)
    cmd = [bwrap, '--die-with-parent', '--new-session', '--unshare-user', '--unshare-pid',
           '--unshare-ipc', '--unshare-uts', '--cap-drop', 'ALL']
    for path in ('/usr', '/lib', '/lib64', '/etc/ssl', '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf'):
        if Path(path).exists():
            cmd += ['--ro-bind', path, path]
    cmd += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
            '--ro-bind', str(binary), '/opt/' + provider, '--bind', str(context), '/owned',
            '--chdir', '/owned/home', '--clearenv']
    environment = {'HOME': '/owned/home', 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TERM': 'dumb',
                   'XDG_CONFIG_HOME': '/owned/config', 'CLAUDE_CONFIG_DIR': '/owned/claude',
                   'CLAUDE_SECURESTORAGE_CONFIG_DIR': '/owned/claude',
                   'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1'}
    for key, value in environment.items():
        cmd += ['--setenv', key, value]
    return cmd + ['/opt/' + provider, *args]


def write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)


@contextmanager
def context(provider):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryDirectory(prefix='cc-usage-' + provider + '-') as directory:
        root = Path(directory)
        for part in ('home', 'claude', 'config', 'config/muse'):
            (root / part).mkdir(mode=0o700)
        yield root


@contextmanager
def process(cmd, *, tty=None):
    proc = subprocess.Popen(cmd, stdin=tty if tty else subprocess.PIPE,
                            stdout=tty if tty else subprocess.PIPE,
                            stderr=tty if tty else subprocess.DEVNULL,
                            env={'PATH': '/usr/bin:/bin'}, start_new_session=True, umask=0o077)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait()
        if tty is None:
            proc.stdin.close()
            proc.stdout.close()
