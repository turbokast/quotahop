# Security policy

## Report a vulnerability privately

Use [GitHub private vulnerability reporting](https://github.com/turbokast/quotahop/security/advisories/new).
Please describe the affected commit, a minimal reproduction using fake data,
and the impact. Do not post vulnerabilities involving credentials in public issues.
Never submit real tokens, passwords, cookies, account files, or vault contents,
even encrypted. Response times are not guaranteed for this personal project.

Security fixes target the latest `main` branch. There are no maintained older
release branches at present.

## Protection boundaries

- Account secrets are encrypted with AES-GCM and a scrypt-derived key.
  Storage directories use `0700`; files use `0600`.
- The five-minute unlock cache holds a derived key in the Linux user keyring.
  The renewal worker holds the key in memory until stopped; a reboot stops it.
  An interactive unlock (a report, or `u` in the panel) restarts it when grants
  need maintenance, except after an explicit `--background stop` or `--lock`.
  An explicitly opened `--panel` similarly retains a key until locked or closed,
  without extending the normal cache TTL. It rereads the vault for each batch.
- **Optional autostart** (`--background enable-autostart`) stores the derived
  vault key in the desktop login keyring (Secret Service) and writes
  `~/.config/autostart/quotahop-renewal.desktop`, so renewal restarts at login
  without a passphrase. While you are logged in, any process running as you can
  read that key, and at rest it is protected by your login password rather than
  the vault passphrase. The login item never prompts, waits up to 60 seconds for
  the keyring, and respects an explicit stop. `--lock` does not remove the stored
  key; `--background disable-autostart` removes both it and the login item. Avoid
  it with auto-login, an empty keyring password, or shared accounts. See the
  [trade-offs](USAGE.md#renewal-after-reboot).
- Claude usage checks share a private `quota-cache/` directory inside the data
  directory (`0700`, files `0600`). It stores parsed metrics, check times, cooldowns
  and one-way credential bindings, never access tokens or raw response bodies.
  This usage cache is separate from the encrypted vault and unlock-key cache.
- Reports never run inference or login and never renew a token themselves. They
  may restart a stopped worker (see above) and, for Claude logins Claude Code
  holds, copy its already-renewed access token into the encrypted vault, using
  read-only file reads and at most one profile GET. Captures do not modify
  provider authentication. Explicit Claude switches, including a confirmed panel
  action, modify the shared subscription login; isolated enrollment owns its
  separate grants. See [USAGE.md](USAGE.md) for the precise boundaries.
- Each Claude grant has exactly one renewal owner. An explicit switch gives the
  target's full grant to Claude Code, which renews it while active; while holding
  Claude Code's refresh locks, QuotaHop takes the outgoing login back from Claude
  Code's latest copy and renews it in the background. Running sessions re-read
  their credentials under that lock and adopt the new file, so they never refresh
  the returned grant. The worker also takes back the previous login and adopts the
  new one when Claude Code's login changes outside QuotaHop (such as `/login`),
  including accounts that were only captured, but only after attributing the
  login by exact token or a server-verified account/organisation match; an
  unattributed login is never taken. Captures still copy access tokens only.
  Renewal copies of Claude Code's grants are encrypted in the renewal registry.
  The switch undo record also holds the outgoing refresh token (encrypted) and goes
  stale once QuotaHop renews that grant; undo refuses if QuotaHop holds it again.
- Usage clients restrict endpoint paths and methods. No inference is performed.
  Muse CLI quota retrieval uses an explicitly selected bootstrap POST, whose
  server-side behaviour is not guaranteed to remain read-only.
- Root or a compromised local user can access unlocked process memory. Deleting
  a file does not guarantee secure disk erasure. A forced kill can leave private
  temporary native-auth files behind.
- Undocumented provider interfaces may change, stop working, or be restricted by
  provider policies. Hash checks on native auth binaries deliberately fail closed.
  QuotaHop keeps a retained, hash-verified copy of the pinned Claude build under
  `${XDG_DATA_HOME:-~/.local/share}/quotahop/` so renewal survives Claude Code
  pruning old versions; if no verified build exists, renewal pauses without
  sending any refresh token.

`quotahop --lock` stops the worker (it stays stopped until `--background start`),
clears this vault's cached unlock, and signals open quota panels to clear their
displays and keys (checked about once per second). The optional Herdr plugin
launches the installed QuotaHop command; it never copies credentials into its
checkout or config. Full panel reports stay in memory; the short Claude usage
cache above is shared with ordinary reports and switching. Password changes
re-encrypt credentials, renewal grants and undo history, and update a key stored
by autostart; they do not revoke provider sessions or make independently saved
old backups unreadable.
