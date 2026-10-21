"""ReplyGate tier 2 (opt-in Haiku classifier) driven through on_message.
The classifier subprocess is always a fake."""

import asyncio
import copy
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from test_relay_reply_gate import (  # noqa: E402,F401
    AMOS_BOT_ID, CHANNELS, FOREIGN_BOT_ID, GATED_CHANNEL, ALEX, MIKE,
    FakeMessage, adapter, mention_amos, relay,
)
import reply_classifier as rc  # noqa: E402


OBJ = {"classifier": "haiku", "max_per_minute": 4, "max_per_hour": 60}


class Runner:
    def __init__(self, text="ENGAGE 0.9", cost=0.004, delay=0.0):
        self.text, self.cost, self.delay = text, cost, delay
        self.prompts, self.active, self.max_active = [], 0, 0

    async def __call__(self, prompt, cfg):
        self.prompts.append(prompt)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if isinstance(self.text, Exception):
                raise self.text
            return rc.RunResult(self.text, self.cost)
        finally:
            self.active -= 1


@pytest.fixture
def t2(relay, adapter, monkeypatch, tmp_path):
    channels = copy.deepcopy(CHANNELS)
    channels["channels"]["kitchen"]["reply_gate"] = dict(OBJ)
    monkeypatch.setattr(relay, "channels_config", channels)
    monkeypatch.setattr(relay, "HEALTH_FILE", tmp_path / "health" / "relay.json")
    monkeypatch.setattr(relay, "WORKSPACE_ROOT", tmp_path)  # no agent-server.db here
    adapter.costs = []

    async def post(path, payload=None):
        adapter.costs.append((path, payload))
        return True, "", {}
    adapter.agent_server_post_json = post
    adapter.runner = Runner()
    adapter.reply_gate.runner = adapter.runner
    return adapter


def say(a, text, who=MIKE, **kw):
    asyncio.run(a.on_message(FakeMessage(who, text, **kw)))
    return a.routed


def test_ambiguous_engages_and_routes_to_default_agent(t2):
    say(t2, "does anyone know if the oven is on?")
    assert t2.routed == [("amos", "does anyone know if the oven is on?")]
    assert len(t2.runner.prompts) == 1


def test_silent_verdict_stays_silent(t2):
    t2.runner.text = "SILENT 0.9"
    say(t2, "does anyone know if the oven is on?")
    assert t2.routed == []


def test_low_confidence_and_garbage_silent(t2):
    t2.runner.text = "ENGAGE 0.5"
    say(t2, "can you check the oven")
    t2.runner.text = "Sure! ENGAGE 0.9"
    say(t2, "can you check the oven again")
    assert t2.routed == []


def test_bool_gate_never_builds_a_runner(relay, adapter):
    def boom(*a, **k):
        raise AssertionError("classifier reached")
    adapter.reply_gate.runner = boom
    asyncio.run(adapter.on_message(FakeMessage(MIKE, "can you check the oven?")))
    assert adapter.routed == []


def test_object_without_classifier_is_heuristic(t2, relay):
    t2.runner.text = "ENGAGE 0.99"
    relay.channels_config["channels"]["kitchen"]["reply_gate"] = {"context_messages": 3}
    say(t2, "can you check the oven?")
    assert t2.routed == [] and not t2.runner.prompts


def test_kill_switch(t2, monkeypatch):
    monkeypatch.setenv("KARAKOS_REPLY_CLASSIFIER", "off")
    say(t2, "can you check the oven?")
    assert t2.routed == [] and not t2.runner.prompts


def test_tier1_never_reaches_runner(t2):
    say(t2, "can you grab it?", replied_to=ALEX)           # tier-1 silent
    say(t2, "amos, add milk")                                  # tier-1 engage
    assert not t2.runner.prompts
    assert t2.routed == [("amos", "amos, add milk")]


def test_volley_never_reaches_runner(t2):
    gate = t2.reply_gate
    now = time.time()
    for i in range(8):
        gate.decide(channel_id=GATED_CHANNEL, content="x", mentions_agent=False,
                    replied_to_author_id=None, agent_ids={AMOS_BOT_ID}, now=now + i * 0.1)
    say(t2, "what about tuesday")
    assert not t2.runner.prompts and t2.routed == []


