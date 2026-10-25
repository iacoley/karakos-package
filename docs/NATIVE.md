# Running Karakos natively (no Docker)

For a dedicated host (a mini PC, a WSL distro) where the machine is already the
boundary. Docker stays the default.

## Install

```bash
bash install.sh --native
```

This clones the release, runs the same wizard as the Docker path (`./setup.sh --native`,
which skips the Docker checks) and then `bin/native-install.sh`, which needs Python 3.11
or newer, git, node and npm, the Claude CLI and a systemd user session. It creates
`.venv`, builds the dashboard, runs `bin/native-bootstrap.sh` (the setup the container's
entrypoint does: schema stamp, data and inbox directories, hooks, slash commands),
writes the units, enables linger and starts `karakos.target`. Re-running is safe.
`--skip-dashboard` and `--no-start` are available on `bin/native-install.sh`.

## Upgrade

```bash
make upgrade-native                       # fast-forward the current branch
KARAKOS_REF=v2.5.0 make upgrade-native    # or a tag
```

A tag leaves the checkout on a detached HEAD. That is expected: the next plain `make upgrade-native`
refuses to fast-forward a detached checkout, so pass `KARAKOS_REF=<branch>` (for example `main`) to
return to a branch.

`bin/native-upgrade.sh` stops if tracked files have local changes, moves the checkout,
refreshes the venv and dashboard, re-runs the bootstrap and restarts the units.
Schema changes still go through `karakos migrate`; the bootstrap refuses data that needs it.

## What runs

`install.sh --native` does this for you. The same four programs as `config/supervisord.conf`: agent-server, relay,
dashboard, scheduler. `lib/native_units.py` writes one `systemd --user` unit for
each, plus `karakos.target` to start them together:

```bash
python3 lib/native_units.py --out ~/.config/systemd/user --python "$PWD/.venv/bin/python"
systemctl --user daemon-reload
systemctl --user enable --now karakos.target
```

Units set `KARAKOS_HOME` and `WORKSPACE_ROOT` to the checkout and read
`config/.env`. A test keeps the program set and stop timeouts in step with
`config/supervisord.conf`.

## Design notes

- **User units need linger.** They stop at logout unless
  `loginctl enable-linger $USER` is set. On WSL the distro can also shut down
  when the last session closes; test that, do not assume systemd keeps it up.
- **`Restart=always`, not `on-failure`.** A clean return from `main()` is exit 0
  and must still restart, as supervisord's `autorestart=true` does.
- **Backoff.** `RestartSteps=8` and `RestartMaxDelaySec=300` (systemd 254 or newer)
  stop a hard-down dependency at boot from becoming a tight restart loop, without
  parking the unit in `failed`.
- **Dashboard.** `next-server` is a child of the node process. `KillMode=mixed`
  and a port sweep before start stop a restart crash-looping on `EADDRINUSE`.
- **No PTY needed.** The agent server talks to the Claude CLI over stdin/stdout
  pipes (stream-json), not a terminal.
- **Logs** go to the journal, one `SyslogIdentifier` per program
  (`journalctl --user -u karakos-relay`).
- **Reload-on-commit.** Do not port auto-restart-on-commit as is. The agent server
  must never be bounced by a commit it is delivering a reply for, and the relay
  needs to drain in-flight handlers on SIGTERM first. Tracked as a follow-up.

## PATH and the install root

The units set `PATH` at generate time: the venv's `bin`, the directory holding `claude`, then the `PATH` of the shell that ran the generator. Generation fails if `claude` is not found. Re-run the generator after moving or upgrading the CLI.

`config/.env` is loaded as an `EnvironmentFile`, which overrides `Environment=` lines. To keep a stale `WORKSPACE_ROOT` in that file from winning, each `ExecStart` sets `WORKSPACE_ROOT` and `KARAKOS_HOME` through `env(1)`.

## Moving from a container install

Only `data/`, `logs/` and `inbox/` live in Docker named volumes; `config/`, `agents/` and `.karakos/` are bind mounts from the checkout, so the checkout you already have is the native one.

```bash
make down                        # the container must be stopped
bin/native-migrate.sh --dry-run  # shows what it would copy
bin/native-migrate.sh            # cp -a each volume into the checkout, drops WORKSPACE_ROOT from config/.env
bin/native-bootstrap.sh
```

The copy runs as root inside a throwaway container and the files are then `chown`ed to the user who ran the script, so the host uid does not have to match the image's. The script refuses to run while the container is up or when `data/`, `logs/` or `inbox/` already hold files; `--move-aside` renames such a directory to `<dir>.pre-native.<time>` first (a stale `data/build-queue.db` from an earlier host run is the usual case). It never deletes the volumes (`config_karakos-data`, `config_karakos-logs`, `config_karakos-inbox` for the default project): keep them, and the stopped container, for a day on native before removing them. To roll back, start the container again.
