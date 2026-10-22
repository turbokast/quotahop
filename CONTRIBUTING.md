# Contributing to QuotaHop

Small, focused improvements are welcome. For substantial changes, open an issue
first to describe the problem and proposed behaviour. Read the [usage guide](USAGE.md)
and [interface research](RESEARCH.md) before changing provider integration code.
Participation is covered by the [code of conduct](CODE_OF_CONDUCT.md).

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B -m unittest discover -s tests -v
```

Use Python 3.12+ on Linux; CI runs Python 3.12 and 3.14. Automated tests use fake
credentials and isolated temporary directories. Do not use a real subscription to test inference,
authentication rotation, or failure recovery. The optional filesystem sandbox
check needs native Codex and bubblewrap installed.

## Pull requests

Explain the user-visible change and include relevant test results. Keep the
runtime small; prefer the standard library when practical. The exception is the
optional login-keyring autostart, which uses `jeepney` and `secretstorage`
(pinned in `requirements.txt`). Update `--help` and
documentation when commands or behaviour change.

Preserve these boundaries:

- Reports fetch quota once per account and exit; no inference, login, token
  renewal or automatic polling. They may restart a stopped renewal worker (never
  after an explicit stop) and copy Claude Code's already-renewed access token.
- Capture never refreshes or writes a provider-owned authentication context.
- Only explicit switching changes shared Claude subscription auth.
- Each Claude grant has exactly one renewal owner: Claude Code while it is the
  active login, otherwise QuotaHop. Switching holds Claude Code's refresh locks
  and takes the outgoing login back; never let both sides refresh one grant.
- Renewal suspends uncertain rotations rather than replaying a refresh token.
- Password changes preserve all encrypted records or leave recoverable state.
- Provider errors, logs and JSON must not expose secrets.

Never commit `.usage-data/`, real credentials, private account labels, or vaults,
even encrypted. Use clearly fake fixtures such as `person@example.test`.
Native authentication build pins (and the retained verified copy of the Claude
build that keeps renewal working after Claude Code prunes old versions) require an
offline, inference-free validation before updating; never remove the check merely
to support a newer binary.

Dependency and workflow updates arrive as Dependabot PRs. Review and test them;
they are not merged automatically. Workflows pin actions to commit hashes.
Report security issues [privately](SECURITY.md), not through public bug reports.
