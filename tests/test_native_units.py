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
    units = native_units.render(str(tmp_path), "/venv/bin/python", path="/usr/bin:/bin")
    assert set(units) == {f"karakos-{n}.service" for n in native_units.PROGRAMS} | {"karakos.target"}
    srv = units["karakos-agent-server.service"]
    # env(1) sets the root so an EnvironmentFile (config/.env) cannot override it
    assert (f"ExecStart=/usr/bin/env WORKSPACE_ROOT={tmp_path} KARAKOS_HOME={tmp_path} "
            f"/venv/bin/python {tmp_path}/bin/agent-server.py") in srv
    assert 'Environment="PATH=' in srv
    assert f"Environment=KARAKOS_HOME={tmp_path}" in srv
    assert "Restart=always" in srv and "TimeoutStopSec=45" in srv
    assert "RestartSteps=8" in srv and "SyslogIdentifier=karakos-agent-server" in srv
    assert "KillMode=mixed" in units["karakos-dashboard.service"]
    assert f"WorkingDirectory={tmp_path}/dashboard" in units["karakos-dashboard.service"]
    assert "karakos-relay.service" in units["karakos.target"]
    assert "$${DASHBOARD_PORT:-3000}" in units["karakos-dashboard.service"]  # systemd escape
    for text in units.values():
        assert not re.search(r"/workspace", text)


def test_cli_writes_files(tmp_path, monkeypatch):
    monkeypatch.setattr(native_units.shutil, "which", lambda _n: "/opt/bin/claude")
    assert native_units.main(["--home", str(tmp_path), "--out", str(tmp_path / "u")]) == 0
    assert (tmp_path / "u" / "karakos.target").exists()


def test_build_path_orders_venv_claude_then_base():
    p = native_units.build_path("/v/bin/python", "/home/u/.local/bin/claude", "/usr/bin:/v/bin")
    assert p == "/v/bin:/home/u/.local/bin:/usr/bin"


def test_render_uses_given_path_and_escapes_percent(tmp_path):
    srv = native_units.render(str(tmp_path), "/v/bin/python", "/v/bin:/a%b")["karakos-relay.service"]
    assert 'Environment="PATH=/v/bin:/a%%b"' in srv


def test_cli_fails_without_claude(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(native_units.shutil, "which", lambda _n: None)
    assert native_units.main(["--home", str(tmp_path), "--out", str(tmp_path / "u")]) == 1
    assert "claude" in capsys.readouterr().err
    assert not (tmp_path / "u").exists()
