# QuotaHop usage guide

See [README](README.md) for installation and a quick start.
`./quotahop` and the original `./claude-usage` command are equivalent.

To change or reset the vault password without losing accounts, use
`quotahop --change-password` (alias `--reset-password`). See the
[password-change guide](README.md#change-the-vault-password) for worker restart,
interruption recovery, and forgotten-password limitations.

In the compact report, `● LIVE` means usage was fetched successfully;
`● LIVE ✓` additionally marks a Claude switching candidate. The checkmark is
not a background-renewal indicator; use `--background status` for that.
`◷ RECENT` identifies a successful Claude check reused for up to 60 seconds.
Reports, switches and panels share this cache; original check times are preserved.
`THROTTLED` means HTTP 429 from the usage endpoint, not exhausted subscription
quota. A shared cooldown honors Retry-After, with a five-minute minimum and a
one-day maximum. Authentication, timeout, network and malformed-response errors
also have distinct labels; `--verbose` includes the explanation.

For an explicitly opened, refreshing side panel, use `quotahop --panel` or the
[Herdr plugin](plugins/herdr/README.md). It refreshes every 60 seconds only while
running in the foreground, keeps its own unlock in memory, and closes with `q`.
`--panel-interval SECONDS` changes the interval (30–3600). Ordinary reports below
remain one-shot. `--lock` also locks open panels for the same vault.
In the panel, ↑/↓ selects a provider; Enter previews Next and a second Enter
confirms a Claude switch. Esc cancels. Codex/Muse switching is unavailable.
For the CLI, `--switch-next --provider claude` explicitly selects the provider.

Checks **Claude, Codex, and Muse Code subscription quotas**, then exits.
Claude/Codex use at most one GET each (Claude can reuse a recent check). Muse portal sources use one identity GET and one subscription
quota GET for the portal team you select. Ordinary reports perform no inference, login,
logout, OAuth renewal, or polling. Explicit independent Claude/Codex enrollment and
background renewal are available below. Muse supports independent enrollment and expiry
tracking; automatic renewal of its device access token is unavailable. These are first-party usage endpoints, not documented public HTTP
APIs or guaranteed integrations; see [research](RESEARCH.md). An explicit
`--muse-cli-current` option also checks the current Muse CLI login via its
bootstrap POST, described below. A combined-report Muse row can explicitly
select that route with `"source": "cli_current"`. Other account sources retain
their GET-only behavior.

The report adapts to your terminal width: full columns where they fit, compact
usage/reset columns otherwise, and stacked rows below 76 columns. Names wrap
without truncation. Compact resets show time remaining; `--verbose` includes
exact local timestamps and full details. `--json` is unaffected.

Reports, captures, and `--refresh` preserve provider authentication. Explicit
`--switch`, `--switch-next`, and `--undo-switch` change only shared **Claude Code** subscription
auth, preserving Claude Design, MCP credentials, and unrelated configuration.

Setup creates seven optional Claude template slots. Captured accounts
are identified by **email or account name**; unknown slots keep placeholders.
Muse CLI usage has been verified live; the portal reader still awaits an
authenticated check. See [verification details](RESEARCH.md#installed-muse-cli-verification).

## Optional background authentication renewal

**Claude and Codex:** independently enrolled accounts renew automatically, five
minutes before expiry. **Muse:** independent device sign-in, usage reporting, and
background expiry tracking are supported. The installed Muse CLI can re-mint an
API key but does not renew its device access token. This tool does not substitute
key re-minting for token renewal; it reports `reauth_required` at expiry.

Enroll each saved account once. Successful enrollment starts the background worker automatically:

```sh
./claude-usage --auth-enroll 'you@example.com' --plan max_20x
./claude-usage --auth-enroll 'you@example.com' --plan team_premium
./claude-usage --auth-enroll 'you@example.com' --plan codex
./claude-usage --auth-enroll muse-1
./claude-usage --background status
./claude-usage
```

Repeat the relevant enrollment command for each subscription. **`--plan` is optional**
when an email, name, or ID identifies exactly one saved subscription: the tool uses
that row's plan and verifies the new login against it. For an email shared by Max
and Team, specify `--plan` or use the unique subscription ID, e.g.
`--auth-enroll team-2`. An ambiguous match is rejected before stopping any worker;
the tool never guesses which subscription you intend. For a new row, use `--add ID --plan PLAN --name EMAIL`
first. Claude uses its native browser/code sign-in; Codex and Muse display native
device sign-in instructions. Complete each sign-in with the intended account and
organisation. Existing email, workspace and Claude organisation bindings must
match. Same-email Max and Team subscriptions remain separate. Expired or revoked
captures do not block a fresh independent sign-in: enrollment never contacts the
provider using the old token. For legacy Claude captures without an organisation
binding, the newly authenticated profile establishes that binding after email
and plan checks. Select the intended organisation during sign-in. An existing
organisation binding must still match, and duplicate bound subscriptions are rejected.

Only new, independently signed-in grants qualify. Shared CLI refresh tokens are
never imported. Enrolling Muse makes that row use its saved independent login,
even if it previously followed `source: cli_current`. Other Muse sources are
unchanged. Muse enrollment uses its native key bootstrap with onboarding disabled;
it is not a read-only operation. Its subsequent reports use the existing guarded
bootstrap usage reader and reject a changed API key.

Codex renewal uses its documented `account/read` RPC with `refreshToken: true`.
Claude sends exactly one initialization control frame to an empty isolated native
CLI and closes input: no user message, prompt, turn, tools, or session resume.
Offline tests verified that this saves the rotated grant without inference.
`claude auth status` is unsuitable because it can exit before saving rotation.

Renewal updates only this tool's encrypted grant and access-token vault. Use
`--switch` or `--switch-next` to install a refreshed Claude access token globally;
the worker never performs that switch itself. The shared CLI receives no owned
refresh token. `--refresh` cannot replace enrolled grants with shared CLI copies.
Reports remain on demand; the worker does not poll quotas. Muse expiry tracking
performs no network requests or key exchanges.

Successful `--auth-enroll` (or manual `--background start`) keeps the derived vault key in
worker memory until stopped. Your normal five-minute command unlock cache still
expires on schedule. `--lock` stops the worker before clearing that cache.
No service, cron job, or startup entry is installed: after reboot or worker exit,
run `--background start` again. Status and stop require no passphrase.
Enrollment automatically pauses an existing worker and restarts it afterward.
It reuses the enrollment unlock even if sign-in takes longer than the five-minute
cache TTL. Failed or cancelled sign-in restores a previously running worker;
first-time failure starts none. Cancel sign-in with Ctrl-C. If startup fails after
a successful sign-in, the CLI says the login was saved and exits with status 1;
retry `--background start`, without enrolling again.

Enrollment requires the relevant **native provider CLI** on PATH and **bubblewrap** (Ubuntu:
`sudo apt install bubblewrap`). Native authentication runs with a fresh home and
provider configuration inside a filesystem/PID sandbox. Existing home directories,
CLI credential stores, Design auth, keyrings, and shell configuration are not
mounted. Only that subprocess receives isolated HOME/CODEX_HOME values; your
shell and global environment stay unchanged. Claude 2.1.296 and Muse 1.4.6-R6000.1
are pinned by executable SHA-256 to the builds tested offline; an upgrade fails
closed until its authentication behavior is verified. Muse updater wrappers are
not executed. If isolation cannot be created,
the operation fails; there is no unsandboxed fallback.

Renewable grants are encrypted in `.usage-data/renewal.json` with a separate
AES-GCM context and the vault key. First-party auth files exist briefly in a
0700 temporary directory as 0600 files and are removed on normal/error cleanup.
A forced kill or power failure can leave that private temporary directory; no
claim of secure disk erasure is made. The worker disables core dumps and process
memory inspection. As with the existing vault, root or a compromised user account
is outside the protection boundary.

Refreshes are serialized. An encrypted in-flight marker is saved before calling
the provider, and rotated credentials are persisted before publishing access
tokens. Any failed or interrupted refresh **suspends** that account; an uncertain
rotation is never automatically replayed. To recover:

```sh
./claude-usage --background stop
./claude-usage --auth-forget 'you@example.com' --plan codex
./claude-usage --auth-enroll 'you@example.com' --plan codex
```

Forget deletes only this tool's renewable grant, retaining its last access token;
it never logs out or revokes a provider session. `--remove` also deletes the grant.
Capture refuses to overwrite an enrolled account until it is forgotten. The
worker stops automatically if no renewable accounts remain ready. `--background
status --json` provides account state, expiry, and next renewal time without
exposing tokens. Status is the last observation; a stopped worker does not renew.

## Capture by account identity

While the desired account is signed in through your normal workflow:

```sh
./claude-usage --capture claude --plan team_premium
./claude-usage --capture claude --plan max_20x
./claude-usage --capture codex
```

Run the appropriate command for the current account, then switch normally before
capturing a different one. The capture detects its email/name, prints which
account was saved, and reuses its existing row or an empty matching template
slot. You do not need to remember `team-1`, `max-2`, etc. Repeated captures of the
same identified account and subscription type update that row. Distinct Codex workspace routing
IDs are never merged just because they share an email.

**Personal Max and a Team seat can share the same email.** Capture each while
that subscription is the current Claude login, specifying its plan:

```sh
./claude-usage --capture 'you@example.com' --plan max_20x
# Switch to your Team subscription using your normal login workflow, then:
./claude-usage --capture 'you@example.com' --plan team_premium
```

These create separate rows and keep separate copied access tokens. Recapture with
the same `--plan` to update only that subscription. An email shared by both rows
requires `--plan` or a unique saved ID. The same token cannot be captured as two
subscriptions. If an earlier capture overwrote one, recapture each subscription;
the tool cannot recover a token that was previously replaced.

You can also select a saved account by email/name, or supply one for a new account:

```sh
./claude-usage --capture 'you@example.com'
./claude-usage --capture 'you@example.com' --plan team_premium
./claude-usage --capture 'Work account' --plan codex
```

For a new email/name, specify `--plan`. If local identity is available, its email
takes precedence over a supplied display name; a supplied email that disagrees
is refused. If identity is missing, your supplied name/email is used as the
label, or use `--name 'Work account'` with a provider selector. Plan labels for
Claude remain user-supplied. Use `--plan` to distinguish the same email across
providers or subscription types; ambiguity within the same plan requires a
unique saved ID. Capturing by an existing ID cannot change its subscription type.

## Switch the shared Claude Code subscription

```sh
# Automatically choose and switch to the best usable Claude subscription:
./claude-usage --switch-next
# Preview, or restrict the choice to Team seats:
./claude-usage --switch-next --dry-run
./claude-usage --switch-next --plan team_premium
# Choose a specific subscription yourself:
./claude-usage --switch 'you@example.com' --plan max_20x
./claude-usage --switch 'you@example.com' --plan team_premium
# Unique saved IDs also work:
./claude-usage --switch team-1
# Validate the target without changing Claude files:
./claude-usage --switch team-1 --dry-run
# Restore the preceding local Claude Code authentication:
./claude-usage --undo-switch
```

`--switch-next` uses exactly the default table ranking: the smaller remaining
percentage across reported main windows, divided by `1 + hours to the soonest
reset`. It checks each eligible Claude account once, using the existing bounded
foreground concurrency. Codex and Muse are not queried by this command. Expired,
nearly expired, missing-scope, exhausted, failed, and unknown-usage accounts cannot
win; snapshots cannot win either. Missing reset times rank below known future
resets. If nothing qualifies, authentication stays untouched. If the winning
token is already selected, nothing changes. The final profile check still has
to pass; it does not silently switch to a different identity after a failure.
`--sort` overrides and `--reverse` are rejected for this action.

Switching is **Claude-only** and runs only when explicitly requested. It first
checks the saved token's expiry and makes one `GET` to
`https://api.anthropic.com/api/oauth/profile` to validate the account and Max/Team
organization type. This is the installed CLI's first-party, undocumented profile
endpoint, with its own exact GET-only allowlist. It is not a public switching API.
Wrong emails, mismatched Max/Team types, ambiguous selections, and expired or
nearly expired tokens are rejected before changing authentication.

The only changed JSON values are:

- `claudeAiOauth` in the existing `.credentials.json`;
- `oauthAccount` in the existing `.claude.json`.

All surrounding file bytes are preserved, including `designOauth`, `mcpOAuth`,
settings, project data, and caches. Existing file modes and group ownership are
preserved. No logout/login command, CLI process, inference, token refresh,
environment-variable change, or symlink is used. The tool honors existing
`CLAUDE_CONFIG_DIR` and `CLAUDE_SECURESTORAGE_CONFIG_DIR` paths without altering
them; the defaults are `~/.claude/.credentials.json` and `~/.claude.json`.
Conflicting environment auth overrides, Console/Gateway credentials, symlinks,
unsafe file permissions, and unsupported legacy layouts are refused.

The installed Claude Code **2.1.296** implementation was inspected to identify
these fields, credential-change detection, and its shared write locks. A switch
uses the same short-lived mkdir locks, never steals an existing lock, checks for
concurrent edits, and atomically replaces each file. The two files cannot be
replaced as one filesystem transaction: a failed second write triggers rollback
of only the auth fields. Locks and temporary files are removed on normal completion
or handled errors. Hard termination can leave a lock or partial switch; an
encrypted undo record is written before changing either auth file.

**Captured logins are access-token-only.** The target receives no refresh token;
the previous account's refresh token is never mixed with a different access token.
The selected login lasts only until its captured token expires or is revoked.
Recapture after your usual sign-in when necessary. New captures retain the
source's scope list; old captures without scope metadata use the minimal local
`user:inference` / `user:profile` labels, without changing the server-side grant.
No captured Claude token is renewed by this utility. Existing sessions may detect the shared
credential change, but immediate adoption by every running session is not
guaranteed; a session may need reopening. Design credentials are preserved, not
re-authenticated or revoked.

The last pre-switch Claude auth fields are saved in the separate encrypted
`.usage-data/switch-backup.json`, using the vault key. This recovery record includes
the outgoing Claude refresh token when present, solely for restoration. It never
contains Design or MCP credentials. `--undo-switch` makes no network request and
restores only those two fields; it refuses if a subsequent login has changed the
selected access token. Restored credentials may be stale if another session has
renewed or revoked them. A successful undo deletes the recovery record; another
switch replaces it. Normal usage reports never read this record or modify global
auth. The switch does not revoke any token server-side.

## Selected accounts and readiness

The combined table highlights **▶ SELECTED** in cyan and includes a small key.
**○ Standby** marks other captured logins when the selected login is identified;
**? Unknown** means selection cannot be established safely. Detection compares
the saved access token with read-only CLI credentials before and after fetching
usage. Codex also requires the workspace routing ID to match. Same-email Max
and Team rows cannot both match. Muse's current-CLI row follows that login;
portal sessions are not assumed to be the selected CLI account.

This identifies the CLI's current credential files, **not the account cached by
every already-running process**. Unknown credentials, environment overrides,
rotation, or a login change during the check produce an unknown marker. No
extra network calls or provider-auth writes are needed for these badges.

The **Switch** column shows Claude readiness and the saved access token's
remaining lifetime. `✓ Ready` means live reported headroom and a token with more
than a minute left; the switch still validates the profile immediately before
writing. Expired tokens, unknown expiry, missing scope, unavailable usage, and
exhausted allowances are distinct states. Codex and Muse show `—` here because
this tool switches only Claude.

## Refresh saved copies, on demand

```sh
./claude-usage --refresh
./claude-usage --refresh --verbose
```

This is **credential synchronization and a readiness check, not OAuth token
renewal**. Run it while your captured Claude tokens are still valid to establish
server-verified account UUID + organization UUID bindings in the encrypted vault.
It uses the same allowlisted profile GET as switching, then fetches usage once
for each configured account. Each unbound eligible Claude token gets at most
one profile check; no token-renewal endpoint is allowed.

When Claude Code has already renewed its current access token, `--refresh` can
copy that newer token into the matching saved account. A changed token requires
one profile GET, an exact saved account/organization/plan match, a later expiry,
and a stable source-file reread. It never guesses from email, copies a refresh
token, or changes provider files. Only this tool's encrypted vault is updated.
After a binding exists, `--switch-next` performs the same synchronization before
ranking. Its `--dry-run` skips synchronization writes. Ordinary reports remain
read-only and never rewrite saved credentials.

**Inactive expired subscriptions cannot be kept ready indefinitely with the
credentials currently captured.** They contain no independently owned renewable
grant. This tool never runs a warm-up prompt or rotates a copied refresh token.
If no newer matching credential exists in the current Claude CLI file, recapture
after your normal sign-in. An expired account without an earlier verified binding
also requires explicit recapture. Codex still uses explicit recapture; Muse's
current-CLI source reads the current login on every invocation. No scheduler,
daemon, login process, or automatic polling is installed.

The normal `--refresh` output remains the table and key. `--verbose` explains
synchronization outcomes; `--json` includes `credential_sync` and the explicit
`oauth_renewal: false` flag. Research into other tools and the reasons for this
boundary are in [the refresh assessment](RESEARCH.md#refresh-and-switcher-assessment-2026-10-13).

Claude reads only `oauthAccount` display fields from its local `.claude.json`
metadata during capture and checks that the selected access token did not
change during the read. Codex extracts display fields from its existing access
token's profile claims; it does not copy ID/refresh tokens. These are **local
display hints, not independently verified identities**. They do not change
authentication or network routing, and Claude metadata can be stale. No profile
request, login, refresh, or CLI invocation is added for naming.

Old captures remain compatible. Codex names can be decoded from saved access
tokens immediately. An old Claude capture can be labeled from current local
metadata only when its saved token exactly matches the current token; otherwise
recapture that account once while it is current. Legacy IDs still work for this
one-time migration, e.g. `--capture team-1`, and stay internal storage IDs. Names
and emails appear in terminal/JSON output and the private configuration; tokens
remain encrypted. Reports do not rewrite the vault or configuration.

## Muse Code — current CLI login, no setup

To include the current Muse CLI login in the combined report, first run
`./claude-usage --add muse-current --plan muse_code --name Muse`. Then add
`"source": "cli_current"` to that row in `.usage-data/accounts.json`, preserving
its private permissions. The configuration contains labels, never credentials.
Run:

```sh
./claude-usage
./claude-usage --json
```

The configured row uses your current Muse CLI login and shows the email/name returned
by Meta; switching your Muse login changes the account
this row reports. No Muse cookie or capture is needed. Saved Claude/Codex
credentials require their vault passphrase once per five-minute unlock period.

To check only Muse:

```sh
./claude-usage --muse-cli-current
./claude-usage --muse-cli-current --json
```

This explicitly selected action checks **only your current Muse CLI account**.
It works without browser cookies, a vault passphrase, or account configuration.
It reads `$XDG_CONFIG_HOME/muse/auth.json` (normally `~/.config/muse/auth.json`)
with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_NOATIME`. It neither invokes the
Muse launcher nor writes to any authentication files. Switching your Muse
login through your own workflow changes which account this action reports.

**This route is a bootstrap POST, not a documented read-only usage API.** The
installed CLI obtains subscription quota from `POST https://api.meta.ai/muse-code/key`.
The checker sends exactly one request with `{"onboard":false}`, using the
existing device access token. It requires an already provisioned API key and
checks that Meta returned that same key, then discards the returned credentials.
No login, refresh, onboarding, inference, redirects, or retries are attempted.
The current live verification returned the same key and left source contents
and timestamps unchanged. This is evidence of observed key reuse, **not a
guarantee about future server-side key behavior**; detecting a changed key
cannot undo a server-side change. Keep using the default GET-only portal route
if a strictly read-only endpoint contract is required. The bootstrap route is
selected only by `--muse-cli-current` or a Muse account's explicit
`"source": "cli_current"` setting; it is never a fallback. Only one configured
row may use the current login. Removing `source` restores portal capture for
that row. A configured CLI source takes precedence over saved portal credentials
and snapshots, including on failure.

Only quota, plan, and sanitized email/name fields enter the report; keys, billing
details and action URLs from the bootstrap response are discarded. The current CLI
login is the account binding; session token counts are not subscription quota.
If Meta omits `subs_usage`, the tool reports `usage_unavailable`, not 0%. It
never runs a model turn to make usage appear. It does not save credentials or
turn this current-login check into a persistent account capture.
Reported Muse percentages above 100% are preserved as overage, not rejected
or clamped. Negative and non-finite values remain invalid.

## Codex setup

While the desired ChatGPT subscription is your current Codex account:

```sh
./claude-usage --capture codex
./claude-usage
```

Capture reads `$CODEX_HOME/auth.json` if that variable already exists, otherwise
`~/.codex/auth.json`. It saves only `tokens.access_token`, its expiry when present,
and `tokens.account_id`, plus display identity when available, in the encrypted
vault. It never runs Codex, reads Codex's keyring, copies refresh/ID tokens, or renews authentication. API-key-only and
keyring-only logins are unsupported. `--credential-file /path/to/auth.json` can
select an existing private file without changing Codex configuration.

The report requests `GET https://chatgpt.com/backend-api/wham/usage` with the saved
access token and account-routing ID. It shows server-reported plan, 5h/weekly
usage and resets, additional model/code-review allowances, and credit balances
when returned. Windows of other lengths are displayed separately, never
mislabeled as five-hour or weekly quotas. API token counts are not quota usage.

Capture additional accounts by switching normally, then repeating:

```sh
./claude-usage --capture codex
```

On a fresh installation, run `--setup` first; named capture adds a row as needed.
Saved access tokens remain usable only until expiry or revocation. Capturing a
token does not create an independent server-side login; no refresh-token sharing
or rotation is attempted.

## Muse Code setup — real portal subscription quota

The checker reads the same subscription-quota GET used by Meta's `dev.meta.ai`
frontend. It uses a **browser portal session**, separate from your Muse CLI
authentication. This portal route never opens `~/.config/muse/auth.json`, invokes Muse, mints an
API key, or refreshes any login.

1. Sign into **https://dev.meta.ai/usage** in your browser with the desired account.
2. Open Developer Tools → **Application** (Chrome) or **Storage** (Firefox) →
   **Cookies** → `https://dev.meta.ai`. Copy the **value only** of `llama_dev_sess`.
   Do not copy the whole Cookie header or a cURL command. Do not send it in chat.
3. Run:

```sh
./claude-usage --capture muse
```

This creates or updates a named portal account. To reuse the existing `muse-1`
row instead of adding a portal row alongside the CLI row, remove its
`source: cli_current` setting first. Paste the cookie into the **hidden terminal prompt**. The checker reads your
portal identity and available teams, asks you to explicitly select a team ID
(even if only one is listed), and checks that team's subscription quota. It then
encrypts the session cookie and account/team binding in the existing private
vault. This setup performs three GETs; no credential exchange is performed.

Run the usual report:

```sh
./claude-usage
./claude-usage --json
```

Each later Muse check verifies the saved portal identity and reads the selected
team's quota once. The browser cookie is reusable until it expires or is revoked;
the browser does not need to remain open. Re-run capture if it expires. No cookie
jar, `Set-Cookie` updates, automatic sign-in or renewal are used.

Additional subscriptions:

```sh
./claude-usage --capture 'second@example.com' --plan muse_code
```

Use that account's browser session and choose its team. An optional non-secret
`--muse-team TEAM_ID` supplies the explicit team choice during capture; it must
still be present in the returned team list. Credentials are never arguments.

The result is **live subscription quota for your selected Meta portal team**,
not PAYG API spending. Five-hour and weekly percentages come from reported
weighted usage divided by the corresponding limits; resets use local time.
Other window durations are shown separately. Missing, zero-denominator, or
stale nonzero usage remains unknown, never fabricated zero. Reported overage is
not clamped away. The portal does not prove its link to a particular Muse CLI
login: the team name and this limitation appear in the terminal and JSON
(`quota_scope: selected_meta_portal_team`, `cli_account_verified: false`).

This is an **undocumented web interface**, not a promised public Meta API.
Authentication failures, schema changes and redirects produce safe errors;
there is no fallback to `/muse-code/key`, inference or a different team.
Fresh live access has not yet been tested with your account because no usable
portal session was available during development.

Historical MSP exports remain optional. If you already have a `usage/read`
result with `usage.observedAtMs`, `tier`, `window`, and/or `weekly`:

```sh
./claude-usage --import-muse muse-1 < existing-usage-result.json
```

Imported numbers show **SNAPSHOT** with their original observation timestamp;
they never count as live. A saved live credential always takes precedence and
its errors never silently fall back to the historical export.

## Your shared Claude configuration

If your Claude sessions share one credential file, that file contains the
current login, not seven independent account credentials. The checker therefore
captures the **access token only** for each account into its own encrypted vault.
Capture never saves or uses a refresh token, and never changes the source
credentials. Only an explicit switch stores the outgoing auth in its encrypted
undo record, as described above; it never sends a refresh request.

As you switch accounts using your normal workflow, capture each account **while
that account is current**, using the appropriate plan:

```sh
./claude-usage --capture claude --plan max_20x
./claude-usage --capture claude --plan team_premium
```

The first capture asks you to create a local vault passphrase (12+ characters).
Later captures and reports reuse the unlock for **five minutes** after a
successful password entry. Running another command does not extend that period.
After it expires, the next command needing the vault asks again. This unlocks
local storage; it does not sign in to Claude. Never send the passphrase or tokens
in chat, command arguments, or configuration. Duplicate captured tokens are refused.
Account labels are your responsibility: the checker does not verify identity by
making additional profile calls.

To forget the cached unlock immediately:

```sh
./claude-usage --lock
```

The cache uses Ubuntu's Linux user keyring (`libkeyutils.so.1`), with a
kernel-enforced 300-second expiry and owner-only permissions. Only the derived
vault encryption key is cached, never your password. Nothing is written to disk
for the cache, and no daemon or cleanup process runs. It is shared across your
commands/terminals under the same Linux user. Other processes running as you
can use the unlock during those five minutes. Each vault has its own cache entry;
`--lock` first stops any explicitly started renewal worker for this vault, then
clears only this vault's cache entry, never another application's keys. If the OS keyring is unavailable, the tool asks again next time and never
falls back to plaintext storage. `--verbose` includes the cache-unavailable notice.

The default source is `$CLAUDE_CONFIG_DIR/.credentials.json` if that environment
variable is already set; otherwise it is `~/.claude/.credentials.json`. A custom
source can be selected without changing any environment or shell configuration:

```sh
./claude-usage --capture 'you@example.com' --plan max_20x --credential-file /your/config/.credentials.json
```

**Captured tokens are reusable only while valid.** They may expire or be
invalidated by your subsequent account switches. Access-only capture cannot keep those logins alive. Use independent
enrollment (described above) for renewable credentials. On an expired-token error, capture that account
again after your next normal login to it. It never initiates that login or tries
to refresh a saved token. If switching accounts immediately invalidates previous
tokens, this method cannot provide simultaneous live readings for those accounts.

## Check usage

```sh
./claude-usage
./claude-usage --sort 7d --reverse
./claude-usage --verbose
./claude-usage --json
```

To set up a fresh copy on Ubuntu with Python 3.12+:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./claude-usage --setup
```

Identity labels take precedence over configured fallback names. Keep the stable IDs;
plans are `max_20x`, `team_premium`, `codex`, and `muse_code`. Names are configured
labels; Claude plans remain unverified, while Codex can return its plan. Team
seats each require their own captured access token;
a Team organization ID or owner login is not a substitute.

The default output is just the table, split into **OpenAI**, **Anthropic**, and
**Meta** sections. Empty provider sections are omitted. Accounts are sorted by
recommended next use within each section. Each row shows five-hour and weekly
percentages, local reset timestamps with countdowns, and account status.
Use `--verbose` (or `-v`) for notices, error explanations, observation times,
model-specific windows, and extra usage. JSON always includes the full details.
Vault unlock prompts and fatal command errors still appear when necessary.
Absent metrics remain unknown, never zero. Extra-usage monetary values
remain raw units because their currency/scale is not a verified API contract.

Provider sections use rounded borders, `↻` reset labels, and status glyphs:
`● LIVE`, `✕ ERROR`, `◷ SNAPSHOT`, and `○ NOT_CONFIGURED`. On a colour terminal,
usage below 50% is green, 50–79% amber, and 80% or higher red; unknown values are
muted. These thresholds are visual cues, not provider limit rules. Labels and
numbers remain readable without colour. Colour defaults to automatic and respects
`NO_COLOR`, `TERM=dumb`, and redirected output; override with `--color always` or
`--color never`. JSON never includes colour escapes. Glyphs need a Unicode-capable
terminal; actual font ligatures are controlled by your terminal's font settings.

The default `--sort recommended` balances spare capacity and reset urgency:

- Live accounts with headroom and future reset times come first, scored as
  `remaining percentage / (1 + hours until the earliest reset)`, highest first.
  Remaining percentage is the smaller of the reported five-hour and weekly
  headroom. The one-hour offset prevents a nearly empty account from winning
  solely because it resets in seconds.
- A missing window is ignored, so a subscription with only a weekly limit can
  still rank normally. Live headroom with missing or elapsed reset times comes
  next, ordered by remaining capacity. Elapsed resets never imply a fresh allowance.
- Accounts at or above 100% in either window come after usable accounts. They
  sort by when all exhausted windows are due to reset, where known.
- Errors, unconfigured accounts, historical snapshots, and accounts with no
  reported main-window usage come last.

This is a practical heuristic: percentages are relative to each subscription,
not comparable token counts. Model-specific allowances remain in `--verbose`;
without knowing which model you will use, they do not affect this ordering.

Override with `--sort name`, `plan`, `5h`, `7d`, `5h-reset`, `7d-reset`, or `status`.
These sort ascending; add `--reverse` for descending. Usage sorts numerically,
resets chronologically, and missing values stay last. With `recommended`, reverse
changes the order within each availability tier, keeping usable accounts first.
Sorting also orders the JSON accounts array within provider groups. Each command
still fetches usage once and exits; sorting adds no network requests.

Independent account checks run concurrently using up to four short-lived Linux
worker processes. This overlaps network waits without adding requests. Claude
checks share a private 60-second cache and per-account request locks; a cache hit
never crosses a reset boundary or credential change. Use `--jobs 1` for sequential checks, or `--jobs 2` through
`--jobs 8` to choose the concurrency limit. Duplicate credentials stay in the
same worker so they cannot trigger duplicate usage requests. Unconfigured rows
and snapshots are handled locally without workers.

Workers run only while the command is active. Every worker is reaped before
exit, including after timeout, Ctrl-C, termination, or a partial startup failure.
Linux parent-death signals stop workers if the parent is forcibly killed. The
existing eight-second per-request deadline still applies; an outer 18-second
worker deadline also bounds the two-request Muse portal check. No login, token
refresh, daemon, or scheduled activity is added. A simulated eight-account
benchmark with 200 ms responses took 1.60 s sequentially and 0.43 s with four
workers; real speedup depends on server latency.

Remove an entry by its saved email, name, or stable ID:

```sh
./claude-usage --remove 'you@example.com'
./claude-usage --remove 'you@example.com' --plan codex
./claude-usage --remove muse-1
```

Removal deletes the entry from this tool's configuration, its copied credential
from the encrypted vault, and any imported snapshot. It never contacts a provider
or reads or changes the provider's login files. A vault unlock may be required.
Ambiguous names/emails are rejected; use `--plan` or the unique `id` from `--json`.
For a current Muse CLI row, use its configured name or ID (`muse-1`), since its
live email is not saved. You can remove all entries and add/capture new ones later.

JSON includes each account's `status`, `live`, nullable `usage`, `captured_at`, and
safe errors. Top-level `live` is true only when all accounts returned live data;
`live_accounts` counts successful live accounts. Errors never silently fall back
to a saved snapshot or an old cached reading. Cache hits use `status: "recent"`,
`live: false`, and the original `captured_at`; they remain switching candidates
only while less than 60 seconds old. HTTP 429 errors include `retry_after_seconds`.
JSON stdout contains no prompts; the passphrase prompt uses
the terminal. A valid cached unlock also works without a terminal. Once it
expires, a terminal is required again; there is no echoed password-input fallback.

Exit codes: `0` successful action/report; `1` any account retrieval error;
`2` configuration/vault/input error; `3` uncaptured/unsupported accounts; `130` interruption.
Manual historical snapshot support from the initial version remains available
through `--import ACCOUNT_ID`; snapshots are labeled `SNAPSHOT` and never count
as live. Scripts should inspect `live`, not just exit status.

## Security and verification

- Claude, Codex, and Muse portal sources allow only `GET` to these exact URLs/paths:
  `https://api.anthropic.com/api/oauth/usage` and
  `https://chatgpt.com/backend-api/wham/usage`, plus `https://dev.meta.ai` paths
  `/api/auth/me`, `/api/portal/teams` (setup only), and
  `/api/portal/teams/{numeric_team_id}/subscription-quota`.
  No query strings, redirects, alternate hosts, other profile calls, proxies,
  inference paths, token exchanges, or retries. TLS verification is enabled.
  The tool identifies itself as `private-claude-usage/1.0`, not Claude Code.
- The `--muse-cli-current` action, or a Muse row explicitly configured with
  `source: cli_current`, or an independently enrolled Muse row, allows only one
  `POST https://api.meta.ai/muse-code/key`, with the fixed onboarding-disabled
  body. It does not enable POSTs in the Claude/Codex/portal clients or permit inference.
- Each request has an eight-second deadline, including DNS and body reads.
  Connections close on success/error or when their owned worker exits. Concurrent
  foreground workers are bounded and reaped before report exit. Persistent
  authentication work starts after successful `--auth-enroll` or explicit `--background start`, as described
  above. No services or startup schedules are installed.
- Claude/Codex `--capture` and Muse CLI sources read credentials using Linux `O_RDONLY`,
  `O_NOFOLLOW`, `O_NONBLOCK`, and `O_NOATIME`. It requires an owned private regular
  file and never writes, chmods, locks, renames, or refreshes that source.
  Claude display metadata is read with the same read-only/no-atime flags; that
  non-secret metadata need not be private. Legacy unnamed Claude rows can read
  current metadata during reports only to label an exactly matching saved token.
- Access tokens, expiry metadata, display identities, Codex account-routing IDs, Muse device access tokens/API keys, and Muse portal
  cookies with identity/team bindings enter the encrypted vault. AES-256-GCM
  protects the vault; scrypt derives its key from your passphrase. The passphrase
  is never stored. The command unlock key is cached in the OS keyring for five
  minutes, not in a disk file. An explicitly started renewal worker separately
  holds the key in memory until stopped. A lost passphrase means recapturing tokens once
  that unlock expires.
- `.usage-data` is `0700`; configuration, vault, and switch undo record are `0600`.
  Normal reports/captures do not modify existing provider stores or keyrings.
  Only explicit `--switch`/`--undo-switch` edits the two Claude Code auth fields
  described above, preserving unrelated credentials. Reports use this tool's vault;
  configured Muse CLI sources read current Muse credentials, and legacy unnamed
  Claude captures can read current local metadata for an exact token match.
  Nothing remains connected after the report exits.
- Tokens and raw server error bodies are never logged or included in JSON.
  Runtime memory necessarily contains decrypted tokens while a check runs.
  File encryption does not protect against a compromised process or root.

Run the fake-credential test suite:

```sh
.venv/bin/python -B -m unittest discover -s tests -v
```

265 tests pass. They cover automatic switching, active-account badges, safe access-token
synchronization, recommendation ordering, same-email Max/Team captures, colour and Unicode
alignment, grouped/sorted compact and verbose reports, isolated
entry removal, seven accounts captured from one changing source,
correct plan labels, encrypted storage, source content/mtime/atime preservation,
parsing, missing data, local countdowns/DST, 401/403/429, redirects, TLS/network
errors, hard deadlines, secret redaction, endpoint/method restrictions, and absence
of polling in ordinary reports. Renewal tests additionally cover independent
enrollment, seven distinct grants, isolated filesystem visibility, the auth RPC
allowlist, single-owner locking, rotation persistence ordering, crash suspension,
removal, and actual background worker start/status/stop with fake grants. Serial unit tests prohibit child processes; concurrent integration
tests verify bounded overlap, one cycle per account, duplicate suppression,
worker reaping, timeout/crash handling, Ctrl-C/termination/hangup cleanup, and
partial worker startup failures. Provider tests cover Codex routing and isolation,
mixed reports, credit/model quota parsing, expiry, reflected-secret suppression,
read-only capture, and historical Muse imports. Muse tests verify portal-only
cookie routing, exact GET paths, explicit team selection, identity binding,
one quota read per report, encrypted cookies, counter accuracy, stale/idle
windows, and no key bootstrap in the portal route. Separate CLI-route tests cover
its explicit selection, fixed bootstrap method/path/body, returned-key comparison,
omitted usage, redaction, source preservation, and absence of background work,
refresh or inference. Combined-report tests cover all nine account rows,
configured labels, one Muse request, CLI source validation, and error isolation
without portal/snapshot fallback. Identity tests cover named captures,
recaptures, legacy migration, wrong-account rejection, workspace separation,
metadata read races, source preservation, and redaction. Unlock-cache tests cover fixed expiry, reuse
across captures/reports, wrong passwords, vault isolation, owner permissions,
timeout-before-publish ordering, `--lock`, and safe fallback when keyrings are
unavailable. A separate fake-key check on this Ubuntu machine also verified
reuse across processes and kernel expiry. No tests use real
credentials or inference. Passing mocked tests does not establish live endpoint
access for your accounts.

Switch tests use fake files and tokens to verify exact preservation of unrelated
JSON text, Design/MCP auth, permissions, environment, scope metadata, profile
allowlisting, token/email/plan checks, dry runs, no-op switching, encrypted undo,
lock contention, concurrent edits, backup failure, and rollback after a failed
second write. A read-only live profile GET was also verified successfully with
the current CLI token; no real authentication files were switched during testing.
