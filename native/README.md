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
- **Design fixed 2026-08-11, not yet in this codebase.** The
  restart-safety question above has a real fix now, built and verified
  live against a running deployment: `bin/relay.py` tracks in-flight
  handlers (`on_message` + all seven `/status /usage /clear /reload
  /compact /override /override-clear` slash commands) and drains up to
  10s on `SIGTERM` before closing, instead of dying mid-handler;
  `system/reload-on-commit.py` dispatches the bounce via a detached
  `Popen` rather than blocking `subprocess.run`. That covers the two of
  the native operator's three asks that weren't already handled
  (defer/exclude-self already existed here, via `SELF_PROCESS_WARN`
  excluding `bin/agent-server.py` — unaffected by this).

  **Caveat that matters for this PR specifically**: that fix was written
  and verified against `iacoley/heart-of-gold` (the install repo this
  install actually runs from), not this repo (`mcarmody/karakos-package`,
  the upstream source `native/`'s changes are drafted against). It is
  *not* included in this branch or this PR — deliberately, to keep
  scoped to `native/` only per the collision-avoidance note above (`bin/
  relay.py` is also where #136 is making changes; piling a third set of
  edits onto the same file across two open PRs is exactly the conflict
  this draft was designed to avoid). So: the mechanism this native path
  would inherit `bin/relay.py` from still needs the fix ported here
  before "keep it opt-in and default off" can actually be revisited — as
  of this PR, the underlying hook is still exactly as safe/unsafe as it
  was on 2026-08-10, only the *sibling* install-repo deployment has moved.
  Tracking as a followup, not resolving it in-place here.

Feedback still wanted on whether the unit shape generalizes past this
specific install, before any of this gets wired into the installers for
real.
