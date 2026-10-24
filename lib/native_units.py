"""Generate systemd --user units for native (no-Docker) installs.

One template, four programs, mirroring config/supervisord.conf: same
commands, same restart policy, same stop timeout. A test keeps them in step.

    python3 lib/native_units.py --out ~/.config/systemd/user [--home DIR] [--python PATH]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# name -> (command template, workdir relative to home, TimeoutStopSec, extra [Service] lines)
# The dashboard's next-server is a child of the node process: KillMode=mixed and a
# port sweep stop a restart crash-looping on EADDRINUSE while the child holds the port.
_DASH_EXTRA = ("KillMode=mixed\n"
               "ExecStartPre=-/bin/bash -c 'fuser -k \"$${DASHBOARD_PORT:-3000}/tcp\"'\n")
PROGRAMS = {
    "agent-server": ("{python} {home}/bin/agent-server.py", ".", 45, ""),
    "relay": ("{python} {home}/bin/relay.py", ".", 10, ""),
    "dashboard": ("/bin/bash -c 'exec npx next start -p $${{DASHBOARD_PORT:-3000}}'", "dashboard", 10, _DASH_EXTRA),
    "scheduler": ("{python} {home}/bin/scheduler.py", ".", 5, ""),
}

TEMPLATE = """\
[Unit]
Description=Karakos {name}
After=network-online.target
Wants=network-online.target
PartOf=karakos.target

[Service]
Type=simple
WorkingDirectory={workdir}
Environment=HOME={userhome}
# EnvironmentFile= wins over Environment=, so ExecStart sets the root through env(1) too.
Environment=WORKSPACE_ROOT={home}
Environment=KARAKOS_HOME={home}
Environment="PATH={path}"
{envfile}ExecStart={command}
# Restart=always, not on-failure: a clean return from main() is exit 0 and must
# still restart, as supervisord's autorestart=true does. The backoff (systemd
# 254+) keeps a hard-down dependency at boot from becoming a tight loop without
# ever parking the unit in `failed`.
Restart=always
RestartSec=2
RestartSteps=8
RestartMaxDelaySec=300
KillSignal=SIGTERM
TimeoutStopSec={stop}
{extra}SyslogIdentifier=karakos-{name}

[Install]
WantedBy=karakos.target
"""

TARGET = """\
[Unit]
Description=Karakos (agent-server, relay, dashboard, scheduler)
Wants={wants}

[Install]
WantedBy=default.target
"""


def build_path(python: str, claude: str | None, base: str | None = None) -> str:
    """PATH for the units: the venv's bin, the directory holding `claude`, then the caller's PATH.

    The user manager's own PATH has neither, so without this `claude` is not found.
    """
    parts = []
    if os.sep in python:
        parts.append(str(Path(python).parent))
    if claude:
        parts.append(str(Path(claude).parent))
    parts += (base if base is not None else os.environ.get("PATH", "")).split(os.pathsep)
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return os.pathsep.join(out)


def render(home: str, python: str = "python3", path: str | None = None) -> dict[str, str]:
    home = str(Path(home).resolve())
    path = (path if path is not None else os.environ.get("PATH", "")).replace("%", "%%")
    env = Path(home) / "config" / ".env"
    envfile = f"EnvironmentFile=-{env}\n"
    units = {}
    userhome = os.environ.get("HOME") or str(Path.home())
    for name, (cmd, wd, stop, extra) in PROGRAMS.items():
        units[f"karakos-{name}.service"] = TEMPLATE.format(
            name=name, home=home, path=path, envfile=envfile, stop=stop, extra=extra, userhome=userhome,
            workdir=str(Path(home) / wd) if wd != "." else home,
            command=f"/usr/bin/env WORKSPACE_ROOT={home} KARAKOS_HOME={home} " + cmd.format(python=python, home=home))
    units["karakos.target"] = TARGET.format(
        wants=" ".join(f"karakos-{n}.service" for n in PROGRAMS))
    return units


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=os.environ.get("KARAKOS_HOME")
                    or os.path.abspath(os.path.join(__file__, "..", "..")))
    ap.add_argument("--python", default=sys.executable, help="interpreter (the venv's python)")
    ap.add_argument("--out", required=True, help="directory for the unit files")
    a = ap.parse_args(argv)
    claude = shutil.which("claude")
    if not claude:
        print("error: `claude` is not on PATH; install the Claude Code CLI first", file=sys.stderr)
        return 1
    path = build_path(a.python, claude)
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    for fname, text in render(a.home, a.python, path).items():
        (out / fname).write_text(text)
        print(out / fname)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
