"""bin/buildq doctor and enable: run against a temp package root (spec MÍRDAIN G3b).

bin/buildq has no .py suffix, so import_script() cannot find it; load it with an
explicit SourceFileLoader under a name that does not clash with lib/buildq.py.
"""
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import build_hosts  # noqa: E402

# Written out here, not imported from bin/buildq, so the test pins the literal text.
EXPECTED_YAML = """\
# Build queue. `bin/buildq doctor` checks what it needs.
enabled: true
default_host: local
hosts:
  local:
    kind: local
    concurrency: 1
retry:
  max_attempts: 1
"""


def _load_buildq_cli():
    path = str(ROOT / "bin" / "buildq")
    loader = importlib.machinery.SourceFileLoader("buildq_cli_under_test", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    mod = _load_buildq_cli()
    monkeypatch.setattr(mod, "PKG", tmp_path)
    return mod


def _checks(capsys):
    return {c["name"]: c for c in json.loads(capsys.readouterr().out)}


def test_doctor_without_config_or_agents_fails(cli, capsys):
    rc = cli.main(["doctor", "--json"])
    assert rc == 1
    checks = _checks(capsys)
    assert checks["config"]["ok"] is True
    assert "enabled: false" in checks["config"]["detail"]
    assert checks["database"]["ok"] is False
    assert checks["builder_agent"]["ok"] is False
    assert "buildq enable" in checks["builder_agent"]["detail"]
    assert checks["reviewer_agent"]["ok"] is False


def test_enable_missing_config_writes_exact_yaml(cli, tmp_path):
    cli.main(["enable", "--no-agents"])
    path = tmp_path / "config" / "build-queue.yaml"
    assert path.read_text() == EXPECTED_YAML
    assert build_hosts.load_config(path).enabled is True


def test_enable_keeps_comment_and_flips_value(cli, tmp_path, capsys):
    path = tmp_path / "config" / "build-queue.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("# my note: keep this\nenabled: false  # off for now\n"
                    "default_host: local\nhosts:\n  local: {kind: local}\n")
    cli.main(["enable", "--no-agents"])
    text = path.read_text()
    assert "# my note: keep this" in text
    assert "enabled: true  # off for now" in text
    assert "enabled: false" not in text
    assert build_hosts.load_config(path).enabled is True


def test_enable_prepends_when_no_enabled_line(cli, tmp_path):
    path = tmp_path / "config" / "build-queue.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("default_host: local\n")
    cli.main(["enable", "--no-agents"])
    assert path.read_text() == "enabled: true\ndefault_host: local\n"


def test_doctor_github_auth_passes_with_github_token(cli, monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    cli.main(["doctor", "--json"])
    assert _checks(capsys)["github_auth"]["ok"] is True
