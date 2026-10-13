# QuotaHop

**Know what's left. Pick what’s next.**

A local Ubuntu CLI for **Claude, Codex, and Muse Code subscription usage**.
See your accounts in one compact, colour-coded report, ranked by spare capacity
and how soon their limits reset. No inference requests.

- Five-hour and weekly usage, reset countdowns, and selected-account indicators.
- Provider groups, terminal-width-aware tables, meaningful colours, and JSON output.
- Multiple subscriptions per email, including personal Claude Max and Team seats.
- Encrypted local credentials with a five-minute unlock cache.
- Explicit Claude subscription switching, with preview and undo.
- Optional independent authentication enrollment and background renewal.

QuotaHop is an unofficial personal utility. Usage interfaces are undocumented
first-party endpoints; access and response formats may change. It is not affiliated
with Anthropic, OpenAI, or Meta. No hosted service or telemetry is provided.
See [interface research and limitations](RESEARCH.md).

## Install

Requires Ubuntu/Linux and Python 3.12+. Install `python3-venv` if necessary.

```sh
git clone https://github.com/turbokast/quotahop.git
cd quotahop
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./quotahop --setup
```

Setup creates a private `.usage-data/` directory and an optional template of two
Max 20× accounts and five Team Premium seats. It performs no login or network
requests. Add or remove rows to match your subscriptions. The original
`./claude-usage` command remains equivalent and uses the same local data.

## Capture your accounts

While the desired account is already signed in through its normal CLI:

```sh
./quotahop --capture claude --plan max_20x
./quotahop --capture claude --plan team_premium
./quotahop --capture codex
./quotahop
```

Capture each command's account while it is current. Names/emails are detected
where available. Repeating a capture updates the matching subscription; a Max
subscription and Team seat with the same email remain separate.

The first capture asks you to **create** a vault passphrase of at least 12
characters. There is no default password. Unlocks last five minutes where the
Linux keyring is available. Never put credentials in command arguments or config.

Captures copy access tokens only. They do not log in, refresh tokens, or change
existing Claude/Codex credentials. Captured tokens eventually expire; recapture
after your normal login, or use independent enrollment below. Team seats each
need their own credentials; an organisation owner's login cannot read every
member's subscription quota. API-key-only and keyring-only Codex captures are
not supported.

For **Muse**, check the current CLI login directly:

```sh
./quotahop --muse-cli-current
```

To include it in the combined report, add a row:

```sh
./quotahop --add muse-current --plan muse_code --name Muse
```

Then add `"source": "cli_current"` to that row in `.usage-data/accounts.json`,
retaining its private permissions. That row follows the current Muse login.
Alternatively, use `--capture muse` for a separately saved portal session or
independent enrollment for a separate CLI login; see [Muse setup](USAGE.md#muse-code-setup--real-portal-subscription-quota).

**Muse CLI usage uses a bootstrap POST** to `/muse-code/key` with onboarding
disabled. It requires an existing key, compares the returned key, and discards
returned credentials. This is not a documented read-only API contract: observed
key reuse does not guarantee future server behaviour. The portal alternative
uses GETs, but still awaits authenticated end-to-end verification.

## Everyday commands

```sh
./quotahop                         # Table only; fetch once and exit
./quotahop --verbose               # Model allowances, exact local resets, details
./quotahop --json                  # Structured output without credentials
./quotahop --sort 7d               # Sort within provider groups
./quotahop --remove 'you@example.com' --plan team_premium
./quotahop --lock                  # Stop renewal and forget the cached unlock
```

The default ranking prefers live accounts with spare capacity and sooner resets:
`remaining percentage / (1 + hours until earliest reset)`. Remaining percentage
is the smaller known five-hour/weekly headroom. Exhausted, unavailable, and
unconfigured accounts rank below usable ones. This is a heuristic, not a
comparison of absolute token allowances across plans. Missing metrics stay `—`.

## Switch Claude subscriptions

Switching is explicit and changes the **shared Claude Code subscription login**:

```sh
./quotahop --switch-next --dry-run
./quotahop --switch-next
./quotahop --switch 'you@example.com' --plan team_premium
./quotahop --undo-switch
```

The switch preserves unrelated Claude configuration, Claude Design and MCP auth,
uses no symlinks, and changes no global environment variables. It installs an
access token without an owned refresh token. Already-running sessions may need
to reload credentials. Only Claude switching is implemented.

## Optional independent renewal

Independent enrollment creates a separate login owned by QuotaHop. It requires
the relevant native provider CLI and `bubblewrap` (`sudo apt install bubblewrap`).
The sandbox must be usable on your system; there is no unsandboxed fallback.

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

Use a saved ID or an unambiguous email/name. Add new rows with `--add` first.
Use `--plan` when the same email identifies multiple subscriptions.

**Successful enrollment starts a background worker automatically.** Claude and
Codex grants renew before expiry; Muse supports separate login and expiry
tracking only, not automatic device-token renewal. The worker never polls usage,
starts inference, or switches global auth. It keeps the vault key in memory
until stopped. No service, cron, or boot startup is installed.

Renewal updates the **vault**, not the currently installed global Claude token.
Run `--switch` or `--switch-next` again to install a renewed token. Shared CLI
refresh tokens are never imported. Failed or uncertain rotations suspend the
account instead of replaying a possibly consumed refresh token.

**Build compatibility is deliberately narrow:** Claude 2.1.296 and Muse
1.4.6-R6000.1 authentication were inspected and tested on specific Linux binaries.
Their SHA-256 digests are pinned in `isolated_auth.py`; even a different build
with the same version is refused. Do not bypass these checks to enable renewal.
Read-only token capture does not require these pinned binaries. See the
[full renewal guide](USAGE.md#optional-background-authentication-renewal).

## Security and tests

Credentials stay in an AES-GCM encrypted vault with a scrypt-derived key.
Private directories use mode `0700`; stored configuration and credentials use
`0600`. Secret material is excluded from JSON reports and error messages.
Read-only captures do not mutate existing provider authentication. Explicit
switches and independent enrollment have the distinct effects described above.

Usage clients allow only specific endpoint paths and HTTP methods. Requests and
foreground workers have deadlines; workers are reaped on exit. Authentication
workers only start through enrollment or explicit `--background start`.
Root or a compromised local user can still access unlocked process memory.
Provider policies and undocumented interfaces can restrict this functionality;
this project does not promise provider approval or uninterrupted compatibility.

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

Tests use fake credentials and mocked provider responses. They cover parsing,
seven-account isolation, secret redaction, endpoint restrictions, concurrency,
terminal layouts, switching, enrollment, and renewal failure handling. A native
sandbox check is skipped when its CLI dependencies are unavailable. Automated
tests do not prove live endpoint access for every subscription.

For more commands, exit codes, troubleshooting and implementation details,
see [USAGE.md](USAGE.md), [RESEARCH.md](RESEARCH.md), or `./quotahop --help`.
Contributions should include focused tests and must never include credentials,
account files, or real account fixtures.

## License and acknowledgement

[MIT](LICENSE). Endpoint research began with
[DSado88/claude-tracker](https://github.com/DSado88/claude-tracker); QuotaHop uses
its own Python implementation and authentication isolation design.
