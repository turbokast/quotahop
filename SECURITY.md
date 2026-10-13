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
  An explicitly started renewal worker holds the key in memory until stopped.
  An explicitly opened `--panel` similarly retains a key until locked or closed,
  without extending the normal cache TTL. It rereads the vault for each batch.
- Claude usage checks share a private `quota-cache/` directory inside the data
  directory (`0700`, files `0600`). It stores parsed metrics, check times, cooldowns
  and one-way credential bindings, never access tokens or raw response bodies.
  This usage cache is separate from the encrypted vault and unlock-key cache.
- Reports and captures do not modify provider authentication. Explicit Claude
  switches, including a confirmed panel action, modify the shared subscription
  login; isolated enrollment owns its
  separate grants. See [USAGE.md](USAGE.md) for the precise boundaries.
- Usage clients restrict endpoint paths and methods. No inference is performed.
  Muse CLI quota retrieval uses an explicitly selected bootstrap POST, whose
  server-side behaviour is not guaranteed to remain read-only.
- Root or a compromised local user can access unlocked process memory. Deleting
  a file does not guarantee secure disk erasure. A forced kill can leave private
  temporary native-auth files behind.
- Undocumented provider interfaces may change, stop working, or be restricted by
  provider policies. Hash checks on native auth binaries deliberately fail closed.

`quotahop --lock` stops the worker, clears this vault's cached unlock and signals
open quota panels to clear their displays and keys (checked about once per second).
The optional Herdr plugin launches the installed QuotaHop command; it never copies
credentials into its checkout or config. Full panel reports stay in memory;
the short Claude usage cache above is shared with ordinary reports and switching.
Password changes re-encrypt credentials, renewal grants and undo history; they
do not revoke provider sessions or make independently saved old backups unreadable.
