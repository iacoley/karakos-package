"""Native-mode guard: code must resolve the install root, not hardcode /workspace."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = ("bin", "lib", "mcp", "system")
# Container-only files: the Docker layout is their subject.
ALLOWED = {
    "bin/entrypoint.sh", "bin/karakos",
    "lib/migrate/detect.py", "lib/migrate/compose.py",
}
LITERAL = re.compile(r'"/workspace"|:-/workspace\b')
# Everything resolves the root through lib/paths.py (python) or bin/lib/paths.sh (shell).
# Hooks must stay import-free, so they read the env var themselves.
INLINE_OK = {"lib/paths.py", "bin/lib/paths.sh"}
INLINE = re.compile(r'environ(?:\.get)?\(?\[?"WORKSPACE_ROOT"|\$\{WORKSPACE_ROOT:-')


def _files():
    out = subprocess.check_output(["git", "ls-files", *SCAN], cwd=ROOT, text=True)
    for rel in out.split():
        if rel in ALLOWED or "/templates/" in rel or "/steps/" in rel:
            continue
        if rel.endswith((".py", ".sh")):
            yield rel


def test_no_hardcoded_workspace_default():
    bad = [r for r in _files() if LITERAL.search((ROOT / r).read_text(errors="ignore"))]
    assert not bad, f"hardcoded /workspace default (use KARAKOS_HOME/WORKSPACE_ROOT resolver): {bad}"


def test_root_comes_from_the_one_resolver():
    bad = []
    for r in _files():
        if r in INLINE_OK or r.startswith("system/") or r.endswith("/hookio.py"):
            continue
        if INLINE.search((ROOT / r).read_text(errors="ignore")):
            bad.append(r)
    assert not bad, f"use lib/paths.py workspace_root() or source bin/lib/paths.sh: {bad}"


def test_resolver_order(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "lib"))
    import paths
    monkeypatch.delenv("KARAKOS_HOME", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    assert paths.workspace_root() == str(ROOT)
    monkeypatch.setenv("WORKSPACE_ROOT", "/a")
    assert paths.workspace_root() == "/a"
    monkeypatch.setenv("KARAKOS_HOME", "/b")
    assert paths.workspace_root() == "/b"


def test_python_resolver_honours_karakos_home(tmp_path):
    env = {"KARAKOS_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    code = ("import runpy,sys;sys.argv=['x'];"
            "import importlib.util as u;"
            "s=u.spec_from_file_location('m','bin/purge-data.py');m=u.module_from_spec(s);s.loader.exec_module(m);"
            "print(m.WORKSPACE)")
    out = subprocess.check_output(["python3", "-c", code], cwd=ROOT, env=env, text=True)
    assert out.strip() == str(tmp_path)
