"""Every reader of the agent registry: shell CLIs match the old 1.x output, no
live reads of agents.json remain, and boot stays out of 1.x data."""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import registry  # noqa: E402

REG = ROOT / "lib" / "registry.py"

LEGACY = {"agents": {
    "boss": {"model": "opus", "system_prompt": "agents/boss/SYSTEM_PROMPT.md",
             "discord_bot_token_env": "T_BOSS", "discord_bot_id_env": "I_BOSS"},
    "relay": {"model": "haiku"},
    "builder": {"system_prompt": "agents/builder/SYSTEM_PROMPT.md"},
    "reviewer": {"system_prompt": "agents/reviewer/SYSTEM_PROMPT.md",
                 "discord_bot_token_env": "T_REV"},
}}


@pytest.fixture
def migrated(tmp_path):
    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "agents.json").write_text(json.dumps(LEGACY))
    assert registry.migrate_legacy(ws) is True
    return ws


def cli(ws, *args):
    r = subprocess.run([sys.executable, str(REG), "--workspace", str(ws), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


# -- old per-script logic, evaluated on the legacy dict (table-driven) ---------

def old_find(role_word):          # invoke-builder.sh / invoke-reviewer.sh
    for name, info in LEGACY["agents"].items():
        if role_word in info.get("system_prompt", "") or role_word in name:
            return name


def old_first_token_env():        # discord-notify.sh
    for info in LEGACY["agents"].values():
        if info.get("discord_bot_token_env"):
            return info["discord_bot_token_env"]


def old_first_model():            # upgrade-claude-cli.sh
    for info in LEGACY["agents"].values():
        if info.get("model"):
            return info["model"]


def old_primary():                # bin/kara, entrypoint, scheduler
    return next(iter(LEGACY["agents"]))


CASES = [
    ("builder", ("role", "builder"), lambda: old_find("builder")),
    ("reviewer", ("role", "reviewer"), lambda: old_find("reviewer")),
    ("primary", ("role", "primary"), old_primary),
    ("ids", ("ids",), lambda: "\n".join(LEGACY["agents"])),
]


@pytest.mark.parametrize("name,args,old", CASES, ids=[c[0] for c in CASES])
def test_cli_matches_old_lookup(migrated, name, args, old):
    assert cli(migrated, *args).splitlines()[0] == old().splitlines()[0]
    if name == "ids":
        assert cli(migrated, *args) == old()


def test_legacy_cli_matches_old_dict(migrated):
    got = json.loads(cli(migrated, "legacy"))
    for entry in got["agents"].values():
        entry.pop("prompt", None)       # flags added by the 1.3b migration
        entry.pop("handoff_on_reset", None)   # effective value, always written (2.6)
    assert got == LEGACY
    view = json.loads(cli(migrated, "legacy"))["agents"]
    token = next((i["discord_bot_token_env"] for i in view.values()
                  if i.get("discord_bot_token_env")), None)
    assert token == old_first_token_env()
    assert next(i["model"] for i in view.values() if i.get("model")) == old_first_model()


def _script_env(ws, **extra):
    return {**os.environ, "WORKSPACE_ROOT": str(ws), **extra}


def test_invoke_builder_and_reviewer_resolve_agent(migrated, tmp_path):
    for role in ("builder", "reviewer"):
        # The block is plain bash; run just it with the script's own path context.
        text = (ROOT / "bin" / f"invoke-{role}.sh").read_text()
        start = text.index(f"# Determine {role} agent")
        end = text.index(f'if [[ -z "${role.upper()}_AGENT" ]]')
        block = text[start:end].replace(
            'REGISTRY_PY="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib" && pwd)/registry.py"',
            f'REGISTRY_PY="{REG}"')
        r = subprocess.run(["bash", "-c", block + f'\necho "${role.upper()}_AGENT"'],
                           env=_script_env(migrated), capture_output=True, text=True)
        assert r.stdout.strip() == role, r.stderr


def test_poke_defaults_to_the_primary(migrated):
    r = subprocess.run(["bash", str(ROOT / "bin" / "poke.sh"), "--silent", "hi"],
                       env=_script_env(migrated, AGENT_SERVER_PORT="1"),
                       capture_output=True, text=True, timeout=30)
    # Resolves the primary from agents.yaml, then fails at the (refused) POST.
    assert "Agents config not found" not in r.stderr
    spooled = list((migrated / "data").rglob("*poke*.json"))
    assert spooled and json.loads(spooled[0].read_text())["agent"] == "boss"


def test_scripts_without_registry_error_cleanly(tmp_path):
    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    r = subprocess.run([sys.executable, str(REG), "--workspace", str(ws), "ids"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "not found" in r.stderr


# -- grep test ------------------------------------------------------------------

def test_no_live_reads_of_agents_json():
    """bin/, mcp/ and setup.sh never name agents.json (only the migrator and docs
    do), so there is no code path that could read it."""
    hits = []
    for path in [*(ROOT / "bin").rglob("*"), *(ROOT / "mcp").rglob("*"), ROOT / "setup.sh"]:
        if not path.is_file() or path.suffix == ".pre-2.0" or "__pycache__" in path.parts:
            continue
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(lines, 1):
            if re.search(r"agents\.json", line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert hits == []


# -- boot --------------------------------------------------------------------

def _fake_supervisord(ws):
    fake = ws / "fake-bin"
    fake.mkdir()
    sup = fake / "supervisord"
    sup.write_text("#!/usr/bin/env bash\nexit 0\n")
    sup.chmod(sup.stat().st_mode | stat.S_IEXEC)
    return fake


def _boot(ws):
    for n in ("data", "logs", "inbox", "bin"):
        (ws / n).mkdir(exist_ok=True)
    shutil.copy(ROOT / "bin" / "hooks-sync.py", ws / "bin" / "hooks-sync.py")
    (ws / "lib").mkdir(exist_ok=True)
    shutil.copy(ROOT / "lib" / "paths.py", ws / "lib" / "paths.py")
    env = {**os.environ, "WORKSPACE_ROOT": str(ws), "DASHBOARD_PORT": "3000",
           "AGENT_SERVER_TOKEN": "t", "PATH": f"{_fake_supervisord(ws)}:{os.environ['PATH']}"}
    env.pop("KARAKOS_SKIP_STAMP_CHECK", None)
    return subprocess.run(["bash", str(ROOT / "bin" / "entrypoint.sh")], env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(os.geteuid() == 0, reason="volume guard")
def test_stamped_boot_creates_inboxes_from_yaml_and_syncs_hooks(tmp_workspace):
    from lib.migrate.guard import write_stamp
    write_stamp(tmp_workspace / "data")
    (tmp_workspace / "config" / "claude-settings.json").write_text("{}")
    r = _boot(tmp_workspace)
    assert r.returncode == 0, r.stderr
    assert (tmp_workspace / "inbox" / "test-agent").is_dir()
    assert (tmp_workspace / "agents" / "relay" / "journal").is_dir()
    assert "hooks" in json.loads((tmp_workspace / "config" / "claude-settings.json").read_text())


@pytest.mark.skipif(os.geteuid() == 0, reason="volume guard")
def test_stamped_boot_without_registry_is_an_error(tmp_workspace):
    from lib.migrate.guard import write_stamp
    write_stamp(tmp_workspace / "data")
    (tmp_workspace / "config" / "agents.yaml").unlink()
    r = _boot(tmp_workspace)
    assert r.returncode == 1 and "agents.yaml" in r.stderr
