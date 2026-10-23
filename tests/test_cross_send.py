"""Cross-channel send (#278): policy, rate limit, mention safety, registry key,
the outbox column, the `POST /send` route and the `send` tool."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PACKAGE_ROOT / "lib"))
import cross_send as cs  # noqa: E402
import outbox as ob  # noqa: E402
import registry  # noqa: E402

from conftest import import_script  # noqa: E402

OPS = "800000000000000001"
OTHER = "800000000000000002"


# -- policy -----------------------------------------------------------------

def test_default_is_off_with_a_20_per_hour_limit():
    assert cs.parse_config(None) == ([], 20)
    assert cs.parse_config({}) == ([], 20)


def test_parse_config_keeps_valid_ids_and_falls_back_on_junk():
    ids, per = cs.parse_config({"channels": [int(OPS), OPS, "general", True, "12"], "per_hour": 5})
    assert ids == [OPS] and per == 5
    assert cs.parse_config({"channels": "all", "per_hour": 0}) == ([], 20)
    assert cs.parse_config({"per_hour": True})[1] == 20


def test_neutralise_mentions():
    out = cs.neutralise_mentions("@everyone @HERE <@&123456789012345678> <@42> ok")
    assert "@everyone" not in out and "@here" not in out.lower()
    assert "<@&" not in out
    assert "<@42>" in out                       # a user mention stays readable
    assert out.replace(cs.ZWSP, "") == "@everyone @HERE <@&123456789012345678> <@42> ok"


def test_check_content():
    assert cs.check_content("hi") is None
    assert cs.check_content("   ") and cs.check_content(None) and cs.check_content(5)
    assert "limit" in cs.check_content("x" * (cs.MAX_CONTENT_CHARS + 1))


def test_rate_limiter_is_a_rolling_window_per_agent():
    rl = cs.RateLimiter()
    assert [rl.allow("a", 2, t)[0] for t in (0, 1, 2)] == [True, True, False]
    ok, wait = rl.allow("a", 2, 10)
    assert not ok and 3589 < wait <= 3590
    assert rl.allow("b", 2, 10)[0] is True      # another agent has its own budget
    assert rl.allow("a", 2, 3600)[0] is True    # the first slot has aged out


def test_audit_line_has_no_content():
    line = cs.audit_line("amos", OPS, 11, "ob-1")
    assert OPS in line and "amos" in line and "chars=11" in line


# -- registry ---------------------------------------------------------------

def _parse(send):
    main = {"name": "Main", "role": "primary"}
    if send is not None:
        main["send"] = send
    return registry.parse_registry({"version": 2, "agents": {
        "main": main, "watch": {"name": "Watch", "role": "monitor"}}})


def test_registry_default_and_valid_block():
    reg = _parse({"channels": [OPS], "per_hour": 10})
    assert reg.agent("main").settings["send"] == {"channels": [OPS], "per_hour": 10}
    assert _parse(None).agent("main").settings["send"] == {
        "channels": [], "per_hour": 20, "unlimited_channels": []}
    assert reg.legacy_view()["agents"]["main"]["send"]["channels"] == [OPS]


@pytest.mark.parametrize("bad", [
    "yes", {"channels": "all"}, {"channels": ["general"]}, {"channels": [1]},
    {"per_hour": 0}, {"per_hour": 500}, {"per_hour": True},
    {"channels": [OPS], "unlimited_channels": [OTHER]},   # not in channels
    {"unlimited_channels": [OPS]},                        # channels empty
    {"channels": [OPS], "unlimited_channels": "all"},
])
def test_registry_rejects_a_bad_send_block(bad):
    with pytest.raises(registry.RegistryError) as e:
        _parse(bad)
    assert "'send" in str(e.value.args[0])


# -- outbox -----------------------------------------------------------------

def test_outbox_stores_allowed_mentions_and_old_stores_gain_the_column(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(ob.SCHEMA)
    c.close()
    conn = ob.open_store(p)
    rid, _ = ob.enqueue(conn, "a", "1", "hi", allowed_mentions={"parse": []})
    assert json.loads(ob.get(conn, rid)["allowed_mentions"]) == {"parse": []}
    rid2, _ = ob.enqueue(conn, "a", "1", "plain")
    assert ob.get(conn, rid2)["allowed_mentions"] is None


# -- the route --------------------------------------------------------------

def _scenario(harness, agent_cfg, steps, channels=None):
    h = harness(agents={"a": agent_cfg})

    async def go():
        async with h:
            if channels is not None:
                h.module.channels_config = channels
            h.module._send_limiter = cs.RateLimiter()
            h.module.AGENT_TOKENS.clear()          # nothing is delivered; rows stay queued

            async def call(body):
                resp = await h.client.post("/send", headers=h._headers(), json={"agent": "a", **body})
                return resp.status, await resp.json()
            return await steps(h, call)
    return asyncio.run(go())


def _rows(h):
    conn = h.module._outbox_store(create=False)
    return [dict(r) for r in conn.execute("SELECT * FROM outbox")] if conn else []


def test_send_is_denied_by_default(harness):
    async def steps(h, call):
        return await call({"action": "send", "channel": OPS, "content": "hi"}), _rows(h)
    (status, body), rows = _scenario(harness, {}, steps)
    assert status == 403 and "not allowed" in body["error"] and rows == []


def test_allowed_channel_queues_one_neutralised_row_with_no_mentions(harness):
    async def steps(h, call):
        out = await call({"action": "send", "channel": "ops", "content": "done @everyone"})
        return out, _rows(h)
    (status, body), rows = _scenario(
        harness, {"send": {"channels": [OPS]}}, steps,
        channels={"channels": {"ops": {"id": OPS}}})
    assert status == 202 and body["channel_id"] == OPS
    assert len(rows) == 1 and rows[0]["channel_id"] == OPS
    assert "@everyone" not in rows[0]["content"] and "done" in rows[0]["content"]
    assert json.loads(rows[0]["allowed_mentions"]) == {"parse": []}


def test_unlisted_channel_gets_the_same_answer_as_an_unknown_one(harness):
    async def steps(h, call):
        a = await call({"action": "send", "channel": OTHER, "content": "x"})
        b = await call({"action": "send", "channel": "nowhere", "content": "x"})
        return a, b, _rows(h)
    a, b, rows = _scenario(harness, {"send": {"channels": [OPS]}}, steps)
    assert a == b and a[0] == 403 and rows == []


def test_rate_limit_and_bad_content(harness):
    async def steps(h, call):
        outs = [(await call({"action": "send", "channel": OPS, "content": f"m{i}"}))[0]
                for i in range(3)]
        bad = (await call({"action": "send", "channel": OPS, "content": "  "}))[0]
        return outs, bad, len(_rows(h))
    outs, bad, n = _scenario(harness, {"send": {"channels": [OPS], "per_hour": 2}}, steps)
    assert outs == [202, 202, 429] and bad == 400 and n == 2


def test_exempt_channel_skips_the_limit_and_others_keep_it(harness):
    async def steps(h, call):
        ex = [(await call({"action": "send", "channel": OPS, "content": f"e{i}"}))[0]
              for i in range(4)]
        lim = [(await call({"action": "send", "channel": OTHER, "content": f"l{i}"}))[0]
               for i in range(3)]
        return ex, lim, len(_rows(h))
    cfg = {"send": {"channels": [OPS, OTHER], "per_hour": 2, "unlimited_channels": [OPS]}}
    ex, lim, n = _scenario(harness, cfg, steps)
    assert ex == [202] * 4 and lim == [202, 202, 429] and n == 6


def test_exempt_sends_do_not_use_up_the_limit(harness):
    async def steps(h, call):
        for i in range(5):
            await call({"action": "send", "channel": OPS, "content": f"e{i}"})
        return (await call({"action": "send", "channel": OTHER, "content": "x"}))[0]
    cfg = {"send": {"channels": [OPS, OTHER], "per_hour": 1, "unlimited_channels": [OPS]}}
    assert _scenario(harness, cfg, steps) == 202


def test_parse_unlimited_never_widens_the_allowlist():
    allowed = [OPS]
    assert cs.parse_unlimited({"unlimited_channels": [OPS, OTHER, True]}, allowed) == [OPS]
    assert cs.parse_unlimited({}, allowed) == [] and cs.parse_unlimited(None, allowed) == []


def test_audit_line_marks_exempt_sends():
    assert cs.audit_line("a", OPS, 3, "ob-1").endswith("outbox=ob-1")
    assert cs.audit_line("a", OPS, 3, "ob-1", exempt=True).endswith("outbox=ob-1 exempt=1")


def test_channels_action_lists_only_the_allowlist(harness):
    async def steps(h, call):
        return await call({"action": "channels"})
    status, body = _scenario(harness, {"send": {"channels": [OPS]}}, steps,
                             channels={"channels": {"ops": {"id": OPS}, "secret": {"id": OTHER}}})
    assert status == 200
    assert body == {"channels": [{"id": OPS, "name": "ops"}], "per_hour": 20}


def test_route_requires_the_bearer_token(harness):
    h = harness(agents={"a": {}})

    async def go():
        async with h:
            resp = await h.client.post("/send", json={"agent": "a", "action": "channels"})
            return resp.status
    assert asyncio.run(go()) == 401


# -- delivery carries allowed_mentions --------------------------------------

def test_outbox_send_row_sends_allowed_mentions(tmp_path):
    from test_outbox_send import FakeDiscord, ags as _ags  # noqa: F401
    import importlib.util
    import os
    (tmp_path / "logs").mkdir(exist_ok=True)
    os.environ["WORKSPACE_ROOT"] = str(tmp_path)
    spec = importlib.util.spec_from_file_location("ags_cross_send_under_test",
                                                  PACKAGE_ROOT / "bin" / "agent-server.py")
    ags = importlib.util.module_from_spec(spec)
    sys.modules["ags_cross_send_under_test"] = ags
    spec.loader.exec_module(ags)
    ags.AGENT_TOKENS["amos"] = "t"
    ags.OUTBOX_PATH = tmp_path / "data" / "outbox" / "outbox.db"
    ags.http_session = FakeDiscord()
    conn = ags._outbox_store()
    rid, _ = ob.enqueue(conn, "amos", OPS, "hello", claimed=True, allowed_mentions={"parse": []})
    assert asyncio.run(ags.outbox_send_row(ob.get(conn, rid))) == "msg-1"
    assert ags.http_session.bodies[0]["allowed_mentions"] == {"parse": []}
    rid2, _ = ob.enqueue(conn, "amos", OPS, "plain", claimed=True)
    asyncio.run(ags.outbox_send_row(ob.get(conn, rid2)))
    assert "allowed_mentions" not in ags.http_session.bodies[1]


# -- the tool ---------------------------------------------------------------

@pytest.fixture
def tools_server(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    return import_script("tools-server", file_path=PACKAGE_ROOT / "mcp" / "tools-server.py")


def test_send_tool_is_listed_and_forwards(tools_server, monkeypatch):
    assert "send" in {t["name"] for t in tools_server.CORE_TOOLS}
    sent = []

    def fake(method, path, payload=None, timeout=15.0):
        sent.append((method, path, payload))
        return 202, {"queued": True}
    monkeypatch.setattr(tools_server, "agent_server_request", fake)
    out = tools_server.handle_core_tool("send", {"action": "send", "channel": "ops", "content": "x"})
    assert out == {"queued": True}
    assert sent[0][:2] == ("POST", "/send") and sent[0][2]["content"] == "x"


def test_send_tool_surfaces_errors(tools_server, monkeypatch):
    monkeypatch.setattr(tools_server, "agent_server_request",
                        lambda *a, **k: (403, {"error": "channel not allowed for send"}))
    assert tools_server.handle_core_tool("send", {"action": "send", "channel": "x", "content": "y"}) \
        == {"error": "channel not allowed for send"}
    assert "Unknown" in tools_server.handle_core_tool("send", {"action": "zap"})["error"]
