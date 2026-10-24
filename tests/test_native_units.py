import configparser
import re
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import native_units  # noqa: E402


def _supervisord():
    cp = configparser.RawConfigParser(strict=False)
    cp.read(ROOT / "config" / "supervisord.conf")
    return {s.split(":", 1)[1]: dict(cp[s]) for s in cp.sections() if s.startswith("program:")}


def test_same_programs_as_supervisord():
    assert set(native_units.PROGRAMS) == set(_supervisord())


def test_stop_timeouts_match_supervisord():
    for name, sec in _supervisord().items():
        assert native_units.PROGRAMS[name][2] == int(sec["stopwaitsecs"]), name


def test_render(tmp_path):
    units = native_units.render(str(tmp_path), "/venv/bin/python")
    assert set(units) == {f"karakos-{n}.service" for n in native_units.PROGRAMS} | {"karakos.target"}
    srv = units["karakos-agent-server.service"]
    assert f"ExecStart=/venv/bin/python {tmp_path}/bin/agent-server.py" in srv
    assert f"Environment=KARAKOS_HOME={tmp_path}" in srv
    assert "Restart=always" in srv and "TimeoutStopSec=45" in srv
    assert "RestartSteps=8" in srv and "SyslogIdentifier=karakos-agent-server" in srv
    assert "KillMode=mixed" in units["karakos-dashboard.service"]
    assert f"WorkingDirectory={tmp_path}/dashboard" in units["karakos-dashboard.service"]
    assert "karakos-relay.service" in units["karakos.target"]
    assert "$${DASHBOARD_PORT:-3000}" in units["karakos-dashboard.service"]  # systemd escape
    for text in units.values():
        assert not re.search(r"/workspace", text)


def test_cli_writes_files(tmp_path):
    assert native_units.main(["--home", str(tmp_path), "--out", str(tmp_path / "u")]) == 0
    assert (tmp_path / "u" / "karakos.target").exists()
