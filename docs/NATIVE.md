# Running Karakos natively (no Docker)

For a dedicated host (a mini PC, a WSL distro) where the machine is already the
boundary. Docker stays the default. This page covers the process layer; the
installer and upgrade path land in the next parts of the native-mode work.

## What runs

The same four programs as `config/supervisord.conf`: agent-server, relay,
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
