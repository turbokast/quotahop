"""First-party Codex auth RPC in a filesystem-isolated, disposable context.

Only independently enrolled grants enter this module. No source CLI credentials,
threads, turns, prompts, keyrings, plugins, or user configuration are available.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import resource
import selectors
import shutil
import stat
import subprocess
import tempfile
import time

import account_identity
import live_usage as live

METHODS = frozenset({'initialize', 'initialized', 'account/login/start', 'account/read'})
CONFIG = 'cli_auth_credentials_store = "file"\n[analytics]\nenabled = false\n[feedback]\nenabled = false\n'


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        s = os.fstat(stream.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1 or s.st_mode & 0o077:
            raise live.LiveError('unsafe_auth', 'Isolated authentication file is not private.')
        return live.json_object(stream.read(live.MAX_BYTES + 1))


def validate_auth(auth):
    if (not isinstance(auth, dict) or auth.get('auth_mode') not in (None, 'chatgpt')
            or auth.get('OPENAI_API_KEY') is not None
            or set(auth) - {'auth_mode', 'OPENAI_API_KEY', 'tokens', 'last_refresh'}):
        raise live.LiveError('invalid_auth', 'Expected a managed ChatGPT subscription grant.')
    tokens = auth.get('tokens')
    if not isinstance(tokens, dict) or set(tokens) != {'id_token', 'access_token', 'refresh_token', 'account_id'}:
        raise live.LiveError('invalid_auth', 'Unsupported first-party authentication format; content suppressed.')
    for field in ('id_token', 'access_token', 'refresh_token'):
        live.validate_token(tokens.get(field))
    live.validate_account_id(tokens.get('account_id'))
    return auth


def command(context):
    binary = shutil.which('codex')
    bwrap = shutil.which('bwrap')
    if not binary or not bwrap:
        raise live.LiveError('dependency_missing', 'Isolated renewal requires the native Codex CLI and bubblewrap (apt install bubblewrap).')
    binary = Path(binary).resolve(strict=True)
    # No shell/npm/updater wrappers: only the installed native executable.
    with binary.open('rb') as f:
        if f.read(4) != b'\x7fELF':
            raise live.LiveError('unsupported_cli', 'Put the native Codex executable on PATH; shell wrappers are not supported for renewal.')
    cmd = [bwrap, '--die-with-parent', '--new-session', '--unshare-user', '--unshare-pid',
           '--unshare-ipc', '--unshare-uts', '--cap-drop', 'ALL']
    for path in ('/usr', '/lib', '/lib64'):
        if Path(path).exists():
            cmd += ['--ro-bind', path, path]
    for path in ('/etc/ssl', '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf'):
        if Path(path).exists():
            cmd += ['--ro-bind', path, path]
    cmd += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
            '--ro-bind', str(binary), '/opt/codex', '--bind', str(context), '/owned',
            '--chdir', '/owned/home', '--clearenv', '--setenv', 'HOME', '/owned/home',
            '--setenv', 'CODEX_HOME', '/owned/codex', '--setenv', 'PATH', '/usr/bin:/bin',
            '--setenv', 'LANG', 'C.UTF-8', '/opt/codex', 'app-server']
    return cmd


class Rpc:
    def __init__(self, process):
        self.process = process
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.serial = 0
        self.notifications = []

    def send(self, method, params, *, notification=False):
        if method not in METHODS:
            raise live.LiveError('prohibited_rpc', 'RPC method prohibited by the authentication allowlist.')
        if method == 'account/login/start' and params != {'type': 'chatgptDeviceCode'}:
            raise live.LiveError('prohibited_rpc', 'Only independent first-party device sign-in is permitted.')
        if method == 'account/read' and (set(params) != {'refreshToken'} or type(params['refreshToken']) is not bool):
            raise live.LiveError('prohibited_rpc', 'Invalid authentication-only RPC parameters.')
        self.serial += 1
        packet = {'method': method, 'params': params}
        if not notification:
            packet['id'] = self.serial
        try:
            self.process.stdin.write(json.dumps(packet).encode() + b'\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise live.LiveError('auth_process', 'Isolated Codex process stopped; output suppressed.') from None
        return self.serial

    def receive(self, end):
        while b'\n' not in self.buffer:
            remaining = end - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise live.LiveError('auth_timeout', 'Isolated authentication operation timed out; no automatic replay.')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise live.LiveError('auth_process', 'Isolated Codex process exited; check bubblewrap support and CLI installation.')
            self.buffer += chunk
            if len(self.buffer) > live.MAX_BYTES:
                raise live.LiveError('auth_protocol', 'Oversized authentication response suppressed.')
        line, self.buffer = self.buffer.split(b'\n', 1)
        value = live.json_object(line)
        # Never execute any server-requested action, including credential helpers.
        if 'method' in value and 'id' in value:
            raise live.LiveError('prohibited_rpc', 'Unexpected server request; authentication stopped.')
        return value

    def call(self, method, params, timeout=20):
        ident = self.send(method, params)
        end = time.monotonic() + timeout
        for _ in range(1000):
            value = self.receive(end)
            if value.get('id') == ident:
                if 'error' in value or not isinstance(value.get('result'), dict):
                    raise live.LiveError('auth_rejected', 'First-party authentication failed; re-enroll this account. Provider output suppressed.')
                return value['result']
            if value.get('method') == 'account/login/completed':
                self.notifications.append(value)
        raise live.LiveError('auth_protocol', 'Excessive authentication messages; operation stopped.')

    def login(self):
        try:
            fd = os.open('/dev/tty', os.O_WRONLY | os.O_NOCTTY)
            os.close(fd)
        except OSError:
            raise live.LiveError('tty_required', 'An interactive terminal is required for independent sign-in.') from None
        result = self.call('account/login/start', {'type': 'chatgptDeviceCode'})
        if (result.get('verificationUrl') != 'https://auth.openai.com/codex/device'
                or not isinstance(result.get('userCode'), str)
                or not re.fullmatch(r'[A-Z0-9-]{4,32}', result['userCode'])
                or not isinstance(result.get('loginId'), str)):
            raise live.LiveError('auth_protocol', 'Unexpected device authorization response; content suppressed.')
        # This short-lived code is shown only to the user's controlling terminal,
        # never JSON/stdout, logs, or command arguments.
        with open('/dev/tty', 'w') as tty:
            print('Sign in to this subscription at https://auth.openai.com/codex/device', file=tty)
            print('Device code: ' + result['userCode'], file=tty, flush=True)
        end = time.monotonic() + 600
        for _ in range(10000):
            value = self.notifications.pop(0) if self.notifications else self.receive(end)
            if value.get('method') == 'account/login/completed':
                params = value.get('params', {})
                if params.get('loginId') == result['loginId']:
                    if params.get('success') is not True:
                        raise live.LiveError('auth_rejected', 'Sign-in did not complete; existing credentials are unchanged.')
                    return
        raise live.LiveError('auth_protocol', 'Excessive authentication messages; operation stopped.')


@contextmanager
def session(auth=None):
    """Private transient first-party files; encrypted credentials persist elsewhere.

    Native descendants are inside a PID namespace. Killing/reaping bwrap also
    terminates its descendants, including a pending device-login poller.
    """
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    with tempfile.TemporaryDirectory(prefix='cc-usage-auth-') as directory:
        context = Path(directory)
        (context / 'home').mkdir(mode=0o700)
        (context / 'codex').mkdir(mode=0o700)
        config = context / 'codex/config.toml'
        config.write_text(CONFIG)
        config.chmod(0o600)
        source = context / 'codex/auth.json'
        if auth is not None:
            fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(validate_auth(auth), stream)
        proc = subprocess.Popen(command(context), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin'},
                                start_new_session=True, umask=0o077)
        rpc = None
        try:
            rpc = Rpc(proc)
            rpc.call('initialize', {'clientInfo': {'name': 'cc_usage_auth', 'version': '1.0'}}, timeout=10)
            rpc.send('initialized', {}, notification=True)
            yield rpc, source
        finally:
            if rpc is not None:
                rpc.selector.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.wait()
            proc.stdin.close()
            proc.stdout.close()


def perform(auth=None):
    with session(auth) as (rpc, source):
        if auth is None:
            rpc.login()
        # Codex itself handles OAuth and rotation. No reimplementation or client ID.
        result = rpc.call('account/read', {'refreshToken': auth is not None})
        account = result.get('account')
        if not isinstance(account, dict) or account.get('type') != 'chatgpt':
            raise live.LiveError('auth_rejected', 'No managed ChatGPT subscription in the isolated login.')
        fresh = validate_auth(private_read(source))
        entry = live.read_access_token(source, 'codex')
        identity = account_identity.extract(email=account.get('email'), secrets=tuple(fresh['tokens'].values()))
        if identity:
            entry['identity'] = identity
        if not entry.get('expires_at') or entry['expires_at'] < time.time() + 600:
            raise live.LiveError('auth_expiry', 'Renewal did not produce a token valid for at least ten minutes.')
        return fresh, entry
