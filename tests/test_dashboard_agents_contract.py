"""
Tests for the shape contract on /api/agents in the dashboard.

The case this exists for: `dashboard/app/api/agents/route.ts` reshapes the
agent-server's `/status` dict into `{ agents: [...] }` — an ARRAY of
`{name, state, ...}` objects. `dashboard/app/chat/page.tsx` typed that as
`Record<string, {state}>` and read it with `Object.keys(agentData.agents)`.

`Object.keys()` on an array returns its indices: "0", "1", "2". So the agent
dropdown populated with the strings "0", "1", "2" and was fully selectable —
it looked right. Picking one then sent `agent: "0"` to the backend and built
URLs like `/api/agents/0/reload`. Silently wrong beats visibly broken only for
whoever is not debugging it.

TypeScript could not catch this: the interface asserted a shape the route never
returns, and an assertion is not a check. These tests read the source the way
TestNextjsRouteExports does — the route is the authority on the shape, and the
consumers have to agree with it.
"""

import re
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
DASHBOARD_APP = PACKAGE_ROOT / "dashboard" / "app"
AGENTS_ROUTE = DASHBOARD_APP / "api" / "agents" / "route.ts"


# Endpoints whose `agents` field is an ARRAY. Both proxy an agent-server route
# that builds a list: /api/agents from /status, /api/agents/config from /agents.
#
# Deliberately NOT a match on `.agents` anywhere in the file. The agent-server's
# /health returns `agents` as a dict keyed by name, and dashboard/app/page.tsx
# consumes exactly that -- its `Record<string, ...>` typing is correct, and a
# check that flagged it would be teaching the wrong lesson about a right file.
# The shape follows the endpoint, so the endpoint is what selects the file.
ARRAY_ENDPOINTS = ("/api/agents", "/api/agents/config")


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def _strip_comments(text):
    """Blank out comments, preserving line numbers and offsets.

    Without this the checks below read prose as code. The fix for the original
    bug landed with a comment explaining it -- containing the literal string
    `Object.keys(agentData.agents)` -- so the first run of
    test_no_consumer_calls_object_keys_on_agents failed against the very file
    that had just been fixed. Same trap as the AST note in
    test_dead_letter.py, one language over.

    Replacement is space-for-character rather than deletion so the reported
    line numbers still point at the real source.
    """
    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    return _LINE_COMMENT.sub(blank, _BLOCK_COMMENT.sub(blank, text))


def _consumers():
    """Dashboard source files that poll an endpoint returning an agents array."""
    pattern = re.compile(
        r"""["'`](%s)["'`]""" % "|".join(re.escape(e) for e in ARRAY_ENDPOINTS)
    )
    found = []
    for path in DASHBOARD_APP.rglob("*.tsx"):
        text = _strip_comments(path.read_text())
        if pattern.search(text):
            found.append((path, text))
    return found


def test_the_consumer_scan_finds_something():
    """A scan that silently matches nothing would make every check below pass
    by vacuum. Both known consumers are real pages: chat and settings."""
    names = {path.name for path, _ in _consumers()}
    assert names, f"no dashboard file polls any of {ARRAY_ENDPOINTS}"


def test_route_still_returns_an_array():
    """The premise of every check below. If the route is ever changed to return
    a dict keyed by name, these tests are asserting the wrong contract and
    should fail loudly rather than quietly pass."""
    assert AGENTS_ROUTE.exists(), f"{AGENTS_ROUTE} is missing"
    src = AGENTS_ROUTE.read_text()

    assert re.search(r"const\s+agents\s*=\s*Object\.entries\([^)]*\)\s*\.map\(", src), (
        "/api/agents no longer builds `agents` with Object.entries(...).map(...) "
        "— confirm whether it still returns an array and update these tests"
    )
    assert re.search(r"NextResponse\.json\(\s*\{\s*agents\s*\}", src), (
        "/api/agents no longer returns { agents } — the shape contract moved"
    )


def test_no_consumer_calls_object_keys_on_agents():
    """The bug itself. On an array this yields "0", "1", "2" and the dropdown
    silently offers indices as if they were agent names."""
    offenders = []
    for path, text in _consumers():
        for match in re.finditer(r"Object\.keys\(\s*([\w?.]*\.)?agents\s*\)", text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{line}")

    assert not offenders, (
        "Object.keys() called on the agents array — this yields numeric "
        f"indices, not agent names: {', '.join(offenders)}. "
        "Use `.map(a => a.name)`."
    )


def test_settings_page_only_renders_fields_the_endpoint_returns():
    """The root defect, and the one worth keeping a test on.

    The settings page rendered cfg.model, cfg.max_turns and cfg.timeout while
    polling /api/agents -- which reshapes /status and carries none of them. The
    page was not merely mislabelled, it was reading three fields that did not
    exist, and rendered `undefined` for each. Nothing in the type system
    objects: the interface asserted they were there.

    So: whatever the settings page destructures off an agent must appear in the
    dict that agent-server's handle_agents() actually builds.
    """
    settings = DASHBOARD_APP / "settings" / "page.tsx"
    src = _strip_comments(settings.read_text())

    assert "/api/agents/config" in src, (
        "the settings page no longer polls /api/agents/config — if it moved "
        "back to /api/agents it is reading runtime state as if it were config"
    )

    server = (PACKAGE_ROOT / "bin" / "agent-server.py").read_text()
    handler = re.search(
        r"async def handle_agents\(request\):.*?(?=\nasync def |\ndef )",
        server,
        re.DOTALL,
    )
    assert handler, "handle_agents() not found in bin/agent-server.py"
    served = set(re.findall(r'"(\w+)":', handler.group(0)))

    accessed = set(re.findall(r"\bcfg\.(\w+)", src))
    assert accessed, "the settings page destructures nothing off an agent"

    missing = sorted(accessed - served)
    assert not missing, (
        f"the settings page renders {missing}, which agent-server's "
        f"/agents endpoint does not return (it serves {sorted(served)}). "
        "Every one of those renders as undefined."
    )


def test_no_consumer_types_agents_as_a_dict():
    """The typing that made the bug survive review: an interface asserting a
    dict shape the route never returns."""
    offenders = []
    for path, text in _consumers():
        for match in re.finditer(r"agents\s*:\s*Record\s*<", text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{line}")

    assert not offenders, (
        "`agents` is typed as a Record but /api/agents returns an array: "
        f"{', '.join(offenders)}. TypeScript will not catch this — the "
        "annotation is an assertion about untyped JSON, not a check of it."
    )
