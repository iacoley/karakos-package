"""Documentation checks: the docs must describe the code that is in the checkout.

Reads docs/ and README.md from the checkout. No network, no Docker, no HOME.
"""
import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
README = ROOT / "README.md"
ARCH = DOCS / "ARCHITECTURE.md"
UPGRADING = DOCS / "UPGRADING.md"
EXTENDING = DOCS / "EXTENDING.md"
QUICKSTART = DOCS / "QUICKSTART.md"

ALL_DOCS = sorted(DOCS.glob("*.md")) + [README, ROOT / "AGENTS.md", ROOT / "CONTRIBUTING.md"]
# Files that describe a past state on purpose; stale-phrase checks skip them.
HISTORICAL = {"TEST_RESULTS.md", "migration-inventory.md", "production-grade-plan.md"}
CURRENT_DOCS = [p for p in ALL_DOCS if p.name not in HISTORICAL]

FENCE = re.compile(r"^(```|~~~)")


def read(p):
    return Path(p).read_text()


def split_fences(text):
    """-> (prose_lines, blocks) where blocks is [(info, [lines], start_line)]."""
    prose, blocks, cur, info, start = [], [], None, "", 0
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line.strip())
        if m and cur is None:
            cur, info, start = [], line.strip()[3:].strip(), n
        elif m and cur is not None:
            blocks.append((info, cur, start))
            cur = None
        elif cur is not None:
            cur.append(line)
        else:
            prose.append((n, line))
    return prose, blocks


def prose_text(p):
    return "\n".join(l for _, l in split_fences(read(p))[0])


def headings(text):
    out = []
    for n, line in split_fences(text)[0]:
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            out.append((len(m.group(1)), m.group(2), n))
    return out


def slug(title):
    t = title.strip().lower()
    t = re.sub(r"[`*_]", "", t) if False else t.replace("`", "")
    t = re.sub(r"[^\w\- ]", "", t)
    return t.replace(" ", "-")


def anchors(path):
    seen, out = {}, set()
    for _, title, _ in headings(read(path)):
        s = slug(title)
        k = seen.get(s, 0)
        seen[s] = k + 1
        out.add(s if k == 0 else f"{s}-{k}")
    return out


def section(path, title):
    """Prose+fences text under the heading whose title starts with `title`."""
    text = read(path)
    lines = text.splitlines()
    hs = headings(text)
    for i, (lvl, t, n) in enumerate(hs):
        if t.startswith(title):
            end = len(lines)
            for lvl2, _, n2 in hs[i + 1:]:
                if lvl2 <= lvl:
                    end = n2 - 1
                    break
            return "\n".join(lines[n:end])
    raise AssertionError(f"no heading {title!r} in {path}")


def inline_code(text):
    return re.findall(r"`([^`\n]+)`", text)


# ---------------------------------------------------------------- links

LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")


def test_relative_links_and_anchors_resolve():
    problems = []
    for p in ALL_DOCS:
        for n, line in split_fences(read(p))[0]:
            for target in LINK.findall(line):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("//"):
                    continue
                path, _, frag = target.partition("#")
                dest = (p.parent / path).resolve() if path else p
                if not dest.exists():
                    problems.append(f"{p.name}:{n}: {target} (no such file)")
                    continue
                if frag and dest.suffix == ".md" and frag not in anchors(dest):
                    problems.append(f"{p.name}:{n}: {target} (no such anchor)")
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------- paths

CODE_PREFIXES = ("bin/", "lib/", "config/", "system/", "agents/", "mcp/", "skills/")
# Files the install or the operator creates; they are not in the checkout.
RUNTIME_FILES = {
    "config/.env", "config/.env.backup", "config/agents.yaml", "config/agents.json",
    "config/channels.json", "config/monitor.yaml", "config/build-queue.yaml",
    "config/recall-source", "config/jobs.yaml", "config/docker-compose.override.yml",
    "config/docker-compose.yml.pre-2.0", "config/claude-settings.json",
    "config/secrets-allow.txt",
    # illustrations and negative examples in the docs
    "agents/builder/inbox/", "lib/migrate/steps/NN_name.py",
}
# package-backend-contract.md cites dashboard files, relative to dashboard/.
NOT_OURS = {"package-backend-contract.md"}
PATH_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


