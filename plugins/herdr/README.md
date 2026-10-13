# QuotaHop for Herdr

A small, live **Claude / Codex / Muse Code** quota panel alongside your work.
Uses your existing QuotaHop installation and vault. Linux, Python 3.12+, Herdr
0.9.1+. No extra Python packages, separate account store, or hosted service.

```text
QUOTAHOP  9/9 checked OK
Next check 48s · checked <1m ago
OPENAI · used % / reset
▶ alex@example.com · Codex
  5h    —     —  7d  26% 5d09
ANTHROPIC · used % / reset
○ alex@example.com · Max ✓
  5h  42%  1h23  7d  64% 3d02
▶ alex@example.com · Team ✓
  5h  12%  2h10  7d  35% 4d03
META · used % / reset
▶ alex@example.net · Muse
  5h  29%  3h08  7d  23% 5d12
```

Illustrative values. Percentages are **used**, followed by time until reset.
No quota is inferred when the provider omits it. Each subscription has its own
row; a shared email does not combine personal and Team allowances.

## Install and open

First [install and configure QuotaHop](../../README.md#quick-start). Ensure
`quotahop --help` includes `--panel` and that `quotahop` is on your PATH, or
configure its absolute path below. The plugin intentionally uses that existing
installation so installing or updating a Herdr plugin cannot replace your vault.

```sh
herdr plugin install turbokast/quotahop/plugins/herdr
herdr plugin action invoke turbokast.quotahop.open
```

The action opens a right split and aims for 44 columns on a wide terminal.
It keeps focus in your current pane. Focus QuotaHop with Herdr's usual pane
navigation; press **u** to unlock if the five-minute cache has expired.
Each invocation opens a new panel; close unused ones with **q**.

For a plain split, or to choose another placement:

```sh
herdr plugin pane open --plugin turbokast.quotahop --entrypoint usage --direction right --no-focus
```

The panel belongs to a **tab**. Herdr plugin v1 does not provide a global pinned
status widget across all tabs/workspaces. Open one where you work; normal Herdr
move/resize controls also apply. No startup hook or automatic pane creation is
installed.

For local development, register this directory instead of the GitHub install:

```sh
herdr plugin link /absolute/path/to/quotahop/plugins/herdr
```

Linked plugins use local edits immediately. To move from a link to a GitHub
install, first `herdr plugin unlink turbokast.quotahop`. Reinstall the GitHub
source to update it. Neither operation deletes the QuotaHop vault.

## Controls and display

| Key | Action |
|---|---|
| `u` | Unlock the panel using the existing vault/cache. |
| `r` | Fetch now; requests never overlap and manual checks have a five-second cooldown. |
| `p` | Pause/resume quota checks; cancel any active batch. |
| `d` | Show full account names, plan, exact local reset timestamps and errors. |
| `↑` / `↓`, `j` / `k`, Page Up / Page Down | Scroll when accounts or details exceed the pane height. |
| `l` | Lock this panel and clear its displayed report. |
| `q` / Escape / Ctrl-C | Close, cancel and reap pending requests, release the panel's unlock. |

Accounts keep the normal report's recommended order within provider groups.
`▶` (cyan) means selected in current CLI files; `○` means standby and `?` means
selection could not be verified. Already-running CLI sessions can retain another
login. `✓` means a Claude switching candidate, **not** automatic renewal.
Yellow/red indicate high usage; `◷` marks imported snapshots.

The header shows when the last batch was checked. Paused or failed checks retain
that timestamp; past reset times display `due` without inventing a fresh quota.
Exact reset timestamps in details use your local timezone. `NO_COLOR` is respected.

## Optional configuration

```sh
herdr plugin config-dir turbokast.quotahop
```

Create `config.json` inside the printed directory, containing only the settings
you want to override:

```json
{
  "quotahop_path": "/absolute/path/to/quotahop/quotahop",
  "data_dir": "/absolute/path/to/existing/.usage-data",
  "interval_seconds": 60
}
```

Paths must be absolute. With no config, the plugin runs `quotahop` from PATH,
using that installation's existing default vault. Refresh intervals can be
30–3600 seconds and are measured **after** a batch finishes. No tokens,
passwords or shell commands belong in plugin config.

Optional Herdr keybinding (add yourself to your Herdr configuration):

```toml
[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "turbokast.quotahop.open"
description = "open quota panel"
```

## Authentication and lifecycle

Only an explicitly opened panel polls usage. Ordinary `quotahop` still fetches
once and exits. The panel starts no service, timer, detached process, native
login, inference, token renewal or account switch. Closing it stops its own
workers; a separately enrolled renewal worker is unaffected.

After unlocking, **the panel retains its derived vault key in memory until
locked or closed**. It does not extend the five-minute cache for other commands.
Each check rereads the encrypted vault to see credentials updated by independent
renewal. `quotahop --lock` also signals all panels for this vault to lock and clear
their displays, checked about once per second. A password change invalidates
the panel's old key on its next check. `l` locks only this panel, leaving the
normal cache and independent renewal worker alone.

The panel uses the same provider clients as the report, including the **Muse
CLI bootstrap POST** when that source is configured. Repeated checks therefore
repeat that documented limitation; the plugin does not add any endpoints.
It never writes provider authentication. Report data stays in memory and is
not cached on disk or sent to Herdr's marketplace.

## Publishing

This plugin lives with QuotaHop because its UI and CLI need to stay compatible.
A second repository would add release coordination without improving isolation.

Herdr accepts subdirectory manifests. Publishing this directory on QuotaHop's
default branch and adding the GitHub topic **`herdr-plugin`** makes it eligible
for discovery. The marketplace automatically refreshes, normally every 30 minutes;
there is no approval or PR submission queue. A listing is not an endorsement.
See the official [plugin reference](https://herdr.dev/docs/plugins/) and
[marketplace publishing guide](https://herdr.dev/docs/marketplace/).
