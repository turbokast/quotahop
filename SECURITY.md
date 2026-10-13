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
- Reports and captures do not modify provider authentication. Explicit Claude
  switches modify the shared subscription login; isolated enrollment owns its
  separate grants. See [USAGE.md](USAGE.md) for the precise boundaries.
- Usage clients restrict endpoint paths and methods. No inference is performed.
  Muse CLI quota retrieval uses an explicitly selected bootstrap POST, whose
  server-side behaviour is not guaranteed to remain read-only.
- Root or a compromised local user can access unlocked process memory. Deleting
  a file does not guarantee secure disk erasure. A forced kill can leave private
  temporary native-auth files behind.
- Undocumented provider interfaces may change, stop working, or be restricted by
  provider policies. Hash checks on native auth binaries deliberately fail closed.

`quotahop --lock` stops the worker and clears this vault's cached unlock.
Password changes re-encrypt credentials, renewal grants and undo history; they
do not revoke provider sessions or make independently saved old backups unreadable.
