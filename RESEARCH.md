# Interface and authentication research

A findings reference for contributors. It records what was inspected, on which
builds, and what QuotaHop relies on. Current behaviour is described in the
[README](README.md) and [usage guide](USAGE.md). Findings date from October 2026
(builds are named per section) and should be re-checked against newer provider
versions. Account identities, credentials and quota measurements are never
included; tests use fake credentials and mocked HTTPS only, which does not prove
live access for any particular account.

## Interfaces and endpoints

### Claude

| Interface | Relevance |
|---|---|
| `GET api.anthropic.com/api/oauth/usage` | Undocumented first-party endpoint (bearer access token, `anthropic-beta: oauth-2025-04-20`). The only Claude usage endpoint QuotaHop allows. |
| `GET api.anthropic.com/api/oauth/profile` | Undocumented endpoint the CLI uses for subscription identity. Used for switching, `--refresh`, identity binding and following Claude Code's renewed token, under a separate GET-only allowlist. |
| Browser `claude.ai/api/organizations/{id}/usage` | Cookie-authenticated, undocumented. Not implemented. |
| Settings → Usage | The official UI for individual subscription meters. |
| [Claude Code Analytics API](https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api) | Daily aggregates with delayed availability; not live remaining quota, and API spend is not seat utilisation. |
| [Status-line `rate_limits`](https://code.claude.com/docs/en/statusline#rate-limit-usage) | Values supplied to a session after a model response; not an on-demand query. QuotaHop installs no hook and never generates a response to obtain them. |

The usage endpoint and its headers were first documented in the open-source
[claude-tracker](https://github.com/DSado88/claude-tracker) (commit
`5be378aaf2e3eda1153288260090205632f59f4f`, `src/oauth.rs`). QuotaHop does not
copy that project's client identity, OAuth login flow, cookie path, or
refresh. Max 20× and Team Premium responses parse the same way; windows are never
invented and model-specific windows are retained when returned.

[Team plan limits apply per member](https://support.claude.com/en/articles/9266767-what-is-the-team-plan),
and no documented method lets an owner query every seat's live five-hour and
weekly allowance, so each seat needs its own credentials. Plan labels are
user-configured; an organisation ID grants no access to other seats' meters.

**Local identity hints.** Claude's `.claude.json` `oauthAccount` carries
`emailAddress`, `displayName` and `fullName`; a Codex access token's JWT carries
`https://api.openai.com/profile` email and name. These are display hints, not
authenticated identity: JWT signatures are not verified for display and Claude
metadata can be stale. Server-verified identity comes only from the profile
endpoint (account and organisation UUIDs, Max versus Team).

**Token isolation.** With a shared Claude configuration, the credentials file
holds only the current login, so many references to it do not represent many
accounts. Capture therefore copies the access token and expiry into QuotaHop's
vault and never a refresh token. Copying a refresh token would not create a
separate server-side lineage;
[RFC 9700 §4.14](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14)
describes rotation and reuse risks.

### Codex

- Cached credentials may be in `auth.json` or an OS credential store
  ([authentication](https://learn.chatgpt.com/docs/auth)). QuotaHop reads an
  explicitly selected file only and ignores refresh/ID tokens and API keys.
- Quota is `GET https://chatgpt.com/backend-api/wham/usage` with a ChatGPT account
  ID header, as in OpenAI's own
  [backend client](https://github.com/openai/codex/blob/main/codex-rs/backend-client/src/client.rs):
  `rate_limit.primary_window` / `secondary_window`, `used_percent`,
  `limit_window_seconds`, `reset_at`, credits and additional metered features. The
  same client has separate POST operations that consume reset credits; only the GET
  is implemented. This is an implementation interface, not a documented public
  API. 401/403, redirects and rate limits stop that account's attempt: no client
  impersonation, Cloudflare bypass, fallback host or retry.
- The documented app-server RPC `account/rateLimits/read` returns quota windows,
  but running the app server brings its authentication machinery, so it is not
  used. `account/usage/read` returns token counts, not subscription quota.

### Muse Code

Observed on Muse Code 1.4.6-R6000.1. Official references:
[subscriptions](https://dev.meta.ai/docs/muse-code/subscriptions) (PAYG API
spend is not subscription quota), [authentication](https://dev.meta.ai/docs/muse-code/auth),
and the MSP [`usage/read`](https://meta-models.github.io/muse-code-sdk/next/generated/msp/methods/usage-read/)
method, which returns a host's last-observed reading and can omit it.

- **CLI bootstrap (explicit opt-in).** The CLI obtains subscription quota from
  `POST https://api.meta.ai/muse-code/key`. QuotaHop sends one request with
  `{"onboard":false}` and the existing device token, requires a provisioned API
  key, checks the returned key equals the stored one, projects only plan and usage
  fields, and discards credentials. It is a credential-minting endpoint, not a
  read-only API. Direct invocation of the binary (avoiding the launcher's updater)
  against a local inspecting HTTPS gate showed HTTP 200 with the plan and the same
  key; `subs_usage` was present in some responses and omitted in others,
  and one omitted response coincided with `/usage` showing "currently
  unavailable". Absence is not zero, and no inference was made. Source bytes,
  mtime and atime were unchanged. This shows observed key reuse, **not a
  guarantee about future server behaviour**; key comparison detects a change but
  cannot undo it.
- **No standalone `usage` command.** A freshly started MSP host returned `{}` for
  `usage/read`, and an empty session does not populate it. The binary reads the
  XDG Muse auth path; the launcher's `MUSE_AUTH_PATH` override is not recognised.
- **Portal (GET-only alternative).** Meta's first-party frontend uses the browser
  session cookie `llama_dev_sess` with `GET /api/auth/me`, `GET /api/portal/teams`
  and `GET /api/portal/teams/{team_id}/subscription-quota`. The quota object has
  `tier`, `window_duration_secs`, `window_weighted_used/limit`,
  `window_resets_at`, `weekly_weighted_used/limit` and `weekly_resets_at`
  (schema first seen in the open-source
  [CodexBar provider](https://github.com/steipete/CodexBar/blob/main/Sources/CodexBarCore/Resources/Plugins/muse.ts)
  and confirmed against Meta's public frontend code). Privileged developer
  override headers in that code are not used. QuotaHop computes ratios from
  integer/string counters, preserves overage, withholds stale windows rather than
  assuming resets, never guesses a team, and does not claim the portal team is
  the CLI's account. Meta does not document this as a public API.

## Policy and compatibility limits

Anthropic's [credential-use documentation](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)
restricts third-party subscription login and credential intermediation, and its
[login guidance](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account)
prefers API keys for third-party tools and prohibits misrepresenting client
identity. Neither publishes the usage or profile endpoint as a supported
integration or explicitly settles personal read-only use. QuotaHop's narrow scope
is not a guarantee of permission, server behaviour or future compatibility, and
it does not bypass a denial. Equivalent caveats apply to the Codex and Meta
interfaces: first-party use of an endpoint establishes its purpose, not
third-party permission.

**No documented independent renewal mechanism exists** for a third-party
personal tool to keep a Claude subscription login alive, and other projects doing
so is not evidence that it is permitted. The [CLI reference](https://code.claude.com/docs/en/cli-reference)
lists no standalone refresh command; the documented multi-account route is
[separate configuration directories](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts).
What QuotaHop does instead is limited to:

- letting only the **unmodified first-party CLI** perform OAuth exchange, in a
  bubblewrap sandbox with a fresh home, with an initialization-only invocation and
  no prompt, user message or inference (see below);
- not reimplementing an OAuth client, authorisation flow or refresh endpoint, not
  copying a shared refresh token into a second owner, and identifying itself
  honestly (`quotahop/1.0`);
- using Codex's documented `account/read` RPC for Codex, and expiry tracking only
  for Muse;
- relying on Claude Code's own credential file format and locks for switching,
  which is an implementation-dependent local switch rather than an official API,
  and cannot guarantee that every running session adopts a new login at once;
- stopping at any denial.

Approaches deliberately not used: warm-up prompts through `claude -p` to force a
refresh (inference), file watchers that install and restore credentials
repeatedly, importing a shared refresh token as a second grant, scraping the
browser cookie endpoint for Claude, and credential-helper processes.

## Provider authentication behaviour (verified builds)

Method: native binaries were inspected as data, then run inside a bubblewrap
**offline network namespace** with fake credentials, a private fake CA, and local
HTTPS fixtures. The real home, keyrings, live credentials and internet were
unavailable and no inference request was made. Production installs no CA and
redirects no endpoint. Executable SHA-256 hashes are pinned in
`isolated_auth.BUILDS` (one pin for Claude Code 2.1.296's Linux binary, one for
Muse 1.4.6-R6000.1); other builds, even of the same version, fail closed.

**Claude Code 2.1.296.**
- `claude auth status --json` attempts a renewal during startup but can exit
  before saving the rotated credential, so it must not be used.
- An initialization-only invocation (`-p --input-format stream-json
  --output-format stream-json --verbose --no-session-persistence
  --strict-mcp-config` with a single `initialize` control request, then EOF)
  persisted the new access and refresh tokens and expiry. The native CLI's request
  trace was one `POST platform.claude.com/v1/oauth/token` and two
  `GET api.anthropic.com/api/oauth/profile`; no prompt or inference endpoint
  appeared. QuotaHop itself makes no profile request after a renewal (a failure
  there could discard the only valid copy) and keeps the existing account binding;
  enrollment binds the new grant with one profile read.
- The native credential writer persists optional `clientId` and
  `refreshTokenExpiresAt`; the grant validator accepts and preserves both, and they
  stay encrypted. Fixtures without them wrongly rejected real logins.
- The credential store holds separate `claudeAiOauth`, `designOauth` and
  `mcpOAuth` fields; logout/re-login can revoke Design credentials, so that path
  is never invoked. Plaintext writes serialise on a mkdir-based
  `.storage-write.lock`; global configuration uses `<config filename>.lock`.
  Claude reads identity from `oauthAccount` and detects credential-file mtime
  changes. Its refresh-save compare-and-set rejects an old refresh token when the
  stored one is null, which is why an access-only install writes `refreshToken:
  null` rather than a mismatched token. The switch file format and locks were
  checked through 2.1.308.

**Codex 0.162.1.** In an empty isolated home, `initialize`, `initialized` and
`account/read` with `refreshToken: false` returned an unauthenticated account and
created no credential file; no thread, turn or model request occurred. The
documented `account/read` with `refreshToken: true`
([auth state](https://learn.chatgpt.com/docs/app-server#1-check-auth-state)) forces
renewal of Codex-managed authentication. The RPC allowlist is initialization,
managed device sign-in and account read. Bounds: 10 s startup, 20 s per auth RPC,
up to ten minutes for interactive device sign-in; processes are reaped and the PID
namespace ends descendants. The
[CI/CD guidance](https://learn.chatgpt.com/docs/auth/ci-cd-auth) warns against
concurrent users of one credential copy, so enrolled grants are tool-owned.

**Muse 1.4.6-R6000.1.** MSP `account/loginStart` with `type: deviceCode`
completed against fake device-authorisation, token and key-bootstrap fixtures and
saved a separate `providers.meta` grant (DCA access and refresh tokens, expiry,
API key, display identity); the native bootstrap sent `{"onboard":false}` and
completion is signalled by `account/loginCompleted` with outcome `granted` (see
[`account/loginStart`](https://meta-models.github.io/muse-code-sdk/next/generated/msp/methods/account-loginstart/)).
Restarting with an expired fake grant and calling `account/read` and `model/list`
did **not** renew the device token; a mock 401 on `/muse-code/models` caused one
`/muse-code/key` re-mint and one retry while the saved expiry and refresh token
stayed unchanged. There is no verified native renewal, and re-minting could affect
an account-level key, so Muse support is sign-in, reporting and **expiry
tracking** (`reauth_required` at expiry, no background network activity).

**Build lookup and retention.** The pinned Claude binary is found as the `claude`
on PATH, a sibling build in Claude's versions directory, or a retained copy at
`${XDG_DATA_HOME:-~/.local/share}/quotahop/claude-<hash16>` (a hard link, with a
hash-verified copy fallback) that QuotaHop keeps so pruning of old versions cannot
stop renewal. A refusal before any process starts raises a preflight error: the
refresh token was never sent, so renewal pauses as `blocked` and retries every ten
minutes rather than suspending. Any failure after a request may have been sent
suspends the grant, since a lost response can hide a successful rotation.

## Claude renewal ownership

**Principle.** A refresh token should have one party rotating it. Once a grant's
tokens are in Claude Code's credentials file, running sessions can refresh it, and
a second refresher (the worker) leaves one side holding a superseded token. Filesystem
isolation alone does not keep a grant private once its access token is used
elsewhere. Upstream reports describe concurrent-session refresh races and
revocation ([#24317](https://github.com/anthropics/claude-code/issues/24317),
[#41507](https://github.com/anthropics/claude-code/issues/41507)); they are not a
provider guarantee about every token's lifetime, so QuotaHop assumes the
conservative case.

**Evidence relied on (Claude Code 2.1.300, inspected as data).** Before
refreshing, Claude Code takes a mkdir-based `.oauth_refresh.lock` in its config
directory (and the legacy sibling `<config dir>.lock`), **re-reads its credentials
under that lock**, and, if the file changed, **adopts the file instead of
refreshing**. Credential changes are also detected by file mtime. A process that
replaces the credentials while holding those locks therefore moves running
sessions to the new login without a refresh by them. The switch implementation
uses the same locks (refresh locks first, then `.storage-write.lock` and
`.claude.json.lock`, never stealing one) and was checked through 2.1.308.

**Design.**
- Each Claude grant has one owner: `ready` (QuotaHop renews it) or `cli_owned`
  (Claude Code renews it; QuotaHop mirrors the latest copy, encrypted, to be able
  to take it back). `legacy_hold` marks older grants with no ownership record;
  they are never refreshed until the CLI is confirmed to hold a different login.
- A switch gives the target's full grant to Claude Code and returns the outgoing
  login, from Claude Code's latest copy, to `ready`. The refresh-lock wait is 10 s
  (`claude_refreshing` on timeout); the storage and metadata locks fail
  immediately if held. A failed switch is reconciled on the next sync.
- The worker mirrors Claude Code's renewals of the active grant and, when the CLI
  login changes outside QuotaHop (such as `/login`), takes the previous account
  back and adopts the new one, including accounts that were only captured.
  Attribution is by exact access-token match, saved refresh-token match, or a
  server-verified account/organisation profile match against a saved binding
  (one profile GET, only when a Claude grant is already maintained and the token
  is valid for more than 30 s). If the current login cannot be attributed, or its
  grant is still the one a saved record holds under another identity, nothing is
  taken back: reclaiming could create a second owner.
- Reports and the panel follow Claude Code's already-renewed access token for
  `cli_owned` grants through read-only reads plus at most one profile GET, an exact
  binding and plan match, a later expiry, and a stable reread.
- `--undo-switch` restores only the two auth fields and refuses if QuotaHop again
  holds the saved refresh token; the undo record's copy goes stale once QuotaHop
  renews that grant.

**Limits.** A process already holding an old token may need restarting to recover.
Behaviour beyond the inspected Claude Code versions is unverified; the switch and
lock logic should be re-inspected after Claude Code changes its credential
storage or refresh path.
