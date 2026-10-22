# QuotaHop usage guide

QuotaHop reports **Claude, Codex, and Muse Code subscription quotas** in one
terminal table and, on request, switches the shared Claude Code login to the
subscription with the most room. Reports fetch each account once and exit. An
optional background worker keeps independently held logins renewed. Nothing
runs inference.

See the [README](README.md#quick-start) for installation and first captures.
`./quotahop` is the command; it uses the project's `.venv` automatically.
Run `./quotahop --help` for every option.

Contents: [reports](#check-usage) · [panel](#side-panel) · [capture](#capture-by-account-identity) ·
[vault and unlock cache](#vault-and-unlock-cache) · [Codex](#codex-setup) ·
[Muse](#muse-code--current-cli-login-no-setup) · [selection and readiness](#selected-accounts-and-readiness) ·
[switching](#switch-the-shared-claude-code-subscription) · [refresh](#refresh-saved-copies-on-demand) ·
[background renewal](#optional-background-authentication-renewal) · [data and files](#data-directory-and-files) ·
[security](#security-and-verification)

## Check usage

```sh
./quotahop
./quotahop --sort 7d --reverse
./quotahop --verbose
./quotahop --json
```

The default output is the table, split into **OpenAI**, **Anthropic**, and
**Meta** sections; empty sections are omitted. Accounts are sorted by
recommended next use within each section. Each row shows five-hour and weekly
percentages, local reset timestamps with countdowns, and account status.
`--verbose` (`-v`) adds notices, error explanations, observation times,
model-specific windows, and extra usage. JSON always includes full details.
Absent metrics remain unknown, never zero. Extra-usage monetary values remain
raw units because their currency/scale is not a verified API contract.

The report adapts to terminal width: full columns where they fit, compact
usage/reset columns otherwise, and stacked rows below 76 columns. Names wrap
without truncation. Compact resets show time remaining; `--verbose` includes
exact local timestamps.

Status glyphs are `● LIVE`, `◷ RECENT`, `✕ ERROR`, `◷ SNAPSHOT`, and
`○ NOT_CONFIGURED`. `● LIVE ✓` additionally marks a Claude switching candidate;
the checkmark is not a renewal indicator (use `--background status`).
`◷ RECENT` is a successful Claude check reused for up to 60 seconds. On a colour
terminal, usage below 50% is green, 50–79% amber, and 80% or higher red; unknown
values are muted. These are visual cues, not provider limit rules. Colour follows
`NO_COLOR`, `TERM=dumb`, and redirected output; override with `--color always` or
`--color never`. JSON never includes colour escapes.

### Error labels

A failed check is never shown as zero usage or exhausted quota. Labels:

| Label | Meaning |
|---|---|
| `EXPIRED` (`token_expired`) | The saved access token has expired. A grant QuotaHop maintains recovers on its own once the worker runs; otherwise recapture or re-enroll (see [renewal](#optional-background-authentication-renewal)). |
| `THROTTLED` | HTTP 429 from the usage endpoint. Quota is unknown, not exhausted. A shared cooldown honours `Retry-After` (five-minute minimum, one-day maximum). |
| `AUTH ERROR`, `BAD TOKEN` | The provider rejected the credential, or it is malformed. |
| `WRONG ACCOUNT`, `WRONG PROVIDER`, `DUPLICATE` | The credential does not match the saved row, belongs to another provider, or duplicates another saved token. |
| `TIMEOUT`, `NETWORK`, `TLS ERROR`, `HTTP ERROR`, `BAD DATA`, `REDIRECTED`, `BLOCKED`, `CACHE ERROR`, `CHECK FAILED`, `CHECKING` | Transport, response-format, allowlist, cache or worker problems. `--verbose` explains. |

JSON adds `error_code` and, for HTTP 429, `retry_after_seconds`.

### Ranking

The default `--sort recommended` balances spare capacity and reset urgency:

- Live accounts with headroom and future reset times come first, scored as
  `remaining percentage / (1 + hours until the earliest reset)`, highest first.
  Remaining percentage is the smaller of the reported five-hour and weekly
  headroom. The one-hour offset prevents a nearly empty account from winning
  solely because it resets in seconds.
- A missing window is ignored, so a subscription with only a weekly limit can
  still rank normally. Live headroom with missing or elapsed reset times comes
  next, ordered by remaining capacity. Elapsed resets never imply a fresh allowance.
- Accounts at or above 100% in either window come after usable accounts,
  sorted by when all exhausted windows are due to reset, where known.
- Errors, unconfigured accounts, historical snapshots, and accounts with no
  reported main-window usage come last.

This is a practical heuristic: percentages are relative to each subscription,
not comparable token counts. Model-specific allowances remain in `--verbose`.

Override with `--sort name`, `plan`, `5h`, `7d`, `5h-reset`, `7d-reset`, or `status`.
These sort ascending; add `--reverse` for descending. Usage sorts numerically,
resets chronologically, and missing values stay last. With `recommended`,
reverse changes the order within each availability tier, keeping usable accounts
first. Sorting also orders the JSON accounts array within provider groups and adds
no network requests.

### What a report does and does not do

Reports **never run inference or login and never renew a token themselves.**
Claude and Codex use at most one usage GET each (Claude can reuse a recent
check); Muse portal sources use one identity GET and one quota GET for the
selected team. These are first-party interfaces, not documented public HTTP
APIs or guaranteed integrations; see [research](RESEARCH.md).

Two side effects exist, both local and bounded:

- After a successful unlock, a report **restarts a stopped renewal worker** if
  grants need maintenance, unless you stopped it explicitly with
  `--background stop` or `--lock` (see [renewal lifecycle](#renewal-after-reboot)).
- For Claude grants that Claude Code currently holds (`cli_owned`), a report
  copies Claude Code's already-renewed access token into the encrypted vault.
  This uses read-only file reads of Claude Code's credentials and at most one
  profile GET, and requires an exact saved account/organisation match.

Provider authentication files are otherwise untouched. Only the explicit
`--switch`, `--switch-next` and `--undo-switch` actions change Claude Code's
auth, as [described below](#switch-the-shared-claude-code-subscription).

### Concurrency and shared cache

Independent account checks run concurrently using up to four short-lived Linux
worker processes (`--jobs 1` for sequential, `--jobs 2`–`8` to choose). This
overlaps network waits without adding requests. Duplicate credentials stay in the
same worker so they cannot trigger duplicate usage requests. Unconfigured rows
and snapshots are handled locally. Workers run only while the command is active
and are reaped before exit, including after timeout, Ctrl-C, termination, or a
partial startup failure; parent-death signals stop them if the parent is killed.
The eight-second per-request deadline applies, and an outer 18-second deadline
bounds the two-request Muse portal check.

Claude reports, panels and switches share a private 60-second usage cache with
per-account request locks. Cache hits keep the original check time, expire at
reset boundaries, and never survive a credential change. HTTP 429 starts a shared
cooldown. No retries or token refreshes are triggered. `--switch-next` after a
report therefore does not query every account again.

### JSON and exit codes

JSON includes each account's `status`, `live`, nullable `usage`, `captured_at`,
and safe errors. Top-level `live` is true only when all accounts returned live
data; `live_accounts` counts successful ones. Errors never fall back to a saved
snapshot or an old cached reading. Cache hits use `status: "recent"`,
`live: false` and the original `captured_at`; they remain switching candidates
only while under 60 seconds old. JSON stdout contains no prompts; the passphrase
prompt uses the terminal. A valid cached unlock works without a terminal; once it
expires a terminal is required (there is no echoed-password fallback).

Exit codes: `0` success; `1` retrieval/restart error; `2` input/vault error;
`3` unconfigured/unsupported accounts; `130` interrupted. Historical snapshots
can exit 0, so scripts should inspect `live`, not just the exit status.

### Removing accounts

```sh
./quotahop --remove 'you@example.com'
./quotahop --remove 'you@example.com' --plan codex
./quotahop --remove muse-1
```

Removal deletes the entry from configuration, its saved credential and renewal
grant from the encrypted stores, and any imported snapshot. It never contacts a
provider or changes provider login files. A vault unlock may be required.
Ambiguous names/emails are rejected; use `--plan` or the unique `id` from
`--json`. For a current-Muse-CLI row use its configured name or ID, since its
live email is not saved.

Identity labels take precedence over configured fallback names. Plans are
`max_20x`, `team_premium`, `codex`, and `muse_code`. Claude plan labels are
unverified; Codex returns its plan. Team seats each require their own
credentials; a Team organisation ID or owner login is not a substitute. Historical
snapshots from earlier versions remain available with `--import ACCOUNT_ID`;
they show `SNAPSHOT` and never count as live.

## Side panel

```sh
./quotahop --panel                  # any interactive terminal
./quotahop --panel --panel-interval 120
```

The compact panel (or the [Herdr plugin](plugins/herdr/README.md)) refreshes
every 60 seconds while it is running in the foreground (`--panel-interval`
30–3600 seconds), keeps its own unlock key in memory, and closes with `q`. `u`
unlocks using the normal cache or passphrase; **unlocking can restart a stopped
renewal worker**, which then outlives the panel (unless stopped explicitly).
`--lock` also locks open panels for the same vault. ↑/↓ selects a provider;
Enter previews the Next account and a second Enter confirms a Claude switch;
Esc cancels. Codex and Muse switching is unavailable. `d` toggles full details.

Per provider the panel shows **Active** (the subscription matching the current
CLI credentials) and **Next** (the best usable alternative, using the CLI
ranking), with five-hour and weekly remaining percentages. Codex rows are
matched by ChatGPT account ID; Claude rows by access token.

| Label | Meaning |
|---|---|
| `Active` | Verified against the current CLI credentials. Shows `Unverified` if it cannot be. |
| `Next` | Best verified alternative; for Claude it must also be ready to switch. |
| `Saved?` | The only saved account's quota, shown without claiming it is the active login. |
| `Best?` | Active cannot be verified; the best saved candidate's quota is shown and switching stays blocked. |
| `~` after a label | The reading is a reused cached check (at most 60 s old). |

When no alternative is usable, the Next line says why:

| Text | Meaning |
|---|---|
| `No other sub` | Only one subscription is saved for this provider. |
| `All N expired · re-enroll` | Every alternative's token expired and nothing is renewing them. |
| `Expired · renewing` | Alternatives are expired but a maintained grant is due to renew. |
| `Expired · renewal paused` | Renewal is `blocked` (unverified Claude build or missing tool). |
| `Throttled · quota unknown` | An alternative's usage check returned HTTP 429. |
| `Check failed · quota unknown` | An alternative's check failed for another reason. |
| `N expired · rest unusable` | Some alternatives expired and the rest cannot be used. |
| `At quota limit` | Every alternative is exhausted. |
| `Usage/auth unverified` | Usage or credentials could not be verified; nothing is claimed. |

Warnings at the foot of the summary:

| Warning | Meaning |
|---|---|
| `! Renewal off: quotahop --background start` | The worker is not running. |
| `! Renewal paused (n): unverified Claude build` | n grants are `blocked`; see [builds](#native-builds-and-sandbox). |
| `! Renewal stopped (n): re-enroll` | n grants are `suspended`; see [recovery](#recovery-and-re-enrollment). |
| `! CLI sync: …` | Following Claude Code's renewed token failed for a reason that will not resolve itself. |

## Capture by account identity

While the desired account is signed in through your normal workflow:

```sh
./quotahop --capture claude --plan team_premium
./quotahop --capture claude --plan max_20x
./quotahop --capture codex
```

Run the appropriate command for the current account, then switch normally before
capturing a different one. The capture detects its email/name, prints which
account was saved, and reuses its existing row or an empty matching template
slot (`--setup` creates seven Claude template slots; unknown slots keep
placeholders). You do not need to remember `team-1`, `max-2`, etc. Repeated
captures of the same identified account and subscription type update that row.
Distinct Codex workspace routing IDs are never merged just because they share
an email.

**Personal Max and a Team seat can share the same email.** Capture each while
that subscription is the current Claude login, specifying its plan:

```sh
./quotahop --capture 'you@example.com' --plan max_20x
# Switch to your Team subscription using your normal login workflow, then:
./quotahop --capture 'you@example.com' --plan team_premium
```

These create separate rows with separate copied access tokens. Recapture with the
same `--plan` to update only that subscription. An email shared by both rows
requires `--plan` or a unique saved ID. The same token cannot be captured as two
subscriptions. If an earlier capture overwrote one, recapture each subscription;
a replaced token cannot be recovered.

You can also select a saved account by email/name, or supply one for a new account:

```sh
./quotahop --capture 'you@example.com'
./quotahop --capture 'you@example.com' --plan team_premium
./quotahop --capture 'Work account' --plan codex
```

For a new email/name, specify `--plan`. If local identity is available, its email
takes precedence over a supplied display name; a supplied email that disagrees
is refused. If identity is missing, your supplied name/email is used as the
label, or use `--name 'Work account'` with a provider selector. Plan labels for
Claude remain user-supplied. Ambiguity within the same plan requires a unique
saved ID. Capturing by an existing ID cannot change its subscription type.

**Captures copy access tokens only.** A capture never saves or uses a refresh
token, never runs the provider CLI, and never changes the source credentials.
Captured tokens are reusable only while valid; they can expire or be invalidated
by later account switches. On an expired-token error, capture that account again
after your next normal login to it, or use
[independent enrollment](#optional-background-authentication-renewal) for a
renewable login. A Claude login that Claude Code holds can also become renewable
without enrollment: see [adopting a CLI login](#claude-renewal-ownership).

Claude reads only `oauthAccount` display fields from its local `.claude.json`
during capture and checks that the selected access token did not change during
the read. Codex extracts display fields from its existing access token's profile
claims. These are **local display hints, not independently verified identities**;
Claude metadata can be stale. No profile request, login, refresh, or CLI
invocation is added for naming. Old captures remain compatible: Codex names can
be decoded from saved tokens, and an old Claude capture is labelled from current
metadata only when its saved token exactly matches the current one (otherwise
recapture it once). Legacy IDs such as `--capture team-1` still work. Reports
never rewrite the vault or configuration for naming.

## Vault and unlock cache

The first capture asks you to create a vault passphrase of at least 12
characters; there is no default. Account secrets are encrypted with AES-256-GCM
under a scrypt-derived key; the passphrase is never stored. Never put the
passphrase or tokens in command arguments, configuration, chat, or screenshots.

After a successful passphrase entry, later commands reuse the unlock for
**five minutes** without extending it. It unlocks local storage; it does not sign
in to any provider. The cache holds only the derived key (never the passphrase)
in the Linux user keyring (`libkeyutils.so.1`) with a kernel-enforced 300-second
expiry and owner-only permissions. Nothing is written to disk and no daemon runs.
It is shared across your terminals under the same Linux user, so other processes
running as you can use it during that time. Each vault has its own entry. If the
keyring is unavailable the tool asks again next time and never falls back to
plaintext; `--verbose` shows the notice.

```sh
./quotahop --lock                  # clear the cached unlock, lock open panels, stop the worker
./quotahop --change-password       # alias: --reset-password
```

`--lock` stops this vault's renewal worker until `--background start`, signals
open panels to clear their displays, and clears only this vault's cache entry.
It does **not** remove a vault key stored by [autostart](#renewal-after-reboot);
it prints a reminder. `--change-password` preserves accounts, credentials,
renewable grants, and undo history; see the [README](README.md#change-the-vault-password)
for recovery from an interrupted change and forgotten-password limits.

The default Claude source is `$CLAUDE_CONFIG_DIR/.credentials.json` if that
variable is set, otherwise `~/.claude/.credentials.json`. Capture another file
read-only with `--credential-file /your/config/.credentials.json`.

## Codex setup

While the desired ChatGPT subscription is your current Codex account:

```sh
./quotahop --capture codex
./quotahop
```

Capture reads `$CODEX_HOME/auth.json` if that variable is set, otherwise
`~/.codex/auth.json`. It saves only `tokens.access_token`, its expiry when
present, and `tokens.account_id`, plus display identity when available, in the
encrypted vault. It never runs Codex, reads Codex's keyring, or copies
refresh/ID tokens. API-key-only and keyring-only logins are unsupported.

The report requests `GET https://chatgpt.com/backend-api/wham/usage` with the
saved access token and account-routing ID, and shows the server-reported plan,
five-hour/weekly usage and resets, additional model/code-review allowances, and
credit balances when returned. Windows of other lengths are displayed separately,
never mislabelled. API token counts are not quota usage. Capture more accounts
by switching normally and repeating `--capture codex`. A capture is usable until
expiry or revocation; for a renewable login use
[independent enrollment](#optional-background-authentication-renewal).

## Muse Code — current CLI login, no setup

To include the current Muse CLI login in the combined report, run
`./quotahop --add muse-current --plan muse_code --name Muse`, then add
`"source": "cli_current"` to that row in `.usage-data/accounts.json`, preserving
its private permissions. The configuration holds labels, never credentials. The
row follows whichever Muse login is current. To check only Muse:

```sh
./quotahop --muse-cli-current
./quotahop --muse-cli-current --json
```

This action needs no vault, passphrase, or configuration. It reads
`$XDG_CONFIG_HOME/muse/auth.json` (normally `~/.config/muse/auth.json`) with
`O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_NOATIME`, neither invokes the Muse
launcher nor writes any authentication file.

**This route is a bootstrap POST, not a documented read-only usage API.** The
installed CLI obtains subscription quota from `POST https://api.meta.ai/muse-code/key`.
The checker sends exactly one request with `{"onboard":false}` using the existing
device access token, requires an already provisioned API key, checks that Meta
returned that same key, and discards the returned credentials. No login,
refresh, onboarding, inference, redirects, or retries are attempted. Key reuse
was observed during verification ([research](RESEARCH.md#muse-code)), which is
**not a guarantee about future server-side behaviour**; detecting a changed key
cannot undo a server-side change. Use the GET-only portal route if a strictly
read-only contract is required. The bootstrap route is selected only by
`--muse-cli-current`, a row with `"source": "cli_current"`, or an enrolled Muse
row; it is never a fallback. Only one row may use the current login. Removing
`source` restores portal capture, and a configured CLI source takes precedence
over saved portal credentials and snapshots, including on failure.

Only quota, plan, and sanitised email/name fields enter the report; keys, billing
details and action URLs are discarded. If Meta omits `subs_usage`, the tool
reports `usage_unavailable`, not 0%. Reported percentages above 100% are kept as
overage; negative and non-finite values are invalid. Session token counts are not
subscription quota.

## Muse Code setup — real portal subscription quota

The checker can read the subscription-quota GET used by Meta's `dev.meta.ai`
frontend, using a **browser portal session** separate from CLI authentication.
This route never opens `~/.config/muse/auth.json`, invokes Muse, mints an API
key, or refreshes any login. It has been exercised with mocked responses; a live
authenticated result is not claimed here.

1. Sign into **https://dev.meta.ai/usage** in your browser with the desired account.
2. Open Developer Tools → **Application** (Chrome) or **Storage** (Firefox) →
   **Cookies** → `https://dev.meta.ai`. Copy the **value only** of `llama_dev_sess`,
   not the whole Cookie header or a cURL command.
3. Run `./quotahop --capture muse` and paste the cookie into the **hidden prompt**.

The checker reads your portal identity and available teams, asks you to select a
team ID explicitly (even if only one is listed; `--muse-team TEAM_ID` supplies it
non-interactively and must be in the returned list), checks that team's quota,
and encrypts the cookie with its identity/team binding. Setup performs three
GETs. To reuse the `muse-1` row instead of adding a portal row, remove its
`source: cli_current` setting first. Add further subscriptions with
`./quotahop --capture 'second@example.com' --plan muse_code`.

Each later check verifies the saved identity and reads the selected team's quota
once. The cookie is reusable until it expires or is revoked; re-run capture then.
No cookie jar, `Set-Cookie` update, sign-in or renewal is used. The result is
**live subscription quota for the selected portal team**, not PAYG API spending.
Percentages come from reported weighted usage divided by limits; other window
durations are shown separately; missing, zero-denominator or stale usage stays
unknown; overage is not clamped. The portal does not prove its link to a Muse CLI
login, so JSON carries `quota_scope: selected_meta_portal_team` and
`cli_account_verified: false`. This is an **undocumented web interface**.
Authentication failures, schema changes and redirects produce safe errors; there
is no fallback to `/muse-code/key`, inference or another team.

Historical exports remain optional: `./quotahop --import-muse muse-1 < usage-result.json`
accepts a `usage/read` result with `usage.observedAtMs`, `tier`, `window` and/or
`weekly`. Imports show **SNAPSHOT** with the original timestamp and never count
as live. A saved live credential always takes precedence.

## Selected accounts and readiness

The combined table highlights **▶ SELECTED** in cyan and includes a small key.
**○ Standby** marks other captured logins when the selected login is identified;
**? Unknown** means selection cannot be established safely. Detection compares
the saved credential with read-only CLI credentials before and after fetching
usage: **Claude by exact access token, Codex by ChatGPT account ID** (an enrolled
Codex login has its own tokens, so the account ID is its identity). Same-email Max
and Team rows cannot both match. Muse's current-CLI row follows that login;
portal sessions are not assumed to be the selected CLI account.

This identifies the CLI's current credential files, **not the account cached by
every already-running process**. Unknown credentials, environment overrides,
rotation, or a login change during the check produce an unknown marker. These
badges need no extra network calls or provider-auth writes.

The **Switch** column shows Claude readiness and the saved access token's
remaining lifetime. `✓ Ready` means live reported headroom and a token with more
than a minute left; the switch still validates the profile immediately before
writing. Expired, expiring, unknown-expiry, missing-scope, unavailable-usage,
exhausted, and unverified-identity states are distinct. Codex and Muse show `—`
because only Claude is switched.

## Switch the shared Claude Code subscription

```sh
# Automatically choose and switch to the best usable Claude subscription:
./quotahop --switch-next
# Preview, or restrict the choice to Team seats:
./quotahop --switch-next --dry-run
./quotahop --switch-next --plan team_premium
# Choose a specific subscription yourself:
./quotahop --switch 'you@example.com' --plan max_20x
./quotahop --switch team-1
# Validate the target without changing Claude files:
./quotahop --switch team-1 --dry-run
# Restore the preceding local Claude Code authentication:
./quotahop --undo-switch
```

`--switch-next` uses exactly the default table ranking. It checks each eligible
Claude account once (bounded concurrency); Codex and Muse are not queried.
Expired, nearly expired, missing-scope, exhausted, failed, and unknown-usage
accounts cannot win, nor can snapshots. If nothing qualifies, or the winner is
already selected, authentication stays untouched. The final profile check must
still pass; it never silently switches to a different identity. `--sort` and
`--reverse` are rejected.

Switching is **Claude-only** and runs only when explicitly requested. It checks
the saved token's expiry and makes one `GET` to
`https://api.anthropic.com/api/oauth/profile` (the installed CLI's undocumented
first-party profile endpoint, under its own exact GET-only allowlist) to validate
the account and Max/Team organisation type. Wrong emails, mismatched plan types,
ambiguous selections, and expired or nearly expired tokens are refused before
anything changes.

The only changed JSON values are `claudeAiOauth` in `.credentials.json` and
`oauthAccount` in `.claude.json`. All surrounding bytes are preserved, including
`designOauth`, `mcpOAuth`, settings, and caches, as are file modes and group
ownership. No logout/login command, CLI process, inference, token refresh,
environment change, or symlink is used. Existing `CLAUDE_CONFIG_DIR` and
`CLAUDE_SECURESTORAGE_CONFIG_DIR` are honoured without alteration (defaults
`~/.claude/.credentials.json` and `~/.claude.json`). Conflicting environment auth
overrides, Console/Gateway credentials, symlinks, unsafe permissions, and
unsupported legacy layouts are refused.

### Claude renewal ownership

**Every Claude grant has exactly one renewal owner.**

- **`ready`**: QuotaHop holds the grant and renews it in the background.
- **`cli_owned`**: Claude Code holds it and renews it while it is the active
  login. QuotaHop mirrors Claude Code's latest copy into the encrypted renewal
  registry so it can take the grant back.

`--switch`/`--switch-next` give the target's **full grant** to Claude Code
(`cli_owned`). While the switch holds Claude Code's refresh locks
(`.oauth_refresh.lock` in the Claude config directory and the legacy
`<config dir>.lock`, e.g. `~/.claude.lock`; waited for up to 10 seconds, then the
switch fails with `claude_refreshing` and changes nothing) plus
`.storage-write.lock` and `.claude.json.lock` (which fail immediately if held),
the outgoing login's latest copy is taken **back from Claude Code** and returned
to QuotaHop's background renewal (`ready`). Claude Code re-reads its credentials
under the refresh lock and adopts a changed file instead of refreshing, so
running sessions move to the new login and never refresh the returned grant. No
login is lost by switching, and sessions may need a moment to adopt the file.
`--dry-run` changes neither ownership nor provider files. A failed switch is
reconciled on the next sync.

The worker keeps this true afterwards. It mirrors Claude Code's renewals of the
active grant and, when the CLI login changes **outside QuotaHop** (for example
`/login`), takes the previous account back and adopts the new login. This
includes accounts you only captured and never enrolled, provided the login can be
attributed by an exact token match or a server-verified account/organisation
profile match against a saved binding (made by `--refresh` or an earlier
switch). So **sign in once in Claude Code and QuotaHop keeps that login
renewable.** `--capture` itself still copies access tokens only. If the current
login cannot be attributed, nothing is taken back. `legacy_hold` grants (older
ones with no ownership record) resume renewal once the CLI is confirmed to hold a
different login.

A target QuotaHop holds only as an **access-only capture** is installed without a
refresh token (it lasts until that token expires or is revoked); after it
expires, sign in through Claude Code and QuotaHop adopts the new login as above.

### Undo

The last pre-switch Claude auth fields are saved in the separate encrypted
`.usage-data/switch-backup.json` before either file changes. It includes the
outgoing refresh token when present, solely for restoration, and never contains
Design or MCP credentials. `--undo-switch` makes no network request and restores
only those two fields. It refuses if a later login changed the installed access
token, and refuses if QuotaHop again holds the saved refresh token
(`Use --switch ACCOUNT instead`): the record's copy goes stale once QuotaHop
renews that grant. A successful undo deletes the record; another switch replaces
it. Reports never read it. Nothing is revoked server-side.

### File format, locks and failure handling

The switch uses Claude Code's own short-lived mkdir locks, never steals one,
checks for concurrent edits, and replaces each file atomically. The two files
cannot be replaced as one transaction, so a failed second write rolls back only
the auth fields. Locks and temporary files are removed on normal completion or
handled errors; hard termination can leave a lock or partial switch, and the
undo record is written before the first write. The file format and locks were
inspected in Claude Code 2.1.296 and checked through 2.1.308. This is an
implementation-dependent local switch, not an official switching API.

## Refresh saved copies, on demand

```sh
./quotahop --refresh
./quotahop --refresh --verbose
```

This is **credential synchronisation and a readiness check, not OAuth token
renewal**. While your captured Claude tokens are still valid it establishes
server-verified account UUID + organisation UUID bindings in the encrypted vault,
using the same allowlisted profile GET as switching (at most one per unbound
token) and then fetches usage once per account. No token-renewal endpoint is
allowed.

When Claude Code has already renewed its current access token, `--refresh` can
copy it into the matching saved account. A changed token requires one profile GET,
an exact saved account/organisation/plan match, a later expiry, and a stable
source reread. It never guesses from email, copies a refresh token, or changes
provider files. After a binding exists, `--switch-next` performs the same
synchronisation before ranking (`--dry-run` skips its writes). Ordinary reports
and the panel follow Claude Code's token the same way, only for grants
Claude Code holds.

Inactive expired captures cannot be kept ready by this: they contain no
renewable grant, and the tool never runs a warm-up prompt or rotates a copied
refresh token. If Claude Code's current file has no newer matching credential,
recapture after your normal sign-in, or enroll the account. Codex captures need
recapture; Muse's current-CLI source reads the current login each time.

The output remains the table; `--verbose` explains synchronisation outcomes and
`--json` includes `credential_sync` and `oauth_renewal: false`. See the
[refresh assessment](RESEARCH.md#policy-and-compatibility-limits) for why this
boundary exists.

## Optional background authentication renewal

**Claude and Codex:** grants QuotaHop holds are renewed automatically, five
minutes before expiry. **Muse:** independent device sign-in, usage reporting, and
background expiry tracking are supported; the installed Muse CLI can re-mint an
API key but does not renew its device access token, and this tool does not
substitute key re-minting. Muse reports `reauth_required` at expiry.

Enroll each saved account once. Successful enrollment starts the worker:

```sh
./quotahop --auth-enroll 'you@example.com' --plan max_20x
./quotahop --auth-enroll 'you@example.com' --plan team_premium
./quotahop --auth-enroll 'you@example.com' --plan codex
./quotahop --auth-enroll muse-1
./quotahop --background status
```

`--plan` is optional when an email, name, or ID identifies exactly one saved
subscription; for an email shared by Max and Team, give `--plan` or the unique ID
(`--auth-enroll team-2`). An ambiguous match is rejected before any worker is
stopped. For a new row use `--add ID --plan PLAN --name EMAIL` first. Claude uses
its native browser/code sign-in; Codex and Muse display device sign-in
instructions. Complete each with the intended account and organisation: existing
email, workspace and organisation bindings must match, and same-email Max and
Team subscriptions remain separate. An expired or revoked capture does not block
a fresh sign-in, since enrollment never contacts the provider with the old token.
For legacy Claude captures without an organisation binding, the new profile
establishes it after email and plan checks; duplicate bound subscriptions are
rejected.

Enrollment signs in separately inside a sandbox and does not read Claude Code's
or Codex's credential files. Enrolling Muse makes that row use its saved
independent login even if it had followed `source: cli_current`. Muse enrollment
uses its native key bootstrap with onboarding disabled; it is not read-only.

Codex renewal uses its documented `account/read` RPC with `refreshToken: true`.
Claude sends exactly one initialization control frame to an empty isolated native
CLI and closes input: no user message, prompt, turn, tools, or session resume.
Offline tests confirmed this saves the rotated grant without inference
(`claude auth status` is unsuitable: it can exit before saving). After a Claude
renewal QuotaHop makes no profile request; the existing account binding is kept.

Renewal updates only this tool's encrypted renewal registry
(`.usage-data/renewal.json`, a separate AES-GCM context under the vault key) and
the access-token vault. The worker never polls quotas or switches global auth.
For who owns a Claude grant after a switch or `/login`, see
[Claude renewal ownership](#claude-renewal-ownership).

### Renewal after reboot

The worker holds the vault key **only in memory**, so a reboot or worker exit
stops it. Any interactive unlock (a report, or `u` in the panel) restarts it if
grants need maintenance, so logins that expired meanwhile recover within seconds
(grants keep long-lived refresh tokens). The exception is an **explicit stop**:
after `--background stop` or `--lock` the worker stays down, and unlocks and
autostart leave it down, until `--background start` (or a successful
`--auth-enroll`). No service, cron job or startup entry exists unless you opt in:

```sh
quotahop --background enable-autostart   # unlocks once (cache or passphrase)
quotahop --background disable-autostart  # removes the stored key and login item
```

Autostart stores the derived vault key in your desktop login keyring (Secret
Service, e.g. GNOME Keyring, unlocked by PAM at login) and writes
`~/.config/autostart/quotahop-renewal.desktop`. The login item waits up to 60
seconds for the keyring, **never prompts**, quietly does nothing when no key is
available, and respects an explicit stop. It needs `jeepney` and `secretstorage`
(in `requirements.txt`) and a running Secret Service. `--lock` does not remove
the stored key (it prints a reminder); use `disable-autostart`. Password changes
update the stored key.

**Trade-off:** while you are logged in, any process running as you can read the
key, and at rest it is protected by your login password through the keyring
rather than by the vault passphrase. Processes running as you can already read
Claude Code's and Codex's active logins in plaintext, and could modify a
user-writable QuotaHop install to capture the passphrase, so the practical loss is
small on a single-user machine with a strong login password. Avoid it with
auto-login, an empty keyring password, or shared accounts.

Other lifecycle rules: `--background status` and `stop` need no passphrase.
Enrollment pauses an existing worker and restarts it afterwards, reusing its
unlock even if sign-in outlasts the five-minute cache; a failed or cancelled
sign-in restores a previously running worker (Ctrl-C cancels), and a first-time
failure starts none. If startup fails after a successful sign-in, the CLI says the
login was saved and exits 1; retry `--background start` without enrolling again.
The worker stops by itself when no grants remain to maintain.

### Status

`--background status` (add `--json` for expiry and next-renewal times, never
tokens) shows the last observation; a stopped worker does not renew.

| State | Meaning |
|---|---|
| `ready` | QuotaHop holds the grant and renews it five minutes before expiry (Muse: expiry tracking). |
| `blocked` | A `ready` grant is paused because no verified Claude build or required tool is available. The refresh token was never sent; retried every 10 minutes. |
| `cli_owned` (mode `cli_renewal`) | Claude Code holds and renews it while active; QuotaHop mirrors it and takes it back when the CLI moves on. |
| `legacy_hold` (mode `renewal_hold`) | An older grant with no ownership record, held until the CLI is confirmed to hold a different login. |
| `in_flight` | A renewal request was sent and not yet recorded. An interrupted one becomes `suspended`. |
| `suspended` | Renewal outcome uncertain. Never replayed automatically. |
| `reauth_required` | A Muse device sign-in expired. |
| `needs_enrollment` | No grant is saved for this row. |

### Recovery and re-enrollment

Re-enroll only for `suspended` or `reauth_required`, or when a refresh token is
expired or revoked. **An `EXPIRED` access token on a maintained grant recovers on
its own** once the worker runs; do not re-enroll for it.

```sh
./quotahop --background stop
./quotahop --auth-forget 'you@example.com' --plan codex
./quotahop --auth-enroll 'you@example.com' --plan codex
```

Forget deletes only the local renewable grant, retaining its last access token;
it never logs out or revokes a provider session. `--remove` also deletes the
grant. Capture refuses to overwrite an enrolled account until it is forgotten.

Refreshes are serialised. An encrypted in-flight marker is saved before calling
the provider, and rotated credentials are persisted before their access tokens
are published. Any failed or interrupted refresh **suspends** that account;
an uncertain rotation is never replayed.

### Native builds and sandbox

Enrollment and renewal need the relevant **native provider CLI** and
**bubblewrap** (`sudo apt install bubblewrap`). Native authentication runs with a
fresh home inside a filesystem/PID sandbox; your home, CLI credential stores,
Design auth, keyrings, and shell configuration are not mounted, and only that
subprocess sees the isolated `HOME`/`CODEX_HOME`. If isolation cannot be created
the operation fails; there is no unsandboxed fallback.

The behaviour of the no-prompt startup path was tested offline, and the
executables are pinned by SHA-256: **Claude Code 2.1.296's Linux binary** and Muse
1.4.6-R6000.1 (`isolated_auth.BUILDS`). Other builds, even of the same version,
are refused until verified; a pin is never removed merely to support a newer
binary. Muse updater wrappers are not executed. For Claude, QuotaHop looks for the
pinned build in the `claude` on your PATH, sibling builds in Claude's versions
directory, and a **retained copy** at
`${XDG_DATA_HOME:-~/.local/share}/quotahop/claude-<hash16>` — a hard link (copy
fallback) that QuotaHop keeps so that Claude Code pruning old versions cannot stop
renewal. If no build verifies, renewal pauses as `blocked` and retries every 10
minutes instead of suspending, because the refresh token was never sent. (The
switch file format and locks were checked separately, through Claude Code
2.1.308.)

Renewable grants are encrypted in `.usage-data/renewal.json`. Native auth files
exist briefly in a `0700` temporary directory as `0600` files and are removed on
normal and error cleanup. A forced kill or power failure can leave that
directory; no secure-erasure claim is made. The worker disables core dumps and
process-memory inspection. Root or a compromised user account is outside the
protection boundary.

## Data directory and files

`--data-dir DIR` selects the private storage directory (default `.usage-data`
beside the checkout). It must be owned by you with mode `0700`, may not be inside
a Claude/Codex/Muse/AWS/git directory, and is created only by `--setup`. The Herdr
plugin and the autostart login item pass it explicitly.

Inside it (`0600` files): `accounts.json` (labels and plans, no credentials),
`vault.json` (encrypted access-token vault), `renewal.json` (encrypted grants),
`renewal-status.json` and the stop/lock markers, `switch-backup.json` (encrypted
undo record), imported snapshots, `quota-cache/` (parsed metrics only), and
`vault-rekey.json` only during an interrupted password change.

Outside it, QuotaHop may create:

- the **retained Claude build** at `${XDG_DATA_HOME:-~/.local/share}/quotahop/claude-<hash16>`
  (see [native builds](#native-builds-and-sandbox));
- the autostart login item `${XDG_CONFIG_HOME:-~/.config}/autostart/quotahop-renewal.desktop`
  and a vault-key item in the login keyring, only after `--background enable-autostart`;
- Claude Code's two auth fields in `~/.claude/.credentials.json` and `~/.claude.json`,
  only on an explicit switch or undo.

## Security and verification

- **Network allowlists.** Usage clients allow only `GET` to
  `https://api.anthropic.com/api/oauth/usage` and
  `https://chatgpt.com/backend-api/wham/usage`, plus `https://dev.meta.ai` paths
  `/api/auth/me`, `/api/portal/teams` (setup only), and
  `/api/portal/teams/{numeric_team_id}/subscription-quota`. A separate allowlist
  permits `GET https://api.anthropic.com/api/oauth/profile` for switching,
  `--refresh` and following Claude Code's renewed token. No query strings,
  redirects, alternate hosts, proxies, inference paths, token exchanges, or
  retries; TLS verification is on. Requests identify as `quotahop/1.0`, not
  Claude Code. Each has an eight-second deadline including DNS and body reads.
  Native Claude/Codex/Muse processes started for enrollment or renewal make their
  own first-party authentication requests inside the sandbox; they are not
  QuotaHop HTTP clients.
- **Muse bootstrap.** `--muse-cli-current`, a Muse row with `source: cli_current`,
  or an enrolled Muse row allows one `POST https://api.meta.ai/muse-code/key` with
  the fixed onboarding-disabled body. This does not enable POSTs in the other
  clients or permit inference.
- **Source files.** Claude/Codex `--capture` and Muse CLI sources read with
  `O_RDONLY`, `O_NOFOLLOW`, `O_NONBLOCK` and `O_NOATIME`, require an owned private
  regular file, and never write, chmod, lock, rename, or refresh it. Claude
  display metadata uses the same flags and need not be private. Reports and the
  worker read Claude Code's credentials the same way to follow its login.
- **Vault.** Access tokens, expiries, display identities, Codex routing IDs, Muse
  device tokens/API keys, and portal cookies with bindings are in the AES-256-GCM
  vault with a scrypt-derived key. See [the unlock cache](#vault-and-unlock-cache)
  and [renewal after reboot](#renewal-after-reboot) for where a derived key may
  live. A lost passphrase means recapturing tokens once the unlock expires.
- **Files.** `.usage-data` is `0700`; files are `0600`. Reports and captures do
  not modify provider stores or keyrings. Explicit `--switch`/`--undo-switch`
  edits the two Claude Code auth fields. Nothing remains connected after a
  report exits, except a renewal worker started by an unlock or enrollment.
- **Secrets.** Tokens and raw server error bodies are never logged or placed in
  JSON. Runtime memory necessarily holds decrypted tokens while a check runs, and
  encryption does not protect against a compromised process or root.

## Run the tests

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

Tests use fake credentials, temporary directories, and mocked provider responses;
none use real credentials or inference. CI runs the suite on Python 3.12 and
3.14. Passing mocked tests does not establish live endpoint access for your
accounts.
