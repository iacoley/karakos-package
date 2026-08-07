"""
Tests for the Discord dead-letter queue in bin/agent-server.py.

The case this exists for: the agent ran, the tokens were spent, the reply
exists — and the relay could not deliver it. `crash_recovery()` covers a
message stuck in the queue by a crash. It does not cover a reply that was
generated and then failed to post, which until now became a log line and
nothing else.

These drive the real `post_to_discord` against a fake Discord API rather than
grepping the source, because the thing under test is what happens across
retries and status codes, and source text cannot tell you that.

Acceptance test from #89, pinned in test_permission_revoked_*: revoke the
bot's Send Messages permission (Discord answers 403), send it a message, and
the reply must end up in data/discord-dead-letter.jsonl with /health showing a
non-zero count.
"""

import ast
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"


# --- structural helpers ----------------------------------------------------
# The call-site checks below go through the AST rather than through `in src`.
# A substring search reads comments and docstrings as if they were code: the
# first draft of test_crash_recovery_does_not_dead_letter passed a grep for
# `dead_letter=True` against the comment explaining why that call deliberately
# omits it.

def _tree():
    return ast.parse(AGENT_SERVER.read_text())


def _function(name):
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in agent-server.py")


def _calls_to(func_node, callee):
    out = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            fn = node.func
            fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if fname == callee:
                out.append(node)
    return out


