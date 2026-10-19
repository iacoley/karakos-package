# The overlay: per-install data without a fork

Tracking issue: karakos-package #243 ("MÍRDAIN G12: a private overlay repo for per-install
data, so installs never fork the package"). The point: an install's own config, personas and
skills must not require a fork of the package, because a fork cannot receive upstream fixes.

## What it is

The overlay is a second git repository, private to you, at `overlay/` inside the install.
The package gitignores that directory, so it never enters the package repo. You can push it to
your own remote as a backup. The remote URL is read once, by `init`.

The overlay holds **named seams**, not a copy of the install. A seam is a path the install
reads that you are allowed to change:

| Seam | Meaning |
|---|---|
| `config/agents.yaml` | the agent registry |
| `config/channels.json` | channel mapping |
| `agents/<name>/` | each agent directory (persona, prompts), except `agents/templates/` |
| `skills/<name>/` | each skill directory the package does **not** ship |
| `memory-exports/` | optional; memory exports you put there by hand |
| `secrets.manifest` | overlay root only: secret **names** (see below), never values |

Not seams, so never captured or applied: `agents/CORE.md`, `agents/HOUSE_STYLE.md`,
`agents/templates/`, `__pycache__/`, `*.generated.md`, and any `inbox/` or `journal/`
directory (runtime state).

## Precedence

Package defaults first, then the overlay on top. `apply` copies seam files from the overlay
into the install and overwrites the install copy. Two rules limit it:

- It never writes a path that `git ls-files` of the package checkout lists as tracked. Such a
  path is reported as `skip-tracked` and the install copy is left alone.
- A skill the package ships is never overwritten or captured, whatever the overlay holds
  (any file under a shipped skill directory is `skip-tracked`). This relies on the install
  being a git checkout: with no `.git`, nothing counts as tracked.

`apply` is idempotent: files that already match are reported as `unchanged`.

## Setup

```bash
# New overlay, empty, from the install root (~/karakos):
python3 bin/overlay.py init

# Or with a remote you already created (the owner supplies credentials, as for any git remote):
python3 bin/overlay.py init --remote git@your-host:you/karakos-overlay.git
# Or set KARAKOS_OVERLAY_REPO in config/.env (parsed, never sourced) and run init.

python3 bin/overlay.py status     # path, remote, branch, and the apply plan
python3 bin/overlay.py capture    # copy install seams into the overlay and commit
python3 bin/overlay.py push       # push the overlay to its remote
python3 bin/overlay.py pull       # pull the overlay (fast-forward only), then apply
python3 bin/overlay.py apply      # copy overlay seams into the install
python3 bin/overlay.py check-secrets
```

`init` creates `overlay/.git` on branch `main`, a `.gitignore` (blocks `.env*`, `*.pem`,
`*.key` and `secrets/`), a short `README.md`, an empty `secrets.manifest`, and the
pre-commit guard, then makes the first commit `Initial overlay`. Running it again prints
`overlay already initialised` and changes nothing. If a remote is configured and `overlay/`
does not exist, `init` clones the remote instead and installs the guard in the clone.

`capture` copies seam files install -> overlay (skipping tracked paths and shipped skills),
runs `git add -A` in the overlay and commits `Capture install state` only if something
changed. Run it after you change a persona or add a skill in the install, then `push`.

## Upgrades

Package upgrades never touch `overlay/`. The upgrade flow is:

```bash
git pull                          # the package (develop or the release you track)
python3 bin/overlay.py apply      # re-assert your overlay over the new package
```

On container start, `bin/entrypoint.sh` also runs `apply --quiet` when `overlay/.git` exists,
after the schema check passes. A failure there is never fatal.

## Secrets

Secrets stay out of git entirely, in both repos:

- Values live in an env file **outside** both repos. `config/.env` in the install is already
  gitignored by the package. Do not copy it into the overlay.
- `secrets.manifest` lists the names only, one per line, `#` comments allowed:

  ```
  # env var names the install needs; values live in config/.env
  DISCORD_BOT_TOKEN
  ANTHROPIC_API_KEY
  ```

- The overlay's `.gitignore` covers `.env*`, `*.pem`, `*.key` and `secrets/`. The pre-commit
  hook also refuses `id_*` files and scans staged lines for token patterns. The hook runs
  `system/check-secrets.py` from the package, the same rules the package uses. The rules are
  not copied into the overlay, so they cannot drift.
- `python3 bin/overlay.py check-secrets` runs the same rules over every tracked file in the
  overlay. The hook only sees what is staged, so use this after a history change or a manual
  edit.

`capture` skips secret-looking files in the install (`.env*`, keys, `secrets/`), so an install
secret is never copied into the overlay.

## Backups

`lib/migrate/backup.py` includes `overlay/` in a backup when it exists, next to `data`,
`config` and `agents`. The overlay's own `.git` is left out: its objects are read-only, so a
restore onto the live overlay would fail, and its history is on the overlay's remote. A restore
puts the seam files back on the same machine. On a new machine, clone the overlay remote
(`init --remote`, into an empty or absent `overlay/`) and then run `apply`.

## Known gap: the container

The compose file bind-mounts only `config/`, `agents/` and `.karakos/`. `overlay/` is not
mounted, so `bin/entrypoint.sh` finds no `overlay/.git` inside the container and its `apply`
does nothing. Run `python3 bin/overlay.py apply` on the host instead:

- `config/` and `agents/` changes reach the container through the bind mounts;
- `skills/` is baked into the image, so a skill change needs an image rebuild.

Mounting `overlay/` is a compose change (`config/` is a protected path), so it is not part of
this change.

## Is the vault tool the same thing?

No. The vault tool is a separate git-backed knowledge vault (`vault/`, synced by the `vault`
tool). The overlay holds install config, personas and skills. Keep them separate; do not
move the overlay into the vault or the vault into the overlay.

## Code

- `lib/overlay.py`: seam listing, the apply and capture plans, tracked-path checks, the
  guard hook, and the scan over tracked files. No network.
- `bin/overlay.py`: the subcommands above.
- `tests/test_overlay.py`: init, apply, capture, tracked and shipped-skill rules, the hook
  rejecting a staged `.env`.
