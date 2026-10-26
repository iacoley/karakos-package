import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["bin/native-install.sh", "bin/native-bootstrap.sh", "bin/native-upgrade.sh",
           "install.sh", "setup.sh"]


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_parses(script):
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


def test_makefile_has_upgrade_native():
    mk = (ROOT / "Makefile").read_text()
    assert re.search(r"^upgrade-native:\n\t\./bin/native-upgrade\.sh$", mk, re.M)
    assert "upgrade-native" in mk.split("\n", 1)[0]


def test_install_and_setup_document_native_flag():
    for f in ("install.sh", "setup.sh"):
        assert "--native" in (ROOT / f).read_text(), f


def test_docker_checks_are_skipped_when_native():
    setup = (ROOT / "setup.sh").read_text()
    assert 'if [ "$NATIVE" != 1 ]; then' in setup
    inst = (ROOT / "install.sh").read_text()
    assert inst.index('if [ "$NATIVE" != 1 ]; then') < inst.index("get.docker.com")


def _ws(tmp_path):
    """A workspace holding just the native scripts and the resolver."""
    (tmp_path / "bin" / "lib").mkdir(parents=True)
    for f in ("native-install.sh", "native-bootstrap.sh", "native-upgrade.sh"):
        shutil.copy(ROOT / "bin" / f, tmp_path / "bin" / f)
    shutil.copy(ROOT / "bin" / "lib" / "paths.sh", tmp_path / "bin" / "lib" / "paths.sh")
    return tmp_path


def test_bootstrap_without_env_file_says_what_to_do(tmp_path):
    ws = _ws(tmp_path)
    r = subprocess.run(["bash", str(ws / "bin" / "native-bootstrap.sh")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "KARAKOS_HOME": str(ws)})
    assert r.returncode == 1 and "setup.sh --native" in r.stderr


def test_install_without_config_fails_before_touching_anything(tmp_path):
    ws = _ws(tmp_path)
    r = subprocess.run(["bash", str(ws / "bin" / "native-install.sh"), "--skip-dashboard"],
                       capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "KARAKOS_HOME": str(ws), "HOME": str(ws)})
    assert r.returncode != 0
    assert not (ws / ".venv").exists()


def test_upgrade_refuses_outside_a_git_checkout(tmp_path):
    ws = _ws(tmp_path)
    r = subprocess.run(["bash", str(ws / "bin" / "native-upgrade.sh")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "KARAKOS_HOME": str(ws)})
    assert r.returncode == 1 and "not a git checkout" in r.stderr


def test_bootstrap_ignores_workspace_root_in_env_file(tmp_path):
    """A .env from a container install says /workspace; the checkout must win."""
    ws = _ws(tmp_path / "ws")
    (ws / "config").mkdir(exist_ok=True)
    (ws / "config" / ".env").write_text(
        "WORKSPACE_ROOT=/workspace\nDASHBOARD_PORT=3000\nAGENT_SERVER_TOKEN=x\n")
    stub = tmp_path / "stub"
    stub.mkdir()
    log = tmp_path / "seen.log"
    (stub / "python3").write_text(f'#!/bin/bash\necho "$WORKSPACE_ROOT|$KARAKOS_HOME|$PWD" >> {log}\necho main\n')
    (stub / "python3").chmod(0o755)
    r = subprocess.run(["bash", str(ws / "bin" / "native-bootstrap.sh")], capture_output=True, text=True,
                       env={"PATH": f"{stub}:/usr/bin:/bin", "KARAKOS_HOME": str(ws), "HOME": str(tmp_path)})
    seen = log.read_text().splitlines()
    assert seen, r.stderr
    for line in seen:
        assert line == f"{ws}|{ws}|{ws}", (line, r.stderr)


def test_setup_native_does_not_write_workspace_root():
    text = (ROOT / "setup.sh").read_text()
    assert "sed -i '/^WORKSPACE_ROOT=/d'" in text
