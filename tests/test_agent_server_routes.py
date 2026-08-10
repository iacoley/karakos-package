"""Tests for bin/agent-server.py's routing surface.

The full server is heavy (event loop + sqlite + subprocesses) so we avoid
booting it and read the route table out of the AST instead.

These tests used to be `assert "<literal>" in src` greps. Every assertion they
made was true, so they looked like coverage, but a sabotage pass put five real
defects straight through them and failed one *correct* reformatting:

  - deleting `app.router.add_get("/health", handle_health)` and leaving the
    path in a comment: GREEN, because the grep only wanted the string;
  - dropping the `_AGENT_NAME_RE.match` call out of the register handler while
    the constant and the error message survived elsewhere: GREEN;
  - pointing a route at a handler that does not exist: GREEN, and the server
    would NameError at boot;
  - deleting the `/usage` and `/ask/{ask_id}/answer` routes: GREEN, they were
    never in the list;
  - splitting the register line over two lines the way a formatter would: RED,
    against code that was completely correct.

The rule underneath all six: grepping source text tests the text, not the
behaviour, and a comment is source text too. Read the AST, and assert on the
route table the code actually builds.
"""

import ast
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT

AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
_SOURCE = AGENT_SERVER.read_text()
_TREE = ast.parse(_SOURCE)

# aiohttp's UrlDispatcher methods we care about: add_get, add_post, add_route...
_ADD_PREFIX = "add_"


def _registered_routes():
    """Every `<something>.router.add_<method>(path, handler)` the module makes.

    Returns [(method, path, handler_name)]. Only literal paths and bare-name
    handlers are collected; anything computed is reported separately by
    ``_dynamic_route_calls`` so it cannot hide from these tests silently.
    """
    routes = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or not func.attr.startswith(_ADD_PREFIX):
            continue
        owner = func.value
        if not (isinstance(owner, ast.Attribute) and owner.attr == "router"):
            continue
        method = func.attr[len(_ADD_PREFIX):]
        args = node.args
        # add_route(method, path, handler) vs add_get(path, handler)
        if method == "route" and len(args) >= 3:
            method_node, path_node, handler_node = args[0], args[1], args[2]
            method = (
                method_node.value.lower()
                if isinstance(method_node, ast.Constant)
                else "?"
            )
        elif len(args) >= 2:
            path_node, handler_node = args[0], args[1]
        else:
            continue
        if not isinstance(path_node, ast.Constant) or not isinstance(
            path_node.value, str
        ):
            continue
        handler = handler_node.id if isinstance(handler_node, ast.Name) else None
        routes.append((method, path_node.value, handler))
    return routes


def _dynamic_route_calls():
    """Line numbers of route registrations whose path is not a string literal."""

    def _is_str_literal(node):
        return isinstance(node, ast.Constant) and isinstance(node.value, str)

    dynamic = []
    for node in ast.walk(_TREE):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith(_ADD_PREFIX)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "router"
        ):
            continue
        args = node.args
        # add_route(method, path, handler) puts the path second; add_get/add_post
        # and friends put it first.
        path_index = 1 if node.func.attr == "add_route" else 0
        if len(args) <= path_index or not _is_str_literal(args[path_index]):
            dynamic.append(node.lineno)
    return dynamic


def _top_level_functions():
    """{name: node} for module-level def / async def."""
    out = {}
    for node in _TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node)
    return out


def _function(name):
    defs = _top_level_functions().get(name)
    assert defs, f"{name} is not defined at module level in agent-server.py"
    return defs[0]


def _calls_in(node):
    """Names of things called inside a function: `f()`, `obj.f()`, `await f()`."""
    plain, attrs = set(), set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                plain.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                attrs.add(sub.func.attr)
                if isinstance(sub.func.value, ast.Name):
                    attrs.add(f"{sub.func.value.id}.{sub.func.attr}")
    return plain, attrs


# The routes the rest of the system depends on. Each entry is the contract a
# caller outside this file relies on, so deleting one has to fail here.
#   bin/create-agent.sh          -> POST /agents/{name}/register
#   bin/relay.py, discord relay  -> POST /message, /agents/{name}/{reset,reload}
#   bin/health-monitor.py, /sys  -> GET  /health, /agents
#   bin/cost-report.sh, /sys     -> POST /cost, GET /cost/{agent}, /usage
#   the ask surface (#101/#135)  -> POST /ask, GET /ask/{id}, POST /ask/{id}/answer
#   the relay's control buttons  -> POST /agents/{name}/{interrupt,kill,flush}
REQUIRED_ROUTES = {
    ("post", "/message"): "handle_message",
    ("get", "/health"): "handle_health",
    ("get", "/agents"): "handle_agents",
    ("post", "/agents/{name}/reset"): "handle_agent_reset",
    ("post", "/agents/{name}/reload"): "handle_agent_reload",
    ("post", "/agents/{name}/register"): "handle_agent_register",
    ("post", "/agents/{name}/interrupt"): "handle_agent_interrupt",
    ("post", "/agents/{name}/kill"): "handle_agent_kill",
    ("post", "/agents/{name}/flush"): "handle_agent_flush",
    ("post", "/cost"): "handle_cost",
    ("get", "/cost/{agent}"): "handle_cost_get",
    ("get", "/usage"): "handle_usage",
    ("post", "/ask"): "handle_ask_create",
    ("get", "/ask/{ask_id}"): "handle_ask_status",
    ("post", "/ask/{ask_id}/answer"): "handle_ask_answer",
}