def test_backticked_code_paths_exist():
    missing = []
    for p in CURRENT_DOCS:
        for span in inline_code(prose_text(p)):
            if not span.startswith(CODE_PREFIXES) or not PATH_RE.match(span):
                continue
            if span in RUNTIME_FILES or span.startswith(("agents/main/", "skills/my-skill/")):
                continue
            if p.name in NOT_OURS:
                continue
            if not (ROOT / span.rstrip("/")).exists():
                missing.append(f"{p.name}: {span}")
    assert not missing, "\n".join(sorted(set(missing)))


RUNTIME_PREFIXES = ("data/", "logs/", "inbox/")
RUNTIME_EXAMPLES: set = set()


def test_runtime_paths_appear_in_data_layout():
    layout = section(ARCH, "Data layout")
    missing = []
    for p in (ARCH, UPGRADING, EXTENDING, QUICKSTART, DOCS / "DISCORD_SETUP.md"):
        for span in inline_code(prose_text(p)):
            if not span.startswith(RUNTIME_PREFIXES) or not PATH_RE.match(span):
                continue
            base = span.rstrip("/").split("/")[-1]
            if base.endswith(".tmp") or span in RUNTIME_EXAMPLES:
                continue
            if base not in layout:
                missing.append(f"{p.name}: {span}")
    assert not missing, "\n".join(sorted(set(missing)))


def test_coverage_table_covers_every_step():
    table = section(ARCH, "Where each 2.0 feature lives")
    rows = [l for l in table.splitlines() if l.startswith("| ") and not l.startswith("| Step")
            and not l.startswith("|---")]
    steps = {r.split("|")[1].strip() for r in rows}
    wanted = ["0.5", "1.0", "1.1a / 1.1b", "1.2", "1.3 / 1.3b", "1.4", "1.5", "1.6", "1.7",
              "2.0", "2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "3.1", "3.2", "3.3",
              "4.1", "4.2 / 4.2b", "4.3", "4.4", "5.0 / 5.4", "5.1 / 5.2", "5.3b", "5.5",
              "6.1", "6.2", "6.3", "6.4", "7.1"]
    assert set(wanted) <= steps, set(wanted) - steps
    for r in rows:
        files = inline_code(r.split("|")[3])
        assert files, r
        for f in files:
            assert (ROOT / f).exists(), f"coverage table names {f}"


# ---------------------------------------------------------------- env vars

ENV_SCAN_DIRS = ["bin", "lib", "mcp", "config", "system", "skills"]
ENV_SCAN_FILES = ["setup.sh", "Dockerfile", "Makefile", "install.sh"]
# Read by the dashboard (dashboard/), not by the Python side.
DASHBOARD_SIDE = {"KARAKOS_COOKIE_SECURE", "SESSION_MAX_AGE_SECONDS",
                  "DASHBOARD_USER", "DASHBOARD_PASSWORD", "SESSION_SECRET"}
# Provided by the shell, Docker or the CLI rather than read by package code.
EXTERNAL = {"PATH", "HOME", "TZ", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CONFIG_DIR", "ANTHROPIC_SMALL_FAST_MODEL", "GITHUB_TOKEN"}


def _code_corpus():
    parts = []
    for d in ENV_SCAN_DIRS:
        for f in (ROOT / d).rglob("*"):
            if f.is_file() and f.suffix in {".py", ".sh", ".yml", ".yaml", ".json", ".template", ""} \
                    and "__pycache__" not in f.parts:
                try:
                    parts.append(f.read_text())
                except UnicodeDecodeError:
                    pass
    for f in ENV_SCAN_FILES:
        if (ROOT / f).is_file():
            parts.append((ROOT / f).read_text())
    parts.append((ROOT / "config/.env.template").read_text())
    return "\n".join(parts)


def _template_vars():
    out = []
    for line in read(ROOT / "config/.env.template").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]+)=", line)
        if m:
            out.append(m.group(1))
    return out