def _kwarg(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


@pytest.fixture
def ags(tmp_path):
    """Import bin/agent-server.py against a temp workspace.

    Function-scoped: DEAD_LETTER_PATH is resolved at import time, so each test
    gets its own file and they cannot see each other's records.
    """
    (tmp_path / "logs").mkdir(exist_ok=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("ags_under_test", AGENT_SERVER)
        module = importlib.util.module_from_spec(spec)
        sys.modules["ags_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev

    module.AGENT_TOKENS["amos"] = "fake-token"
    module.POST_RETRY_BASE_SEC = 0.0  # don't sleep through the backoff in tests
    return module


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload if payload is not None else {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class FakeDiscord:
    """Answers POSTs with a scripted sequence of statuses."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 200:
            return FakeResponse(200, {"id": f"msg-{self.calls}"})
        if status == 429:
            return FakeResponse(429, {"retry_after": 0})
        return FakeResponse(status, text=f"error {status}")


def read_letters(ags):
    if not ags.DEAD_LETTER_PATH.exists():
        return []
    return [json.loads(line) for line in
            ags.DEAD_LETTER_PATH.read_text().splitlines() if line.strip()]


def post(ags, statuses, content="here is your answer", dead_letter=True):
    ags.http_session = FakeDiscord(statuses)
    result = asyncio.run(
        ags.post_to_discord("amos", "555", content, dead_letter=dead_letter)
    )
    return result, ags.http_session


# ---------------------------------------------------------------------------
# The acceptance test — Send Messages revoked
# ---------------------------------------------------------------------------

def test_permission_revoked_writes_the_reply_to_the_dead_letter_queue(ags):
    result, _ = post(ags, [403], content="the answer you waited for")

    assert result is None
    letters = read_letters(ags)
    assert len(letters) == 1
    assert letters[0]["content"] == "the answer you waited for", \
        "the point is recovering the reply, so the text has to survive intact"
    assert letters[0]["agent"] == "amos"
    assert letters[0]["channel_id"] == "555"
    assert "403" in letters[0]["reason"]


def test_permission_revoked_shows_up_in_the_health_count(ags):
    assert ags.dead_letter_count() == 0
    post(ags, [403])
    post(ags, [403])
    assert ags.dead_letter_count() == 2


def test_health_route_reports_the_count(ags):
    """The count is only useful if the endpoint actually surfaces it — every
    agent looks idle and healthy while its answers are going nowhere."""
    health = _function("handle_health")
    assert _calls_to(health, "dead_letter_count"), \
        "handle_health never asks for the count"

    keys = [n.value for node in ast.walk(health) if isinstance(node, ast.Dict)
            for n in node.keys
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert "dead_letters" in keys, "the count is never put in the response body"


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

def test_a_403_is_not_retried(ags):
    """A revoked permission does not heal by trying again; retrying only
    delays the moment the reply is recorded as undeliverable."""
    _, session = post(ags, [403])
    assert session.calls == 1


def test_a_500_is_retried_to_the_limit_then_dead_lettered(ags):
    _, session = post(ags, [500, 500, 500])
    assert session.calls == ags.POST_MAX_ATTEMPTS
    assert len(read_letters(ags)) == 1


def test_a_transient_failure_that_recovers_is_not_dead_lettered(ags):
    """The retry has to be real: a 500 then a 200 is a delivered reply."""
    result, session = post(ags, [500, 200])
    assert result == "msg-2"
    assert session.calls == 2
    assert read_letters(ags) == []


def test_rate_limit_is_retried_and_can_succeed(ags):
    result, session = post(ags, [429, 200])
    assert result == "msg-2"
    assert read_letters(ags) == []


def test_a_network_exception_is_retried_then_dead_lettered(ags):
    class Boom:
        calls = 0

        def post(self, url, **kwargs):
            Boom.calls += 1
            raise ConnectionResetError("connection reset")

    ags.http_session = Boom()
    result = asyncio.run(ags.post_to_discord("amos", "555", "hello", dead_letter=True))

    assert result is None
    assert Boom.calls == ags.POST_MAX_ATTEMPTS
    letters = read_letters(ags)
    assert len(letters) == 1
    assert "ConnectionResetError" in letters[0]["reason"]


# ---------------------------------------------------------------------------
# Scope — what does NOT go in the queue
# ---------------------------------------------------------------------------

def test_incidental_posts_are_not_dead_lettered(ags):
    """Tool-event lines, cost updates and the crash notice all post through
    here. Nobody would replay them, and queueing them would bury the replies
    that matter."""
    post(ags, [403], content="🔧 Bash", dead_letter=False)
    assert read_letters(ags) == []
    assert ags.dead_letter_count() == 0


def test_the_reply_path_opts_in(ags):
    """Pins the caller, not the helper: the queue is worthless if the one path
    that generates real replies forgets to ask for it."""
    opted_in = []
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in _calls_to(node, "post_to_discord"):
                value = _kwarg(call, "dead_letter")
                if isinstance(value, ast.Constant) and value.value is True:
                    opted_in.append(node.name)

    assert opted_in, "nothing in the server ever asks for a reply to be preserved"
    # The one that matters is where a generated response goes out.
    assert any("response" in name or "process" in name for name in opted_in), \
        f"the response path is not among the opted-in callers: {opted_in}"


def test_crash_recovery_does_not_dead_letter(ags):
    """Those rows are already durable in the queue and are retried on every
    startup — dead-lettering them would append a fresh copy of the same reply
    each time the server came up against a still-unreachable channel."""
    recovery = _function("crash_recovery")
    for call in _calls_to(recovery, "post_to_discord"):
        value = _kwarg(call, "dead_letter")
        assert not (isinstance(value, ast.Constant) and value.value is True), \
            "crash_recovery would duplicate a dead letter on every startup"


# ---------------------------------------------------------------------------
# Durability of the recorder itself
# ---------------------------------------------------------------------------

def test_a_broken_dead_letter_file_does_not_take_down_the_reply_loop(ags, monkeypatch):
    """This is the error path. A failure to record a failure must not raise on
    top of it."""
    def explode(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("builtins.open", explode)
    result, _ = post(ags, [403])
    assert result is None, "the post still failed, and it still reported that"


def test_count_survives_a_corrupt_line(ags):
    ags.DEAD_LETTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    ags.DEAD_LETTER_PATH.write_text('{"ok":1}\nnot json\n\n{"ok":2}\n')
    assert ags.dead_letter_count() == 3, "counting must not depend on parsing"


def test_records_append_rather_than_overwrite(ags):
    post(ags, [403], content="first")
    post(ags, [403], content="second")
    letters = read_letters(ags)
    assert [entry["content"] for entry in letters] == ["first", "second"]