def test_agent_server_parses():
    ast.parse(_SOURCE)


@pytest.mark.parametrize(
    "method,path,handler",
    [(m, p, h) for (m, p), h in sorted(REQUIRED_ROUTES.items())],
)
def test_required_route_is_registered(method, path, handler):
    """Each route is registered, with the handler its callers expect.

    Asserts on the parsed route table, so a registration that has been deleted
    or commented out fails even though the path string is still in the file.
    """
    routes = _registered_routes()
    matches = [r for r in routes if r[0] == method and r[1] == path]
    assert matches, (
        f"{method.upper()} {path} is not registered. Registered: "
        + ", ".join(sorted(f"{m.upper()} {p}" for m, p, _ in routes))
    )
    assert matches[0][2] == handler, (
        f"{method.upper()} {path} is wired to {matches[0][2]}, expected {handler}"
    )


def test_every_registered_handler_is_defined():
    """A route pointing at a name that does not exist NameErrors at boot.

    The old grep could not see this: the route line looked perfectly normal.
    """
    defined = _top_level_functions()
    missing = [
        (m, p, h)
        for m, p, h in _registered_routes()
        if h is not None and h not in defined
    ]
    assert not missing, "routes wired to undefined handlers: " + ", ".join(
        f"{m.upper()} {p} -> {h}" for m, p, h in missing
    )


def test_registered_handlers_are_coroutines():
    """aiohttp handlers must be `async def`; a plain def returns a coroutine-less
    object and every request to it 500s."""
    defined = _top_level_functions()
    sync = [
        (p, h)
        for _, p, h in _registered_routes()
        if h in defined and not isinstance(defined[h][0], ast.AsyncFunctionDef)
    ]
    assert not sync, "handlers that are not async def: " + ", ".join(
        f"{p} -> {h}" for p, h in sync
    )


def test_no_duplicate_handler_definitions():
    """Two `async def handle_x` in one module: the later silently wins.

    This is not hypothetical — merging wave 3 turned up two `on_interaction`
    definitions in one class, which would have taken the slash commands dead
    with no traceback. A duplicate here has the same shape and no symptom.
    """
    dupes = {
        name: [d.lineno for d in defs]
        for name, defs in _top_level_functions().items()
        if len(defs) > 1
    }
    assert not dupes, f"duplicate top-level definitions (the later one wins): {dupes}"


def test_no_duplicate_route_registrations():
    """Registering the same method+path twice raises at startup in aiohttp."""
    seen, dupes = set(), []
    for method, path, _ in _registered_routes():
        if (method, path) in seen:
            dupes.append(f"{method.upper()} {path}")
        seen.add((method, path))
    assert not dupes, f"routes registered more than once: {dupes}"


def test_route_paths_are_all_literals():
    """If a registration ever computes its path, these tests go blind to it.

    Fail loudly rather than quietly checking a shrinking subset — that is the
    failure mode that let the old greps look like coverage.
    """
    dynamic = _dynamic_route_calls()
    assert not dynamic, (
        "route registration with a non-literal path at line(s) "
        f"{dynamic} — extend _registered_routes() to cover it"
    )


def test_register_handler_validates_name():
    """Hot-register takes a path-segment name, so it must run it through
    _AGENT_NAME_RE before touching disk.

    Scoped to the handler's own AST: the old test accepted the constant being
    defined anywhere in the file, so deleting the call site left it green.
    """
    node = _function("handle_agent_register")
    _, attrs = _calls_in(node)
    assert "_AGENT_NAME_RE.match" in attrs, (
        "handle_agent_register does not call _AGENT_NAME_RE.match — an agent "
        "name from the URL reaches disk unvalidated"
    )
    messages = {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "Invalid agent name" in messages


def test_agent_name_regex_rejects_path_traversal():
    """The pattern itself, applied — not just asserted to exist."""
    import re

    pattern = None
    for node in ast.walk(_TREE):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_AGENT_NAME_RE"
                for t in node.targets
            )
            and isinstance(node.value, ast.Call)
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
        ):
            pattern = node.value.args[0].value
    assert pattern, "_AGENT_NAME_RE is not assigned a literal pattern"
    rx = re.compile(pattern)
    for good in ["amos", "argus", "agent-1", "agent_1", "A9"]:
        assert rx.match(good), f"{good!r} should be a legal agent name"
    for bad in ["../etc", "a/b", "a b", "", ".", "a.b", "a\nb", "a\x00b"]:
        assert not rx.match(bad), f"{bad!r} must be rejected"


def test_register_handler_reloads_config_and_spawns():
    """The handler must re-read agents.json and start the subprocess, or the
    hot-register is a no-op that reports success."""
    node = _function("handle_agent_register")
    plain, _ = _calls_in(node)
    assert "load_config" in plain, "handle_agent_register never calls load_config()"
    assert "start_agent_subprocess" in plain, (
        "handle_agent_register never calls start_agent_subprocess()"
    )


def test_create_agent_script_targets_register():
    """The script and the server must agree on the endpoint path.

    Comments are stripped first: a commented-out curl would otherwise satisfy
    this, which is the same defect as greping the server for a route string.
    """
    create_agent = PACKAGE_ROOT / "bin" / "create-agent.sh"
    assert create_agent.exists()
    live = "\n".join(
        line
        for line in create_agent.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "/agents/$AGENT_NAME/register" in live, (
        "create-agent.sh does not POST to the register endpoint outside of a comment"
    )
