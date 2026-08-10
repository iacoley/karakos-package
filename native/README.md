# Native (no-Docker) install — draft

Systemd-unit-based alternative to the container/`supervisord` runtime, for
running Karakos directly on a host (WSL-native is the motivating case, but
these should generalize to any systemd-capable Linux).

**Status: draft, not wired into `install.sh`/`install.ps1` yet.** Placeholders
(`@@INSTALL_DIR@@`, `@@KARAKOS_USER@@`, `@@INSTALL_DIR_PARENT@@`,
`@@DASHBOARD_PORT@@`) are not yet substituted by a generator script — filling
them in is manual for now.

## What's here

- `systemd/karakos-{agent-server,relay,scheduler,recovery-agent,dashboard}.service`
  — one unit per `supervisord.conf` program, same `stopwaitsecs`/`stopsignal`
  mapped to `TimeoutStopSec`/`KillSignal`.
- `start.sh` — one-time bootstrap replacing the setup portion of
  `bin/entrypoint.sh` (env-var check, data/log/inbox directories, git hook
  install, Discord slash-command registration). Does **not** touch
  `entrypoint.sh` itself — deliberately scoped to avoid colliding with #136.

## Design notes / open questions

- System units (`/etc/systemd/system`), not user units — user units die at
  logout without `loginctl enable-linger`, and on WSL specifically the
  distro can shut down when the last session closes, a related failure mode
  worth testing explicitly rather than assuming systemd persistence.
- `Restart=always` (not `on-failure`) for the four long-running
  listeners — a clean gateway-close/`main()` return is exit 0, and
  `on-failure` would leave the unit dead with no restart, which is worse
  than what `supervisord`'s `autorestart=true` does today.
  `Restart=on-failure` is kept for dashboard only.
- `RestartSteps=8` / `RestartMaxDelaySec=300` backoff on every unit, so a
  hard-down dependency at boot can't become an infinite tight restart loop.
- Dashboard: `KillMode=mixed` + `ExecStartPre=-/usr/bin/fuser -k PORT/tcp` —
  `next-server` is a child of the node process; without this, a restart
  crash-loops on `EADDRINUSE` because the child keeps holding the port.
- Every unit sets `Environment=HOME=...` explicitly and uses
  `EnvironmentFile=` for the rest — systemd units inherit almost nothing
  from the interactive shell, unlike a container's `ENV` block.
- `agent-server` needs no PTY/tmux — confirmed by reading
  `start_agent_subprocess()` in `bin/agent-server.py`: it talks to the
  Claude CLI over stdin/stdout pipes (`stream-json`), not a terminal.
- **Open**: whether the existing auto-reload-on-commit pattern
  (`system/reload-on-commit.py`) is safe to run natively at all, or needs
  rework — restarting the process that's mid-way through delivering a reply
  drops it, and a bad native hot-patch has no container image to fall back
  to. Recommendation from a native operator (see PR discussion): keep it
  opt-in and default off; if kept, never restart synchronously inside the
  hook, gate on idle rather than a fixed sleep, and defer/exclude the unit
  that's running the session which made the commit.

Feedback wanted specifically on the restart-safety question above and on
whether the unit shape generalizes past this specific install, before any
of this gets wired into the installers for real.