def test_bot_author_never_reaches_runner(t2):
    asyncio.run(t2.on_message(FakeMessage(FOREIGN_BOT_ID, "can you check the oven?", bot=True)))
    assert not t2.runner.prompts and t2.routed == []


def test_empty_message_never_reaches_runner(t2):
    say(t2, "   ")
    assert not t2.runner.prompts


def test_context_excludes_current_includes_agent_posts(t2):
    say(t2, "first thing", who=ALEX)
    t2.runner.text = "SILENT 0.9"
    # an agent post arrives through the own-message branch
    own = FakeMessage(AMOS_BOT_ID, "I put it on the list", bot=True)
    asyncio.run(t2.on_message(own))
    say(t2, "and the second thing?")
    p = t2.runner.prompts[-1]
    assert p.index("first thing") < p.index("I put it on the list") < p.index("and the second thing?")
    assert p.count("and the second thing?") == 1


def test_rate_cap_per_minute(t2):
    t2.runner.text = "SILENT 0.9"
    for i in range(5):
        say(t2, f"q{i}?")
    assert len(t2.runner.prompts) == 4
    assert t2.reply_gate.stats["rate_capped"] == 1


def test_rate_cap_per_hour(relay, t2):
    relay.channels_config["channels"]["kitchen"]["reply_gate"] = {
        "classifier": "haiku", "max_per_minute": 20, "max_per_hour": 3}
    t2.runner.text = "SILENT 0.9"
    for i in range(4):
        say(t2, f"q{i}?")
    assert len(t2.runner.prompts) == 3 and t2.reply_gate.stats["rate_capped"] == 1


def test_breaker_paused_no_call(t2, relay, tmp_path):
    import sqlite3
    (tmp_path / "data" / "memory").mkdir(parents=True)
    con = sqlite3.connect(tmp_path / "data" / "memory" / "agent-server.db")
    con.execute("CREATE TABLE rate_limit_state (rate_limit_type TEXT, status TEXT, resets_at REAL,"
                " overage_status TEXT, utilization REAL, updated_at REAL)")
    con.execute("INSERT INTO rate_limit_state VALUES ('five_hour','rejected',?,NULL,1.0,?)",
                (time.time() + 900, time.time()))
    con.commit()
    con.close()
    say(t2, "can you check the oven?")
    assert not t2.runner.prompts and t2.routed == []


def test_same_channel_calls_run_one_at_a_time(t2):
    t2.runner.delay = 0.05

    async def go():
        await asyncio.gather(
            t2.on_message(FakeMessage(MIKE, "first question?")),
            t2.on_message(FakeMessage(ALEX, "second question?")))
    asyncio.run(go())
    assert len(t2.runner.prompts) == 2 and t2.runner.max_active == 1


def test_runner_failures_are_silent(t2):
    t2.runner.text = RuntimeError("boom")
    say(t2, "can you check the oven?")
    assert t2.routed == [] and t2.reply_gate.stats["errors"] == 1


def test_cost_posted_under_agent_id(t2):
    say(t2, "can you check the oven?")
    assert t2.costs == [("/cost", {"agent": "amos", "cost_delta": 0.004})]


def test_cost_post_failure_changes_nothing(t2):
    async def bad(path, payload=None):
        return False, "down", {}
    t2.agent_server_post_json = bad
    say(t2, "can you check the oven?")
    assert t2.routed == [("amos", "can you check the oven?")]


def test_health_carries_counters(t2, relay):
    say(t2, "can you check the oven?")
    data = json.loads(relay.HEALTH_FILE.read_text())["reply_gate"]
    assert data["calls"] == 1 and data["engaged"] == 1 and data["last_call_ts"]
    assert set(data) == {"calls", "engaged", "silent", "errors", "timeouts",
                         "rate_capped", "budget", "last_call_ts"}


def test_log_has_no_message_text(t2, caplog):
    import logging
    t2.runner.text = "SILENT 0.9"
    with caplog.at_level(logging.INFO):
        say(t2, "my secret oven password?")
    gate_lines = [r.getMessage() for r in caplog.records if "[gate]" in r.getMessage()]
    assert gate_lines and all("secret" not in l for l in gate_lines)
    assert "silent (haiku silent)" in gate_lines[0]
