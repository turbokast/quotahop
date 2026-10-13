"""Native Claude sign-in and initialization-only renewal, with no user prompt."""
import json
import math
import uuid
import subprocess
import time

import account_identity
import claude_switch
import isolated_auth as isolated
import live_usage as live
from owned_codex import private_read
import saved_refresh

ORIGIN = 'isolated-claude-login-v1'
INIT = {'type': 'control_request', 'request_id': 'cc-usage-auth', 'request': {'subtype': 'initialize'}}
ARGS = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
        '--verbose', '--no-session-persistence', '--strict-mcp-config']


def validate_auth(auth):
    if not isinstance(auth, dict) or set(auth) != {'claudeAiOauth'}:
        raise live.LiveError('invalid_auth', 'Invalid isolated Claude grant; content suppressed.')
    oauth = auth['claudeAiOauth']
    if (not isinstance(oauth, dict) or set(oauth) - {'accessToken', 'refreshToken', 'expiresAt',
            'refreshTokenExpiresAt', 'clientId', 'scopes', 'subscriptionType', 'rateLimitTier'}):
        raise live.LiveError('invalid_auth', 'Unsupported isolated Claude grant format.')
    live.validate_token(oauth.get('accessToken'))
    live.validate_token(oauth.get('refreshToken'))
    live.validate_scopes(oauth.get('scopes'))
    expiry = oauth.get('expiresAt')
    if type(expiry) not in (float, int) or not math.isfinite(expiry) or expiry <= 0:
        raise live.LiveError('invalid_auth', 'Isolated Claude grant has no usable expiry.')
    refresh_expiry = oauth.get('refreshTokenExpiresAt')
    if refresh_expiry is not None and (type(refresh_expiry) not in (float, int)
            or not math.isfinite(refresh_expiry) or refresh_expiry <= 0):
        raise live.LiveError('invalid_auth', 'Invalid isolated Claude refresh expiry.')
    # Claude 2.1.296's native credential writer (Ok/xvr) persists both fields.
    # Preserve the public OAuth client ID: native renewal uses the grant's client.
    if oauth.get('clientId') is not None:
        try:
            if str(uuid.UUID(oauth['clientId'])) != oauth['clientId']:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise live.LiveError('invalid_auth', 'Invalid isolated Claude OAuth client metadata.') from None
    return auth


def matches(auth, entry):
    return entry.get('provider', 'claude') == 'claude' and entry.get('access_token') == auth['claudeAiOauth']['accessToken']


def perform(auth=None, *, account):
    with isolated.context('claude') as root:
        source = root / 'claude/.credentials.json'
        isolated.write(root / 'claude/.claude.json', {'hasCompletedOnboarding': True})
        if auth is None:
            try:
                tty = open('/dev/tty', 'r+b', buffering=0)
            except OSError:
                raise live.LiveError('tty_required', 'Independent Claude sign-in requires an interactive terminal.') from None
            with tty, isolated.process(isolated.command('claude', root, ['auth', 'login', '--claudeai']), tty=tty) as proc:
                try:
                    if proc.wait(timeout=600) != 0:
                        raise live.LiveError('auth_rejected', 'Independent Claude sign-in did not complete.')
                except subprocess.TimeoutExpired:
                    raise live.LiveError('auth_timeout', 'Independent sign-in timed out.') from None
        else:
            isolated.write(source, validate_auth(auth))
            # No prompt, user message, session resume or tool request is ever sent.
            # auth status is unsuitable: it exits before saving a rotated token.
            with isolated.process(isolated.command('claude', root, ARGS)) as proc:
                try:
                    output, _ = proc.communicate((json.dumps(INIT) + '\n').encode(), timeout=25)
                except subprocess.TimeoutExpired:
                    raise live.LiveError('auth_timeout', 'Isolated Claude renewal timed out; no automatic replay.') from None
                if proc.returncode != 0 or len(output) > live.MAX_BYTES:
                    raise live.LiveError('auth_rejected', 'Isolated Claude initialization failed; output suppressed.')
                replies = [live.json_object(line) for line in output.splitlines() if line.strip()]
                if not any(r.get('type') == 'control_response' and r.get('response', {}).get('request_id') == INIT['request_id']
                           and r['response'].get('subtype') == 'success' for r in replies):
                    raise live.LiveError('auth_protocol', 'Expected authentication initialization response was not received.')
        fresh = validate_auth(private_read(source))
        entry = live.read_access_token(source, 'claude', include_identity=False)
        if not entry.get('expires_at') or entry['expires_at'] < time.time() + 600:
            raise live.LiveError('auth_expiry', 'Claude did not save a grant valid for at least ten minutes.')
        _, metadata = claude_switch.target_auth(account, entry)
        entry['claude_identity'] = saved_refresh.binding(metadata)
        identity = account_identity.extract(metadata.get('emailAddress'), metadata.get('displayName'))
        if identity:
            entry['identity'] = identity
        return fresh, entry
