"""Procedure skills (#235 build A): index, read and validate skills/<name>/.

Two kinds of skill live under skills/:

- tool skills: tools.json + scripts/, loaded by mcp/tools-server.py as MCP tools;
- procedure skills: a SKILL.md with `name` and `description` frontmatter and no
  tools.json. Instructions only; an agent reads them with the `skill` tool.

Nothing here writes to disk. `validate` runs `--test-tool` for tool skills in a
subprocess with a timeout.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TEST_TIMEOUT_S = 20
MAX_READ = 64_000
_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def parse_frontmatter(text: str):
    """`{key: value}` from a leading ---/--- block, or None when absent."""
    m = _FM.match(text)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _dir(skills_dir, name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError(f"invalid skill name {name!r}: lowercase letters, digits, '-' and '_'")
    d = Path(skills_dir) / name
    if not d.is_dir():
        raise LookupError(f"no skill named {name!r}")
    return d


def scan(skills_dir) -> list:
    """One entry per skill directory that has a tools.json or a SKILL.md."""
    out = []
    root = Path(skills_dir)
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        md, tj = d / "SKILL.md", d / "tools.json"
        if not md.exists() and not tj.exists():
            continue
        fm = parse_frontmatter(md.read_text(errors="replace")) if md.exists() else None
        desc = (fm or {}).get("description", "")
        kind = "tool" if tj.exists() else "procedure"
        if not desc and tj.exists():
            try:
                desc = json.loads(tj.read_text()).get("description", "")
            except (OSError, ValueError):
                pass
        out.append({"name": d.name, "kind": kind, "description": desc,
                    "has_skill_md": md.exists(), "valid": bool(fm and fm.get("name") and desc) or kind == "tool"})
    return out


def read(skills_dir, name) -> dict:
    d = _dir(skills_dir, name)
    md = d / "SKILL.md"
    if not md.exists():
        raise LookupError(f"skill {name!r} has no SKILL.md")
    text = md.read_text(errors="replace")
    return {"name": name, "content": text[:MAX_READ], "truncated": len(text) > MAX_READ}


def _script_for(d: Path, tool: str):
    for base in (tool, "main"):
        for ext in (".py", ".sh"):
            if (d / "scripts" / f"{base}{ext}").exists():
                return base + ext
    return None


def _check_tools_json(d: Path, problems: list) -> list:
    try:
        data = json.loads((d / "tools.json").read_text())
    except (OSError, ValueError) as e:
        problems.append(f"tools.json unreadable: {e}")
        return []
    for key in ("skill_name", "version", "description", "tools"):
        if key not in data:
            problems.append(f"tools.json missing '{key}'")
    if data.get("skill_name") not in (None, d.name):
        problems.append(f"tools.json skill_name {data.get('skill_name')!r} != directory {d.name!r}")
    names = []
    for t in data.get("tools") or []:
        n = t.get("name") if isinstance(t, dict) else None
        if not n:
            problems.append("a tool has no name")
            continue
        names.append(n)
        if not t.get("description"):
            problems.append(f"tool {n}: no description")
        if (t.get("inputSchema") or {}).get("type") != "object":
            problems.append(f"tool {n}: inputSchema.type must be 'object'")
        if not _script_for(d, n):
            problems.append(f"tool {n}: no scripts/{n}.py or scripts/{n}.sh (or main.*)")
    return names


def _smoke(server: Path, workspace: Path, tool: str, problems: list):
    try:
        r = subprocess.run([sys.executable, str(server), "--test-tool", tool, "{}"],
                           capture_output=True, text=True, timeout=TEST_TIMEOUT_S,
                           env={**__import__("os").environ, "WORKSPACE_ROOT": str(workspace)},
                           cwd=str(workspace))
    except subprocess.TimeoutExpired:
        problems.append(f"tool {tool}: --test-tool timed out after {TEST_TIMEOUT_S}s")
        return
    if r.returncode != 0:
        problems.append(f"tool {tool}: --test-tool exited {r.returncode}: {(r.stderr or r.stdout).strip()[-200:]}")
        return
    try:
        res = json.loads(r.stdout)
    except ValueError:
        problems.append(f"tool {tool}: --test-tool printed no JSON")
        return
    err = str(res.get("error", "")) if isinstance(res, dict) else ""
    if "No implementation script" in err or "timed out" in err or "Traceback" in err:
        problems.append(f"tool {tool}: {err[:200]}")


def validate(skills_dir, name, server=None, workspace=None, run_tools=True) -> dict:
    d = _dir(skills_dir, name)
    problems = []
    md, tj = d / "SKILL.md", d / "tools.json"
    if not md.exists() and not tj.exists():
        problems.append("neither SKILL.md nor tools.json")
    if md.exists():
        fm = parse_frontmatter(md.read_text(errors="replace"))
        if fm is None:
            problems.append("SKILL.md has no frontmatter block")
        else:
            if fm.get("name") != name:
                problems.append(f"frontmatter name {fm.get('name')!r} != directory {name!r}")
            if not fm.get("description"):
                problems.append("frontmatter has no description")
    tools = _check_tools_json(d, problems) if tj.exists() else []
    if tools and run_tools and not problems and server:
        for t in tools:
            _smoke(Path(server), Path(workspace or Path(skills_dir).parent), t, problems)
    return {"name": name, "kind": "tool" if tj.exists() else "procedure",
            "ok": not problems, "problems": problems, "tools": tools}
