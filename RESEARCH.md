# Usage endpoint and authentication findings

These are chronological development notes, including superseded approaches.
The [README](README.md) and [usage guide](USAGE.md) describe current behavior.
Live-account identities, credentials, and quota measurements are omitted.

Researched 2026-10-12. The first implementation disabled all live checks because
it did not establish official support. Following the explicit request for real
usage, the implementation now exposes a narrow, clearly disclosed undocumented
GET, using saved access tokens without any renewal machinery. Lack of official
support is not presented as proof that every personal read-only tool is banned.

## Reference project

Inspected [DSado88/claude-tracker](https://github.com/DSado88/claude-tracker) at
commit `5be378aaf2e3eda1153288260090205632f59f4f`:

- [`src/oauth.rs`](https://github.com/DSado88/claude-tracker/blob/5be378aaf2e3eda1153288260090205632f59f4f/src/oauth.rs)
  requests `/api/oauth/usage` using a bearer access token and the
  `anthropic-beta: oauth-2025-04-20` header. It also implements profile and refresh
  requests and reads Claude Code keychain credentials.
- [`src/api.rs`](https://github.com/DSado88/claude-tracker/blob/5be378aaf2e3eda1153288260090205632f59f4f/src/api.rs)
  includes a browser-cookie alternative at
  `https://claude.ai/api/organizations/{org_id}/usage`, an OAuth login flow using
  Anthropic's client ID, and automatic refresh after retrieval errors.
- The reference identifies some requests as Claude Code. This tool does not copy
  that identity, OAuth login flow, cookie path, keyring integration, or refresh.

## Available interfaces

| Interface | Relevance |
|---|---|
| Native Settings → Usage | Official UI for individual subscription meters |
| `GET api.anthropic.com/api/oauth/usage` | Undocumented endpoint used by the reference; the only Claude endpoint this tool allows |
| Browser `/api/organizations/{id}/usage` | Undocumented cookie-authenticated endpoint; not implemented |
| Official Claude Code Analytics API | Daily activity/tokens/cost; not live subscription quota percentages |
| Official status-line `rate_limits` | Session-provided values, not an independent on-demand query |

The [official usage guidance](https://support.claude.com/en/articles/9797557-usage-limit-best-practices)
points subscribers to Settings → Usage. The
[Team plan documentation](https://support.claude.com/en/articles/9266767-what-is-the-team-plan)
states that limits apply per member. No documented method was found for a Team
owner to query every seat's live five-hour and weekly allowance. Each account
therefore needs its own token. Plan and name labels are user-configured; an
organization ID does not grant access to other seats' meters.

The [official analytics API](https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api)
returns daily aggregates with delayed availability, not live remaining quotas.
API spending cannot be substituted for seat allowance utilization. The
[official status-line interface](https://code.claude.com/docs/en/statusline#rate-limit-usage)
provides optional rate-limit data after a session API response. This tool installs
no hook and never creates a model response to obtain those fields.

## Token isolation with a shared Claude configuration

All Claude sessions share one configuration directory, and the user switches
accounts through the existing Claude login workflow. The file contains the most
recent credentials; seven references to it would not represent seven accounts.

Explicit `--capture ACCOUNT_ID` reads that file without modifying even its access
time, extracts only the access token and known expiry, and encrypts those values
into a separately owned vault. The raw file may contain refresh tokens, but they
are not extracted, saved, sent, or used. Claude reports use only the vault.
Capture itself performs no network operation. The utility never invokes Claude,
authenticates a new session, changes the active account, or renews a token.

Copying a refresh token would not create a separate server-side token lineage.
[RFC 9700 §4.14](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14)
describes rotation/reuse risks; the
[concurrent-refresh issue](https://github.com/anthropics/claude-code/issues/24317)
reports session disruption but is not an authoritative description of Anthropic's
revocation scope. No renewal flow is enabled. Captured access tokens may expire
or be invalidated by later user-initiated logins. A GET cannot make an expired
access token reusable. The CLI reports this instead of refreshing it.

## Policy and compatibility limits

Anthropic's [credential-use documentation](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)
restricts third-party subscription login and credential intermediation. Its
[login guidance](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account)
prefers API keys for third-party tools and prohibits misrepresenting client
identity. Neither publishes this usage endpoint as a supported integration or
explicitly settles this personal read-only use case. The implementation's narrow
scope is not a guarantee of Anthropic permission, server behavior, or future
compatibility. It does not bypass a denial or offer a disguised login flow.

Max 20× and Team Premium responses are parsed the same way, without inventing
missing windows. Model-specific windows are retained when returned. Endpoint
access and all seven real-account combinations require authenticated verification;
a successful check for one token does not prove support for every seat.

The test suite uses fake credentials and mocked HTTPS only. A separate single
real-account smoke check requires explicit approval because it sends an existing
access token to the undocumented first-party usage endpoint. No real token is
stored by that smoke check. Account-specific observations are not included in this repository.

## Codex extension

Inspected the installed Codex 0.162.1 executable as data, without executing it,
then checked the official source and documentation:

- [Authentication](https://learn.chatgpt.com/docs/auth): cached subscription
  credentials may live in `auth.json` or an OS credential store. The CLI and
  extension can share them. This utility reads an explicitly selected file only,
  ignoring refresh tokens, ID tokens, and API keys.
- [App-server API](https://learn.chatgpt.com/docs/app-server):
  `account/rateLimits/read` is a documented RPC for quota windows. Running the
  normal app server brings authentication machinery, so this utility does not
  invoke it. Its `account/usage/read` token counts are not subscription quotas.
- [Official quota client](https://github.com/openai/codex/blob/main/codex-rs/backend-client/src/client/rate_limit_resets.rs)
  uses GET `/wham/usage` with a ChatGPT backend base URL, and separates quota
  retrieval from POST operations which consume reset credits. Only the GET is
  implemented here.
- [Official response mapping](https://github.com/openai/codex/blob/main/codex-rs/backend-client/src/client.rs)
  maps `rate_limit.primary_window` / `secondary_window`, `used_percent`,
  `limit_window_seconds`, `reset_at`, credits, and additional metered features.
  Codex supplies a ChatGPT account ID header to route subscription/workspace data.

The exact allowed URL is `https://chatgpt.com/backend-api/wham/usage`. This is an
implementation interface used by OpenAI's client, **not a documented public HTTP
API contract**. No claim of guaranteed third-party support or policy permission
is made. 401/403, redirects and rate limits stop that account's attempt; there is
no client impersonation, Cloudflare bypass, refresh, fallback host or retry.
Account tokens are provider-bound inside the encrypted vault and cannot be sent
to Anthropic by changing a configured plan. Response account IDs are checked when
present; display names remain configured. Live real-account access remains
unverified; tests use fake tokens and mocked HTTPS.

## Muse Code: initial investigation

Initially inspected the installed Muse Code 1.4.6 launcher and executable as files only.
The launcher can authenticate/update, and Meta subscription key acquisition uses
`POST https://api.meta.ai/muse-code/key`. That is a credential-minting operation,
not an allowed read-only usage GET. No guarantees about its effects on existing
keys were established at that stage, so the initial version did not implement
or test it with real credentials. The later explicit CLI verification below
supersedes that initial result.

Official sources:

- [Subscriptions](https://dev.meta.ai/docs/muse-code/subscriptions) distinguish
  Muse Code subscriptions from separately provisioned PAYG API keys; API spending
  is not a substitute for the subscription's five-hour and weekly meters.
- [Authentication](https://dev.meta.ai/docs/muse-code/auth) describes browser
  login and API keys. The initial implementation ran neither a login nor a
  credential exchange; the current explicit option performs a bootstrap only.
- [MSP usage/read](https://meta-models.github.io/muse-code-sdk/next/generated/msp/methods/usage-read/)
  returns a host's last-observed subscription reading without a model call; the
  result can omit usage if the host has not observed it.
- [SubscriptionUsage](https://meta-models.github.io/muse-code-sdk/next/generated/msp/types/subscriptionusage/)
  carries an observation timestamp, tier, short window and weekly window. Imports
  preserve that timestamp and tier rather than substituting the check time.
- [Transport limits](https://meta-models.github.io/muse-code-sdk/next/guides/msp-wire/framing-and-caps/)
  document stdio with a client-spawned host for v1; sockets/WebSockets are deferred.
  An existing first-party TUI is not a supported externally attachable MSP host.

The initial implementation left Muse Code **unsupported for fresh automatic retrieval**
under these constraints. The optional import accepted an already available
`usage/read` result as historical data only. That version opened no Muse
credential file, started no session host, and allowed no Meta network endpoint.
This is a limitation, not a claim that saved observations meet the live-check
requirement. A documented read-only endpoint with safe reusable authentication
would have been needed for that CLI-authenticated approach. The subsequent
portal implementation below supersedes the blanket unsupported conclusion.

## Muse Code: verified portal route and implementation

Further research found a separate browser-session route. Inspected the
[CodexBar provider source](https://github.com/steipete/CodexBar/blob/main/Sources/CodexBarCore/Resources/Plugins/muse.ts)
for the portal schema, then independently downloaded Meta's public frontend
assets without credentials. On 2026-10-12, Meta's own
[subscription hook bundle](https://dev.meta.ai/_next/static/chunks/139ugobemlt3s.js)
defined `useSubscriptionQuota` using GET
`/api/portal/teams/{team_id}/subscription-quota`; it also defined the team-list
GET. The [identity hook bundle](https://dev.meta.ai/_next/static/chunks/0395bxbt-8a2..js)
defined GET `/api/auth/me`. These filenames are deployment-specific evidence,
not runtime dependencies. No JavaScript scraping or discovery is needed on each
report. The frontend's `useApiHeaders` uses JSON content type; privileged
developer override headers seen in its code are **not** used by this tool.

The provider source describes `llama_dev_sess` authentication and the
`subscription_quota` object with `tier`, `window_duration_secs`,
`window_weighted_used/limit`, `window_resets_at`, `weekly_weighted_used/limit`,
and `weekly_resets_at`. This is subscription quota, not team API spending.
The checker computes ratios from integer/string counters, preserves actual
overage, withholds stale windows instead of assuming resets, and does not guess
a team or claim it has proven the portal team's association with the CLI login.

Capture prompts for only the portal cookie through hidden terminal input,
checks identity, lists teams, requires an explicit team selection, reads its
quota once, then encrypts the cookie and binding. Reports check the same portal
identity and issue exactly one quota GET. Requests use only the fixed Meta host
and three narrowly allowed GET path forms. In this portal route, no Muse CLI credentials are opened,
no Meta CLI access token is transmitted, and no key-minting/token-exchange POST
is permitted. `Set-Cookie` is ignored, and no browser profile, cookie database,
session file or OS keyring is modified.

Meta does not document this as a public third-party API. First-party frontend
use establishes the endpoint's purpose, not a contractual permission or future
availability guarantee. The program identifies itself honestly, stops at access
denials, and offers no restriction bypass. A saved portal session is reusable
only while valid; capture does not create a new independent server session.

During development, narrowly scoped in-memory, read-only checks of saved Chrome
and Firefox cookie databases found no usable saved Meta portal cookie. No values
were displayed or persisted. Thus endpoint usage is confirmed by first-party
code, and implementation tests pass with fake credentials, but **an authenticated
live result for this user's Muse subscription is still awaiting portal setup**.

## Installed Muse CLI verification

After the user explicitly authorized invoking the installed Muse CLI, tested
Muse Code **1.4.6-R6000.1** on this machine on 2026-10-12. The binary was invoked
directly, avoiding the launcher's automatic updater and background process.

The CLI has no standalone `usage` subcommand. Its exported experimental schema
includes `usage/read`; a newly started MSP host returned `{}`. Starting an
empty session did not populate a quota observation. No model turn was submitted.

For the interactive `/usage` check, credentials were read using `O_RDONLY |
O_NOFOLLOW | O_NOATIME` and copied into a **sealed in-memory file**. The test used
temporary XDG configuration/data/cache directories; the original credentials
were never supplied as a writable file. The binary reads the XDG Muse auth path;
the launcher's `MUSE_AUTH_PATH` override is not recognized by this binary.

A temporary local HTTPS inspection gate allowed the model-catalog GET and one
`POST /muse-code/key` to Meta, blocking all other routes, including inference,
login and refresh. The private test CA was trusted only by the test child.
No system trust/keyring changes were made. For the final display check, the
second identical CLI bootstrap read received the exact first successful live
response from memory, avoiding a second upstream retrieval or a gate-generated
authentication failure. This was a verification harness, not a new runtime
dependency of the checker.

Initially observed result:

- Meta returned HTTP 200 and **Muse Code High Usage**.
- The returned `api_key` matched the already-stored key in the successful checks.
- The response omitted `subs_usage`, so it supplied no five-hour/weekly
  percentages or reset times. Absence is not zero and does not establish why
  Meta withheld the fields.
- Built-in `/usage` displayed **Subscription · Muse Code High Usage — Currently
  unavailable**. Session input/output tokens and turns were zero.
- Original credential contents, modification time, and access time matched
  before/after. The CLI and inspection gate were stopped and temporary state
  removed; no test credentials were saved in the utility vault.

After the user reported visible quota in another Muse session and requested a
retry, the same protected check returned `subs_usage` successfully:

- Plan: **Muse Code High Usage**.
- Both five-hour and weekly windows included utilization and reset timestamps.
- Returned API key matched the existing key. Source bytes/mtime/atime remained
  unchanged. The new test session had zero model turns and input/output tokens;
  all child processes and the test gate exited.

A separate existing session also displayed subscription quota. The new response establishes that the bootstrap can return quota without
a model call in the checking session. It does not establish whether login,
activity elsewhere, or another server condition caused fields to become
available. A missing quota in one response must not be generalized into a claim
that CLI credentials cannot retrieve usage.

The utility now exposes this route only through the explicit
`--muse-cli-current` action. It directly sends one bootstrap request with
`{"onboard":false}` and no CLI subprocess. Its separate exact allowlist cannot
call model, login, refresh, or onboarding routes; normal reports retain their
GET-only portal route. The action requires an existing device token and API key,
compares the returned key, projects only usage/plan fields, discards credentials,
and never updates authentication. Missing usage remains an error.

The shipped command was then tested directly with `--muse-cli-current --json`:
it exited 0 with `live: true` and both subscription quota windows. The
complete mocked test suite passed **90 tests**. No real credentials are fixtures.

This establishes observed key reuse, **not a universal guarantee about future
server-side key behavior**. It is not a documented read-only API contract, and
key comparison is detection, not a way to undo any server change. The option
must be explicitly selected and is never an automatic fallback. Users requiring
a strictly read-only interface should retain the portal GET route.

Combined reports can explicitly select `source: cli_current` for a Muse row, running
that same check once alongside the saved Claude/Codex accounts. Only one row
can select the current Muse login; the report explains that switching the login
changes that row's account. Portal sources remain available, and a failed CLI
check never falls back to a portal credential or snapshot. The expanded suite
passes **94 tests**, including a nine-account combined report and error isolation.
A combined-report smoke check returned live Muse usage while uncaptured rows
remained `NOT_CONFIGURED`. Only the configured Muse source made a network request.

## Five-minute vault unlock cache

At the user's request, captures and reports now reuse a successful vault unlock
for 300 seconds. The password is never cached. The 32-byte derived AES key is
placed in an owner-only Linux `user` key under this application's namespace,
bound to the vault directory's device/inode and salt. It is first created in the
process keyring; permissions and [kernel expiry](https://man7.org/linux/man-pages/man3/keyctl_set_timeout.3.html)
are set before linking it into the user keyring for later commands. A crash
before publication therefore cannot leave a persistent, unexpiring unlock.
Reads never extend the expiry, and a boot-time deadline also rejects stale
payloads, including time spent suspended. No helper daemon, file cache, timer
process, network request, or password argument is involved.

The cache is available to the same OS user across terminals. It does not protect
against a malicious process under that same user or root. `--lock` revokes and
unlinks only the current vault's exact cache key; other applications' keyring
entries are never enumerated or changed. If keyring access fails, ordinary
password unlocking still works, with a notice that caching is unavailable.

The suite passes **107 tests**. A separate real-kernel check using only synthetic
key material verified a cache hit in a second process, immediate locking, and
expiry with a shortened test timeout. The test left no usable cached unlock and
did not open real credentials or perform network requests.

## Account names and emails

Captures now accept email/name selectors and `claude`, `codex`, or `muse`
provider selectors. Existing internal IDs remain stable. The selected current
login's identity is saved with its access token in the encrypted vault and used
as the report label; the private configuration also keeps the display label.
An exact existing token or matching email within the provider can identify a
recapture, but differing Codex routing IDs are not merged. Mismatched explicit
emails, ambiguous names, and replacement of a known different account are
refused before saving. Unknown accounts retain placeholders.

Read-only inspection on this machine confirmed Claude's `.claude.json`
`oauthAccount` has `emailAddress`/`displayName`/`fullName`, and the Codex access
JWT has `https://api.openai.com/profile.email`/`name`. These local fields are
display hints, not independently authenticated identity claims. The checker
does not verify JWT signatures for display, and Claude metadata may be stale.
It uses only the selected source's adjacent Claude metadata (or the standard
home metadata for the default source), with `O_RDONLY | O_NOFOLLOW | O_NOATIME`.
It verifies that the credential remains the same after reading the metadata.
No source files or permissions are modified, including metadata that is not
private. This adds no authentication machinery, profile endpoint, or refresh.

Old Codex captures can derive labels from their already saved access JWT. Old
Claude captures may use current local metadata only when the saved token
exactly matches the current token; other old captures need a recapture to learn
their name. Reports never save this enrichment or rewrite configuration. Muse
uses sanitized email/name fields from its already existing bootstrap or portal
identity response, without any additional network calls. Secrets and unrelated
billing fields are still discarded.

The suite passes **125 tests**, including named captures, legacy compatibility,
same-email workspace separation, source timestamps, capture races, failed-write
label preservation, and response redaction. Local read-only checks confirmed
name and email extraction for the current Claude and Codex accounts.
# Explicit Claude Code authentication switch (2026-10-13)

The user explicitly relaxed the original shared-Claude-auth isolation rule for
an on-demand switch command, while requiring preservation of Design auth and
every unrelated setting. Normal reports and captures retain the original rule.

Read-only inspection of the installed Linux Claude Code 2.1.296 binary identified
separate `claudeAiOauth`, `designOauth`, and `mcpOAuth` fields in its credential
store. Its logout/re-login path can remove or revoke Design credentials as well,
so this utility never invokes that path. The plaintext storage writer serializes
mutations with the mkdir-based `.storage-write.lock`; global configuration uses
`<config filename>.lock`. Claude reads subscription identity from `oauthAccount`
and detects credential-file mtime changes. The switch changes only these two
Claude Code fields, preserving surrounding JSON bytes and checking for concurrent
changes under those same locks.

The installed CLI retrieves subscription identity using a bearer-authenticated
`GET https://api.anthropic.com/api/oauth/profile`. The switch uses a separate
GET-only allowlist for that endpoint, validates email/UUIDs/Max-versus-Team type,
and installs the already captured access token without any refresh token. The
CLI's refresh-save comparison rejects an old refresh token when the current
stored refresh token is null; this avoids retaining the outgoing account's
refresh token alongside a different account's access token. Existing captures
without scope metadata get minimal local inference/profile labels; future
captures retain the actual source scope list. These labels cannot enlarge the
token's server-side grant.

This is an implementation-dependent local switch, not an officially documented
switching API. The supported first-party multi-account workflow remains separate
configuration directories, per the
[authentication documentation](https://code.claude.com/docs/en/authentication).
The user explicitly requested the shared-file route instead, with no environment
edits or symlinks. It cannot guarantee that every already-running session adopts
the new credential immediately or that a captured access token stays valid.

Verification: one live read-only profile GET succeeded with the current CLI
access token, with required account/organization identifiers and rate-tier fields
present. Only booleans were printed; no tokens, profile contents, or live auth
writes were used in verification. Mock/fake-file tests cover switching, rollback,
encrypted undo, exact preservation of Design/MCP/settings text, private file
handling, expiry, plan/email mismatch, scope capture, and the dedicated network
boundary. The outgoing Claude-only auth fields, including any refresh token, are
encrypted in a separate undo record and are never sent to a refresh endpoint.

## Refresh and switcher assessment (2026-10-13)

Reviewed comparable implementations and actual feedback before adding automatic
selection, CLI-selection badges, and on-demand access-token synchronization:

- **ccswitch:** its [README](https://github.com/2hmad/ccswitch) describes saving the
  current credential before switching and keeping account identity separate from
  shared configuration. Inspection of [its refresh implementation](https://github.com/2hmad/ccswitch/blob/main/bin/ccswitch)
  found that it temporarily installs each account, invokes a Haiku prompt through
  `claude -p`, then saves and restores credentials. It also offers a systemd file
  watcher. These techniques conflict with this project's no-inference,
  preservation, and no-background-work requirements; none were adopted.
- **ClaudexBar:** its [authentication design](https://github.com/ipangdz/claudexbar/blob/main/docs/AUTH.md)
  uses a separate OAuth grant and directly renews it, explaining why refreshing
  a copied Claude Code grant can invalidate the other copy. Its authorization flow
  uses Claude's own client ID and callback. The project's claim of independence
  is not proof of provider permission or a guarantee about every server-side
  revocation case, so this tool does not reproduce that login flow.
- **Reported failure:** [CodexBar issue 1998](https://github.com/steipete/CodexBar/issues/1998)
  describes recurring keychain prompts and apparent logouts. It is user feedback,
  not proof of the reporter's suggested cause. The design response here is no
  credential-helper process or repeated authentication attempts, and an explicit
  unknown state when the CLI selection cannot be verified.
- **Concurrent refresh complaints:** users in [Claude Code issue 24317](https://github.com/anthropics/claude-code/issues/24317)
  report repeated sign-ins with shared credentials and concurrent sessions. Their
  diagnosis is not an authoritative revocation contract. We do not rotate shared
  refresh tokens or assume a filesystem copy owns a separate grant.
- **Positive feedback:** a [user comment on this CodexBar status-line integration](https://gist.github.com/kanoliban/39bb37cda2678b6e0941c5ca99757d9e)
  praises the status bar and asks for easy installation. This is one anecdote,
  not a representative survey. The useful design lesson is immediate quota/reset
  visibility, compact status markers, and simple commands without added services.

Anthropic's [current credential-use policy](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)
restricts third-party Claude subscription sign-in and credential intermediation.
No documented, permitted independent renewal mechanism for this personal checker
was established. Other projects doing it is not evidence that it is permitted.
The existing explicitly disclosed usage/profile GETs remain undocumented; this
change does not claim official support for them.

Implemented `--refresh` as a bounded foreground **synchronization**, not a renewal
grant: verify still-valid saved Claude identities through the existing profile
GET, persist only account/organization UUID bindings in the encrypted vault,
and accept an already-renewed current CLI access token only after its server
identity and plan match one binding. Require a later expiry and reread the source
to reject a concurrent login change. Same-email Max and Team rows cannot be
merged. Failed checks retain the saved credential. `--switch-next` can reuse
bindings before applying the existing usage-ranking algorithm.

This helps avoid restoring stale access-token copies, but cannot keep inactive
accounts renewable indefinitely. Expired inactive accounts without a newer
matching source still need user-managed sign-in and recapture. No refresh-token
endpoint, inference call, daemon, scheduled task, or provider-credential write
was added to the refresh operation. CLI selection is reported separately from
readiness and never presented as proof of what every running process is using.

## Background renewal follow-up (2026-10-13)

The user now explicitly requests background authentication renewal, superseding
the original prohibition on background activity. The existing-session isolation
and zero-inference requirements remain. A scheduler alone cannot renew the
existing access-token-only captures; they are not independently owned renewable
grants. Do not use the switch undo record as a renewal credential source.

There is a concrete official authentication-only path for **Codex**:
[`account/read` with `refreshToken: true`](https://learn.chatgpt.com/docs/app-server#1-check-auth-state)
forces renewal of Codex-managed ChatGPT authentication. That would require an
independently enrolled, tool-owned Codex context, a bounded app-server lifetime,
and a strict RPC allowlist excluding thread/turn creation and credit consumption.
The [official maintenance guidance](https://learn.chatgpt.com/docs/auth/ci-cd-auth)
warns against concurrent users of the same credential copy. Its example uses
an inference prompt; that example is unsuitable here, whereas the documented
account RPC is a candidate for verification using fake credentials first.

**Claude** documents separate configuration directories for
[multiple independent sign-ins](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts).
The installed 2.1.296 binary was inspected as data, without executing it. Its
`auth status` command handler reports authentication state and has no explicit
renewal call; this does not prove that every startup path is free of auth side
effects. The [CLI reference](https://code.claude.com/docs/en/cli-reference)
does not document a standalone subscription-refresh command. A first-party,
zero-inference renewal path still needs verification; do not substitute prompts,
copied shared refresh tokens, or an assumed-permitted third-party OAuth client.

**Muse** [authentication documentation](https://dev.meta.ai/docs/muse-code/auth)
does not establish a standalone, isolated subscription renewal API. Its existing
quota bootstrap is not proof that refreshing a device session or subscription
key leaves every other session unaffected.

The user subsequently approved first-party authentication commands solely in
new, tool-owned contexts. Implemented independent Codex device-code enrollment
and explicit background start/status/stop, using the official RPC above. Existing
captures remain access-only; no copy of a shared refresh token can be enrolled.
At this stage Claude and Muse were unsupported. The provider-specific investigation
and implementation below supersede that initial limitation.

Verified the installed Codex **0.162.1** using an empty bubblewrap-isolated home:
`initialize`, `initialized`, and `account/read` with `refreshToken: false` returned
an unauthenticated account and created no credential file. No thread, turn, model
request, real credential, or real refresh was used in this probe. A separate
filesystem canary test proved the original context was not visible in the
sandbox. Fake-grant tests exercise renewal, seven independent identities,
rotation ordering, crash recovery/suspension, and the real detached worker's
start/stop lifecycle. Live enrollment and renewal remain unverified until the
user completes the new device sign-in; the worker is not enabled for real accounts.

The RPC allowlist contains only initialization, managed device sign-in, and
account-read operations. Native Codex owns the OAuth implementation; this project
does not reproduce an OAuth client or refresh-token endpoint. The original usage
HTTP allowlist is unchanged. Native authentication is bounded (10-second startup,
20-second auth RPC, up to ten minutes for an interactive device sign-in), all
native processes are reaped, and bubblewrap's PID namespace terminates descendants.
A failed renewal is suspended with no automatic replay because a lost response
can hide a successful server-side rotation. Independent grants are encrypted
before their access-only projections enter the ordinary vault.


## Claude and Muse independent authentication follow-up

Tested the installed native binaries inside a bubblewrap **offline network
namespace** with fake credentials, a private fake CA, and local HTTPS fixtures.
The actual home, keyrings, live credentials and internet were unavailable.
No inference request was made. Production does not install that CA or redirect
provider endpoints. Executable hashes are pinned in `isolated_auth.py`.

Claude 2.1.296: `auth status --json` unexpectedly attempted renewal during startup
but exited without saving the rotated credential. It must not be used for renewal.
An initialization-only stream-JSON invocation instead persisted the new access
and refresh tokens and expiry, then exited successfully. The observed request
trace was exactly POST `platform.claude.com/v1/oauth/token` and two GETs of
`api.anthropic.com/api/oauth/profile`. The only input was an `initialize` control
request followed by EOF. No prompt/user message or inference endpoint appeared.
The implementation uses this verified native behavior, plus a read-only profile
check to bind the independent grant to the selected account and organisation.
This is verified implementation behavior, not a documented standalone renewal API.
Official [Claude authentication](https://code.claude.com/docs/en/authentication)
and [usage terms](https://code.claude.com/docs/en/legal-and-compliance) remain
applicable: the unmodified first-party CLI owns login and OAuth exchange.

Muse 1.4.6-R6000.1: native MSP `account/loginStart` with `type: deviceCode`
completed against fake device authorization/token and key-bootstrap fixtures.
It saved a separate `providers.meta` grant, including the DCA access token,
refresh token, expiry, API key and display identity. The native bootstrap sent
`{"onboard":false}`. MSP notifications use `account/loginCompleted` with outcome
`granted`; its device result has no Codex-style login ID.
See the official [MSP login method](https://meta-models.github.io/muse-code-sdk/next/generated/msp/methods/account-loginstart/).

However, restarting Muse with an expired fake DCA grant and calling `account/read`
and `model/list` did **not** renew the device access token. A mock 401 on
`/muse-code/models` caused one `/muse-code/key` re-mint and one catalog retry;
the saved DCA expiry and refresh token stayed unchanged. There is no verified
native device-token renewal command. Re-minting may also affect an account-level
API key, so the worker deliberately never uses that path. Independent Muse login
and reporting are implemented; background support is explicitly **expiry tracking**,
with `reauth_required` at expiry and zero background network activity for Muse.
No undocumented OAuth refresh endpoint/client flow is guessed or reconstructed.

Fake-grant tests cover seven independently renewed Claude accounts, same-email
Max/Team separation, organisation mismatch suspension, rejection of shared-token
synchronization for enrolled accounts, Muse enrollment/projection, correct owned
Muse report routing, expiry tracking without network, strict RPC restrictions,
build pinning, timeout redaction, and sandbox command boundaries. Live independent
enrollment still requires the user's interactive sign-in; no real background
worker was enabled during development.


Claude native credential format correction: inspection of the installed 2.1.296
writer (`Ok`/`xvr`) confirmed optional `clientId` and `refreshTokenExpiresAt` fields.
The independent grant validator now accepts, validates and preserves both;
they remain encrypted and never enter the report or access-only capture. A fresh
offline fake-grant renewal verified native persistence of both fields and the
same token/profile-only request trace. The earlier six-field fixtures missed
this real login format and caused successful enrollment to be rejected.
