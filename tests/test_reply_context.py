"""Replied-to message content reaches the agent payload."""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")

RELAY_PATH = Path(__file__).parent.parent / "bin" / "relay.py"


@pytest.fixture
def relay(tmp_path):
    ws = tmp_path / "ws"
    (ws / "logs").mkdir(parents=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("relay_reply_under_test", RELAY_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["relay_reply_under_test"] = mod
        spec.loader.exec_module(mod)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return mod


def _msg(content="hi", author="Alex", attachments=()):
    return SimpleNamespace(content=content, author=SimpleNamespace(display_name=author),
                           attachments=list(attachments))


def _reply(resolved=None, message_id=5, fetch=None):
    async def fetch_message(mid):
        if isinstance(fetch, Exception):
            raise fetch
        return fetch
    return SimpleNamespace(
        reference=SimpleNamespace(resolved=resolved, message_id=message_id),
        channel=SimpleNamespace(fetch_message=fetch_message),
    )




def _resolver(relay):
    cls = next(v for v in vars(relay).values()
               if isinstance(v, type) and hasattr(v, "resolve_reply_context"))
    return lambda m: asyncio.run(cls.resolve_reply_context(None, m))


def test_not_a_reply(relay):
    m = SimpleNamespace(reference=None)
    assert _resolver(relay)(m) is None


def test_resolved_used(relay):
    out = _resolver(relay)(_reply(resolved=_msg("pick the blue one", "Mike")))
    assert "Mike" in out and "pick the blue one" in out


def test_truncated(relay):
    out = _resolver(relay)(_reply(resolved=_msg("x" * 5000)))
    assert "[truncated]" in out and len(out) < 1200


def test_fetched_when_unresolved(relay):
    out = _resolver(relay)(_reply(resolved=None, fetch=_msg("fetched text")))
    assert "fetched text" in out


def test_deleted_stub_falls_back_to_fetch_then_placeholder(relay):
    stub = SimpleNamespace()  # DeletedReferencedMessage: no author
    out = _resolver(relay)(_reply(resolved=stub, fetch=discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")))
    assert "no longer available" in out


def test_fetch_error_graceful(relay):
    out = _resolver(relay)(_reply(resolved=None, fetch=RuntimeError("boom")))
    assert "no longer available" in out


def test_attachment_only(relay):
    out = relay.format_reply_context(_msg("", attachments=[SimpleNamespace(filename="a.png")]))
    assert "a.png" in out
