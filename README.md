# QuotaHop

**Know what’s left. Pick what’s next.**

[![Tests](https://github.com/turbokast/quotahop/actions/workflows/tests.yml/badge.svg)](https://github.com/turbokast/quotahop/actions/workflows/tests.yml)
[![CodeQL](https://github.com/turbokast/quotahop/actions/workflows/codeql.yml/badge.svg)](https://github.com/turbokast/quotahop/actions/workflows/codeql.yml)
[![MIT license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)

Your **Claude, Codex, and Muse Code subscription quotas** in one terminal report.
See what’s available, when it resets, and which Claude subscription to use next.
Runs locally on Ubuntu. No inference requests, hosted service, or telemetry.

- **One command, one report.** Five-hour and weekly usage, local reset times, and model allowances.
- **Useful defaults.** Accounts grouped by provider and ranked by spare capacity and reset urgency.
- **Readable anywhere.** Compact tables, colours, selected-login markers, and JSON for scripts.
- **Herdr panel.** Keep quotas beside your work in an optional compact, live terminal split.
- **Private accounts.** An encrypted vault, five-minute unlock cache, and separate entries for personal and Team subscriptions sharing an email.
- **Explicit switching.** Preview, switch, and undo Claude subscription changes while preserving unrelated configuration.

[Quick start](#quick-start) · [Everyday commands](#everyday-commands) · [Herdr plugin](#herdr-plugin) · [Renewal](#independent-sign-in-and-renewal) · [Full guide](USAGE.md) · [Security](SECURITY.md)

## What’s supported

| Provider | Subscription usage | Global CLI switching | Independent renewal |
|---|---|---|---|
| Anthropic | Claude Max 20× and Team Premium seats | Yes, with preview and undo | Yes, with verified native builds |
| OpenAI | Codex subscriptions; windows exposed by the account | — | Yes, via native Codex |
| Meta | Muse Code subscription quotas | — | Separate sign-in and expiry tracking only |

This is an **unofficial personal utility**. Usage endpoints are undocumented
first-party interfaces; access, policies, and response formats can change.
Missing data stays unknown, never invented as zero. Team seats each need their
own credentials; organisation API spending is not subscription quota.
See [interface research](RESEARCH.md) for evidence and limitations.

## Quick start

Requires Ubuntu/Linux and **Python 3.12+**. Install `python3-venv` if needed.

```sh
git clone https://github.com/turbokast/quotahop.git
cd quotahop
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./quotahop --setup
```

Setup creates private `.usage-data/` storage and a template of two Max 20× and
five Team Premium rows. It makes no network requests and preserves existing
configuration. Capture accounts into those slots, remove unused rows, or add more.

While the account you want is already signed in through its normal CLI:

```sh
./quotahop --capture claude --plan max_20x
./quotahop --capture claude --plan team_premium
./quotahop --capture codex
./quotahop
```

Each capture reads the **current** login. Switch accounts through your usual
workflow, then capture again to save another subscription. Names and emails are
detected where available. Max and Team subscriptions with the same email stay
separate; use `--plan` when selecting between them.

On first capture, **create a vault password** of at least 12 characters. There
is no default password. Subsequent commands reuse the unlock for five minutes
where the Linux keyring is available. Passwords and tokens never belong in
command arguments, config, issues, or screenshots.

Captures copy access tokens only and leave provider auth untouched. They do not
renew tokens; recapture after your normal login when one expires, or use
[independent enrollment](#independent-sign-in-and-renewal).

The original `./claude-usage` launcher remains supported and shares the vault.

## Everyday commands

```sh
./quotahop                          # Fetch once, show the table, exit
./quotahop --verbose                # Exact local resets, model limits and errors
./quotahop --json                   # Full structured details, no credentials
./quotahop --sort 7d --reverse       # Sort within each provider group
./quotahop --remove 'you@example.com' --plan team_premium
./quotahop --change-password        # Preserve credentials; choose a new password
./quotahop --lock                   # Stop renewal and clear the cached unlock
./quotahop --help                   # Alphabetical options and examples
```

| Indicator | Meaning |
|---|---|
| `● LIVE` | Usage was fetched successfully. |
| `◷ RECENT` | A successful Claude check, less than 60 seconds old, was reused. |
| `● LIVE ✓` / `✓ Ready` | Also a Claude switching candidate: token valid for over a minute, required scope if known, and quota headroom. |
| `▶` | Matches the selected CLI credentials; already-running sessions may use another login. |
| `—` | The provider did not supply this metric. |
| `THROTTLED` | The usage endpoint returned HTTP 429. Subscription quota is unknown; wait for the displayed cooldown. |
| Auth error / Timeout / Network | The check failed; this does not mean the subscription is exhausted. See `--verbose`. |

**Ready does not mean automatic renewal is enabled.** Use `--background status`
for renewal state. A switch verifies the target’s identity before installing it.

Claude reports, panels and switches share a private 60-second usage cache, so
running a report followed by `--switch-next` does not immediately query all seven
accounts again. Cache hits keep the original check time, expire at reset boundaries,
and never survive a credential change. HTTP 429 starts a shared cooldown of at
least five minutes (longer when requested by the server). No retries or token
refreshes are triggered. Failed checks never become zero usage or exhausted quota.

The default ranking uses `remaining percentage / (1 + hours until earliest reset)`.
Remaining percentage is the smaller known five-hour/weekly headroom. Exhausted
and unavailable accounts rank below usable ones. This is a practical heuristic;
percentages across different plans do not represent equal token allowances.

## Herdr plugin

Keep the combined report in a small terminal panel. Requires Herdr 0.9.1+ and
your configured QuotaHop command on PATH:

```sh
herdr plugin install turbokast/quotahop/plugins/herdr
herdr plugin action invoke turbokast.quotahop.open
```

The 32-column panel shows **remaining usage for Active and Next** subscriptions
per provider, with separate five-hour and weekly percentages. It refreshes every
60 seconds while open. Focus it and press `u` to unlock,
`r` to refresh, `d` for details, `p` to pause, or `q` to close. Use ↑/↓ to select a
provider, Enter to preview its Next account, and Enter again to confirm a Claude
switch (Esc cancels). Codex/Muse switching is unavailable. It keeps the vault
key in memory until locked/closed; `quotahop --lock` locks open panels too.
No credentials are copied into Herdr. Ordinary reports remain one-shot.
If the active login cannot be verified, **Best?** shows the best saved candidate's
remaining quota without claiming it is a different login; switching stays blocked.
`~` marks a recent cached check. The panel pauses requests during endpoint cooldowns.
It is a tab-local split; Herdr does not yet provide a global pinned plugin widget.

You can also run `./quotahop --panel` in any interactive terminal.
See the [plugin guide](plugins/herdr/README.md) for configuration, controls,
security boundaries and marketplace publication.

## Switch Claude subscriptions

```sh
./quotahop --switch-next --dry-run
./quotahop --switch-next
./quotahop --switch-next --provider claude
./quotahop --switch 'you@example.com' --plan team_premium
./quotahop --undo-switch
```

These explicit actions change the **shared Claude Code subscription login**.
They preserve Claude Design, MCP auth, and unrelated configuration, without
symlinks or global environment changes. For an independently enrolled Claude
grant, switching transfers its complete credentials to Claude Code and permanently
stops QuotaHop renewing that grant **before** writing the CLI files. Captured
logins remain access-only. Existing sessions may need to reload credentials.
Codex and Muse switching are not implemented.

## Add Muse Code

Check your current Muse CLI login without a vault or setup:

```sh
./quotahop --muse-cli-current
```

To include it in the combined report:

```sh
./quotahop --add muse-current --plan muse_code --name Muse
```

Add `"source": "cli_current"` to that row in `.usage-data/accounts.json`, retaining
its private permissions. That row follows your current Muse login. For separate
saved accounts, use independent enrollment or the [portal setup](USAGE.md#muse-code-setup--real-portal-subscription-quota).

**The CLI route uses a bootstrap POST, not a documented read-only API.** It sends
`{"onboard":false}`, requires an existing key, compares the returned key, and
discards returned credentials. Observed key reuse is not a guarantee about
future server behaviour. The portal alternative uses GETs but still awaits
an authenticated end-to-end check.

## Independent sign-in and renewal

Requires the native provider CLI and `bubblewrap` (`sudo apt install bubblewrap`).
Enrollment runs in a separate filesystem/PID sandbox; shared CLI refresh tokens
are never imported. If isolation cannot be established, enrollment fails.

```sh
./quotahop --auth-enroll max-1
./quotahop --auth-enroll team-1
./quotahop --add codex-work --plan codex --name 'you@example.com'
./quotahop --auth-enroll codex-work
./quotahop --add muse-work --plan muse_code --name 'you@example.com'
./quotahop --auth-enroll muse-work
./quotahop --background status
./quotahop --background stop
```

Use a saved ID, email, or name. Add a new row with `--add` first; `--plan` resolves
ambiguous subscriptions.

**Successful enrollment starts a background worker.** Private Claude standby
grants and Codex grants renew before expiry. Muse tracks expiry but cannot automatically renew its device
token. The worker keeps the vault key in memory and never polls quota or switches
global auth. No service, cron job, or boot startup is installed.

Renewal updates **the vault**. Run `--switch` or `--switch-next` to hand a private
Claude grant to Claude Code. It then becomes `cli_owned`: only Claude Code may
renew it, even after you switch away. Reports safely follow a newer current CLI
access token after verifying its account and organisation. Uncertain refresh
outcomes suspend the account rather than replay a potentially consumed token.

Older Claude grants without ownership tracking show `legacy_hold`; they might
already be used by running sessions, so QuotaHop will not rotate them. An explicit
switch can transfer a still-valid grant to Claude Code. For an expired grant or
a new automatically renewed standby grant, use `--auth-forget ACCOUNT` followed
by `--auth-enroll ACCOUNT` (add `--plan` when ambiguous). This does not log out or
revoke the old session. A provider-revoked grant still requires a fresh sign-in.

Claude 2.1.296 and Muse 1.4.6-R6000.1 authentication are pinned to inspected
Linux executable hashes. Other builds, even with the same version, are refused
until verified. Read-only capture does not require these binaries. See the
[renewal guide](USAGE.md#optional-background-authentication-renewal).

## Change the vault password

```sh
./quotahop --change-password
# Alias: ./quotahop --reset-password
```

Supply the current password or use an unexpired cached unlock, then enter and
confirm a new password. Account definitions, credentials, renewable grants, and
switch undo history are preserved. A running worker is paused and restarted
with the new key; a stopped worker stays stopped.

If interrupted, run the command again and enter the **new** password to finish
the encrypted recovery transaction. Do not delete `vault-rekey.json` manually.
If only the worker restart fails, run `--background start` with the new password.

A forgotten password cannot decrypt a locked vault after the unlock cache expires.
There is no recovery backdoor or silent deletion of saved credentials.

## Contribute

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

Tests use fake credentials and mocked provider responses. The suite covers
parsing, account isolation, endpoint allowlists, timeouts, concurrency, terminal
layouts, switching, renewal, and interrupted password changes. The native sandbox
check requires local CLI dependencies; mocked tests do not prove live access.

GitHub runs tests and CodeQL, reviews dependency changes, and opens Dependabot
update PRs. Changes are reviewed before merging. See [CONTRIBUTING.md](CONTRIBUTING.md)
for a short guide and [SECURITY.md](SECURITY.md) for private vulnerability reports.
Never include real account data or vault files in a contribution.

More detail: [USAGE.md](USAGE.md) · [RESEARCH.md](RESEARCH.md).

## License

[MIT](LICENSE). Not affiliated with Anthropic, OpenAI, or Meta.
Endpoint research began with [DSado88/claude-tracker](https://github.com/DSado88/claude-tracker);
QuotaHop uses its own Python implementation and authentication isolation design.
