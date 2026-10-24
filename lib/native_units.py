"""Generate systemd --user units for native (no-Docker) installs.

One template, four programs, mirroring config/supervisord.conf: same
commands, same restart policy, same stop timeout. A test keeps them in step.

    python3 lib/native_units.py --out ~/.config/systemd/user [--home DIR] [--python PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# name -> (command template, workdir relative to home, TimeoutStopSec)
PROGRAMS = {
    "agent-server": ("{python} {home}/bin/agent-server.py", ".", 45),
    "relay": ("{python} {home}/bin/relay.py", ".", 10),
    "dashboard": ("/bin/bash -c 'exec npx next start -p $${{DASHBOARD_PORT:-3000}}'", "dashboard", 10),
    "scheduler": ("{python} {home}/bin/scheduler.py", ".", 5),
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
Environment=WORKSPACE_ROOT={home}
Environment=KARAKOS_HOME={home}
{envfile}ExecStart={command}
Restart=always
RestartSec=2
KillSignal=SIGTERM
TimeoutStopSec={stop}

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


def render(home: str, python: str = "python3") -> dict[str, str]:
    home = str(Path(home).resolve())
    env = Path(home) / "config" / ".env"
    envfile = f"EnvironmentFile=-{env}\n"
    units = {}
    for name, (cmd, wd, stop) in PROGRAMS.items():
        units[f"karakos-{name}.service"] = TEMPLATE.format(
            name=name, home=home, envfile=envfile, stop=stop,
            workdir=str(Path(home) / wd) if wd != "." else home,
            command=cmd.format(python=python, home=home))
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
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    for fname, text in render(a.home, a.python).items():
        (out / fname).write_text(text)
        print(out / fname)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