def test_documented_env_vars_are_read_somewhere():
    env_section = section(EXTENDING, "Environment variables")
    names = set()
    for line in env_section.splitlines():
        if line.startswith("| `"):
            names.update(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", line.split("|")[1]))
    assert len(names) > 20, names
    corpus = _code_corpus()
    missing = [n for n in sorted(names)
               if n not in DASHBOARD_SIDE | EXTERNAL and n not in corpus]
    assert not missing, missing


def test_every_template_variable_is_documented():
    doc = section(EXTENDING, "Environment variables")
    # variables the wizard generates and sections elsewhere in the docs explain
    missing = [v for v in _template_vars() if v not in doc and v not in read(QUICKSTART)]
    assert not missing, missing


# ---------------------------------------------------------------- registry

def _registry_module():
    sys.path.insert(0, str(ROOT / "lib"))
    try:
        import registry
    finally:
        sys.path.pop(0)
    return registry


NESTED_OK = {"channels", "token_env", "bot_id_env", "role_id_env", "id", "section", "core", "house_style",
             "enabled", "after_s", "max_rows", "coalesce_ms", "max_lines_per_turn",
             "agents", "agents.yaml", "prompt", "shards", "channels.json", "name", "role"}


def test_registry_keys_documented_exist_and_user_facing_ones_are_documented():
    reg = _registry_module()
    known = set(reg._KNOWN_KEYS)
    text = section(ARCH, "Agents and the registry")
    documented = {t for t in inline_code(text) if re.match(r"^[a-z_][a-z_.]*$", t)}
    allowed = known | set(reg.ROLES) | set(reg.EFFORTS) | NESTED_OK | {"low", "medium", "high"}
    unknown = documented - allowed
    assert not unknown, f"documented but not in the registry schema: {sorted(unknown)}"
    user_facing = {"effort", "env", "prompt", "shards", "token_budget_4h",
                   "token_budget_min_pause_s", "context_budget_tokens", "handoff_on_reset",
                   "reset_mode", "model", "max_turns", "timeout", "work_stealing", "steering",
                   "allowed_tools", "disallowed_tools", "discord", "dashboard_chat",
                   "tool_streaming", "stream_to_channel"}
    assert user_facing <= known, user_facing - known
    everything = prose_text(ARCH) + prose_text(EXTENDING) + prose_text(UPGRADING)
    undocumented = [k for k in sorted(user_facing) if f"`{k}`" not in everything
                    and f"{k}:" not in everything]
    assert not undocumented, undocumented


# ---------------------------------------------------------------- routes and tools

def _server_routes():
    src = read(ROOT / "bin/agent-server.py")
    out = set()
    for m in re.finditer(r'router\.add_(get|post|put|delete)\("([^"]+)"', src):
        out.add((m.group(1).upper(), re.sub(r"\{[^}]*\}", "{}", m.group(2))))
    assert out
    return out


def test_documented_routes_exist():
    routes = _server_routes()
    missing = []
    for p in (ARCH, UPGRADING, EXTENDING, DOCS / "hive-call-log.md"):
        for span in inline_code(prose_text(p)):
            m = re.match(r"^(GET|POST|PUT|DELETE) (/[A-Za-z0-9_/{}<>\-]*)(?:\?\S*)?$", span)
            if not m:
                continue
            path = re.sub(r"[{<][^}>]*[}>]", "{}", m.group(2))
            if (m.group(1), path) not in routes:
                missing.append(f"{p.name}: {span}")
    assert not missing, "\n".join(missing)


def test_documented_mcp_tools_exist():
    tools = set()
    for f in ("tools-server.py", "admin-server.py"):
        tools.update(re.findall(r'"name":\s*"([a-z_]+)"', read(ROOT / "mcp" / f)))
    named = set()
    for p in (ARCH, EXTENDING, UPGRADING):
        named.update(re.findall(r"^- \*\*`([a-z_]+)`\*\*", prose_text(p), re.M))
    assert {"buzz", "hive_call", "memory", "graph"} <= named
    assert not (named - tools), named - tools


# ---------------------------------------------------------------- migrator flags

def _migrate_help():
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    env["HOME"] = str(ROOT)  # never the real home; argparse reads nothing from it
    r = subprocess.run([sys.executable, "-m", "lib.migrate", "--help"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _wrapper_flags():
    return set(re.findall(r"(--[a-z][a-z-]*)\)", read(ROOT / "bin/karakos")))


def test_migrate_flags_match_help():
    help_flags = set(re.findall(r"--[a-z][a-z-]*", _migrate_help())) - {"--help"}
    assert {"--dry-run", "--auto", "--force", "--to-backup", "--backup-to"} <= help_flags
    allowed = help_flags | _wrapper_flags() | {"--help"}
    used = set()
    for info, lines, _ in split_fences(read(UPGRADING))[1]:
        for line in lines:
            if re.search(r"karakos migrate|lib\.migrate|karakos-migrate", line):
                used.update(re.findall(r"--[a-z][a-z-]*", line))
    assert used, "UPGRADING shows no migrator command"
    assert not (used - allowed), f"not in --help or the wrapper: {sorted(used - allowed)}"
    text = read(UPGRADING)
    undocumented = sorted(f for f in help_flags if f"`{f}" not in text)
    assert not undocumented, undocumented


# ---------------------------------------------------------------- shell blocks

def test_bash_blocks_parse():
    bad = []
    for p in ALL_DOCS:
        for info, lines, start in split_fences(read(p))[1]:
            if info.split()[:1] not in (["bash"], ["sh"]):
                continue
            r = subprocess.run(["bash", "-n"], input="\n".join(lines) + "\n",
                               capture_output=True, text=True)
            if r.returncode != 0:
                bad.append(f"{p.name}:{start}: {r.stderr.strip()}")
    assert not bad, "\n".join(bad)


# ---------------------------------------------------------------- stale phrases

STALE = ["no migration command", "memory-maintenance", "episodes and facts"]
ALLOWED_IDS = {"main", "helper", "builder", "reviewer", "monitor", "a", "b", "example"}


def test_no_stale_phrases():
    bad = []
    for p in CURRENT_DOCS:
        text = read(p)
        for phrase in STALE:
            if phrase in text.lower():
                bad.append(f"{p.name}: {phrase!r}")
        if "discord-dead-letter" in text and p.name not in (
                "UPGRADING.md", "ARCHITECTURE.md", "DISCORD_SETUP.md"):
            bad.append(f"{p.name}: discord-dead-letter outside the migrate/outbox sections")
    assert not bad, "\n".join(bad)


def test_bindsto_only_in_the_sentence_that_forbids_it():
    paras = re.split(r"\n\s*\n", read(EXTENDING))
    hits = [p for p in paras if "BindsTo=" in p]
    assert hits
    for p in hits:
        assert "never `BindsTo=`" in p, p[:120]
    for p in CURRENT_DOCS:
        if p != EXTENDING:
            assert "BindsTo=" not in read(p), p.name


def test_examples_use_neutral_agent_ids():
    bad = []
    for p in CURRENT_DOCS:
        if p.name in NOT_OURS:      # its example ids are neutral placeholders of the dashboard contract
            continue
        for info, lines, start in split_fences(read(p))[1]:
            in_agents = False
            for off, line in enumerate(lines):
                if re.match(r"^agents:\s*$", line):
                    in_agents = True
                    continue
                if in_agents and line and not line.startswith(" "):
                    in_agents = False
                m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line) if in_agents else None
                if m and m.group(1) not in ALLOWED_IDS:
                    bad.append(f"{p.name}:{start + off + 1}: agent id {m.group(1)!r}")
                for u in re.findall(r"/agents/([A-Za-z0-9_-]+)/", line):
                    if u not in ALLOWED_IDS:
                        bad.append(f"{p.name}:{start + off + 1}: agent id {u!r} in URL")
    assert not bad, "\n".join(bad)


# ---------------------------------------------------------------- smoke blocks

def _smoke_lines():
    lines = read(QUICKSTART).splitlines()
    out = []
    for i, line in enumerate(lines):
        if line.strip() == "<!-- smoke -->":
            assert lines[i + 1].startswith("```"), "a smoke tag must precede a fenced block"
            j = i + 2
            while not lines[j].startswith("```"):
                if lines[j].strip():
                    out.append(lines[j])
                j += 1
    return out


def test_smoke_blocks_take_ports_and_tokens_from_env():
    lines = _smoke_lines()
    assert lines
    assert any("set -a; . config/.env; set +a" in l for l in lines)
    for l in lines:
        assert not re.search(r"\b(3000|18791)\b", l), l
        if "docker compose" in l:
            assert "-f config/docker-compose.yml --env-file config/.env" in l, l


def test_smoke_lines_are_in_the_smoke_script():
    script = ROOT / "tests/smoke/fresh_install.sh"
    assert script.exists(), "tests/smoke/fresh_install.sh is the smoke script the docs name"
    body = script.read_text()
    missing = [l for l in _smoke_lines() if l not in body]
    assert not missing, missing


# ---------------------------------------------------------------- the token residual

def test_agent_server_token_residual_is_stated():
    for p, sec in ((ARCH, "Credentials"), (UPGRADING, "The env allowlist")):
        text = section(p, sec)
        assert "AGENT_SERVER_TOKEN" in text, p.name
        assert "residual" in text or "still passed" in text, p.name
    assert "AGENT_SERVER_TOKEN" in read(EXTENDING)


# ---------------------------------------------------------------- coupling

def test_docs_are_free_of_household_coupling():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    r = subprocess.run(["bash", str(ROOT / "system/check-coupling.sh")], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.stdout.strip().endswith("coupling: clean"), r.stdout + r.stderr
