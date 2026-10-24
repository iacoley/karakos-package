"""Shared fixtures for the step 3.3 build queue tests.

Nothing here reads HOME, binds a port or touches Discord. Every git call runs
with an empty global config; the "remote" is a local bare repository reached
over file://, and the "ssh" is tests/harness/bin/fake-ssh.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
HARNESS_BIN = ROOT / "tests" / "harness" / "bin"
sys.path.insert(0, str(ROOT / "lib"))

REPO = "owner/name"


class FakeRegistry:
    def __init__(self, **roles):
        self.roles = roles or {"builder": ["bld"], "reviewer": ["rev"]}

    def by_role(self, role):
        return [SimpleNamespace(id=i) for i in self.roles.get(role, [])]


def git_env(home):
    env = dict(os.environ)
    env.update(HOME=str(home), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0")
    return env


def git(cwd, *args, home="/nonexistent"):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], cwd=cwd, capture_output=True, text=True,
                          env=git_env(home), check=True).stdout


def make_remote(tmp_path, repo=REPO):
    """A bare repo at <base>/<repo>.git holding `main`; -> base dir."""
    base = tmp_path / "remote"
    bare = base / f"{repo}.git"
    bare.parent.mkdir(parents=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True,
                   env=git_env(tmp_path))
    git(seed, "init", "-q", "-b", "main", home=tmp_path)
    (seed / "README").write_text("seed\n")
    git(seed, "add", "-A", home=tmp_path)
    git(seed, "commit", "-qm", "init", home=tmp_path)
    git(seed, "remote", "add", "origin", str(bare), home=tmp_path)
    git(seed, "push", "-q", "origin", "main", home=tmp_path)
    return base


def bare_path(base, repo=REPO):
    return Path(base) / f"{repo}.git"


def branches(base, repo=REPO):
    out = subprocess.run(["git", "--git-dir", str(bare_path(base, repo)), "branch",
                          "--format=%(refname:short)"], capture_output=True, text=True,
                         env=git_env("/nonexistent")).stdout
    return out.split()


AGENTS_YAML = """version: 2
agents:
  prim:
    name: prim
    role: primary
    system_prompt: agents/prim/SYSTEM_PROMPT.md
    discord: {token_env: DISCORD_BOT_TOKEN_PRIM}
  mon:
    name: mon
    role: monitor
    model: haiku
  bld:
    name: bld
    role: builder
    system_prompt: agents/bld/SYSTEM_PROMPT.md
  rev:
    name: rev
    role: reviewer
    system_prompt: agents/rev/SYSTEM_PROMPT.md
"""


def make_workspace(tmp_path):
    ws = tmp_path / "ws"
    for d in ("config", "data/memory", "data/health", "logs", "bin", "inbox/builder",
              "inbox/reviewer", "agents/bld", "agents/rev", "agents/prim"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    (ws / "config" / "agents.yaml").write_text(AGENTS_YAML)
    for a in ("bld", "rev", "prim"):
        (ws / "agents" / a / "SYSTEM_PROMPT.md").write_text(f"You are {a}.\n")
    for s in ("invoke-builder.sh", "invoke-reviewer.sh"):
        (ws / "bin" / s).symlink_to(ROOT / "bin" / s)
    (ws / "bin" / "lib").symlink_to(ROOT / "bin" / "lib")
    poke = ws / "bin" / "poke.sh"
    poke.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$WORKSPACE_ROOT/poke.log"\n')
    poke.chmod(0o755)
    return ws


def pokes(ws):
    p = Path(ws) / "poke.log"
    return p.read_text().splitlines() if p.exists() else []


def brief(repo=REPO, branch="main", requester="prim", extra="", body="Do the thing."):
    return (f"---\nrepo: {repo}\ntarget_branch: {branch}\nrequester: {requester}\n"
            f"callback_channel: general\n{extra}---\n{body}\n")


def write_script(path, default=None, rules=None):
    Path(path).write_text(json.dumps({"default": default or {}, "rules": rules or []}))


def isolate(monkeypatch, tmp_path, ws=None):
    """Environment for subprocess tests: harness bin first on PATH, empty git config,
    a throwaway HOME, fake ssh as the ssh binary, a fake remote home."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    rhome = tmp_path / "remote-home"
    rhome.mkdir(exist_ok=True)
    logs = tmp_path / "fake-logs"
    logs.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{HARNESS_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("KARAKOS_SSH_BIN", str(HARNESS_BIN / "fake-ssh"))
    monkeypatch.setenv("KARAKOS_FAKE_REMOTE_HOME", str(rhome))
    monkeypatch.setenv("FAKE_CLAUDE_LOG_DIR", str(logs))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(tmp_path / "script.json"))
    monkeypatch.setenv("GH_STUB_REPO", REPO)
    for k in ("KARAKOS_FAKE_SSH_FAIL", "KARAKOS_QUEUE_RUN", "AGENT_SERVER_PORT"):
        monkeypatch.delenv(k, raising=False)
    if ws is not None:
        monkeypatch.setenv("WORKSPACE_ROOT", str(ws))
    write_script(tmp_path / "script.json")
    return SimpleNamespace(home=home, rhome=rhome, logs=logs, script=tmp_path / "script.json")
