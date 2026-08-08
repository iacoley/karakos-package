"""
Tests for rate-limit headroom tracking (#105).

Acceptance test from the issue: `/usage` reports percent of the current
rate-limit window consumed, and crossing 80% posts one alert.

Two design decisions are load-bearing and each is pinned below.

**The numbers are read in-band, not polled.** The CLI emits `rate_limit_event`
on the stream-json output it is already writing. A poller against the OAuth
usage endpoint would answer the same question on separate auth, on its own
schedule, and be stale between polls. `test_a_rate_limit_event_on_the_stream_*`
drives the real stream loop.

**"No reading yet" is not "0% used".** They are opposite answers and rendering
them the same would reproduce the failure this issue describes — headroom
invisible until it fires — with a reassuring number on top.
"""

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
AGENT_SERVER = PACKAGE_ROOT / "bin" / "agent-server.py"
RELAY_PATH = PACKAGE_ROOT / "bin" / "relay.py"

FIVE_HOURS = 5 * 3600


@pytest.fixture
def ags(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "logs").mkdir(parents=True, exist_ok=True)
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(workspace)
    try:
        spec = importlib.util.spec_from_file_location("ags_usage_under_test", AGENT_SERVER)
        module = importlib.util.module_from_spec(spec)
        sys.modules["ags_usage_under_test"] = module
        spec.loader.exec_module(module)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    module.AGENT_TOKENS["amos"] = "fake-token"
    module.agent_config["amos"] = {"system_prompt": "unused"}
    return module


def _info(**overrides):
    """A rate_limit_info payload in the shape the CLI actually emits."""
    base = {
        "status": "allowed",
        "resetsAt": int(time.time()) + FIVE_HOURS,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "isUsingOverage": False,
    }
    base.update(overrides)
    return base


def _with_db(ags, coro_factory):
    async def run():
        await ags.init_db()
        try:
            return await coro_factory()
        finally:
            await ags.db.close()
    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Window arithmetic
# ---------------------------------------------------------------------------

def test_a_fresh_window_reads_as_zero_percent_elapsed(ags):
    now = 1_000_000
    progress = ags.rate_limit_window_progress(
        _info(resetsAt=now + FIVE_HOURS), now=now)
    assert progress == 0.0


def test_four_hours_into_a_five_hour_window_reads_as_eighty_percent(ags):
    now = 1_000_000
    progress = ags.rate_limit_window_progress(
        _info(resetsAt=now + 3600), now=now)
    assert progress == pytest.approx(0.8)


def test_a_seven_day_window_uses_its_own_length(ags):
    now = 1_000_000
    progress = ags.rate_limit_window_progress(
        _info(rateLimitType="seven_day", resetsAt=now + 7 * 86400 // 2), now=now)
    assert progress == pytest.approx(0.5)


@pytest.mark.parametrize("broken", [
    {"rateLimitType": "five_hour"},                      # no resetsAt
    {"resetsAt": 123, "rateLimitType": "fortnightly"},   # unknown window
    {"resetsAt": None, "rateLimitType": "five_hour"},
    None,
    "not a dict",
])
def test_an_uncomputable_window_is_unknown_and_never_zero(ags, broken):
    # Returning 0.0 here would report "no headroom used" for a reading we do
    # not have — the reassuring version of the bug being fixed.
    assert ags.rate_limit_window_progress(broken) is None


def test_an_already_expired_window_is_unknown_rather_than_a_hundred_percent(ags):
    now = 1_000_000
    assert ags.rate_limit_window_progress(_info(resetsAt=now - 60), now=now) is None


# ---------------------------------------------------------------------------
# Recording and alerting
# ---------------------------------------------------------------------------

def test_an_event_is_persisted_with_every_field_the_cli_gave(ags):
    def scenario():
        async def go():
            await ags.record_rate_limit_event("amos", _info(isUsingOverage=True))
            async with ags.db.execute("SELECT * FROM rate_limit_state") as cursor:
                return [dict(r) for r in await cursor.fetchall()]
        return go()

    rows = _with_db(ags, lambda: scenario())
    assert len(rows) == 1
    assert rows[0]["status"] == "allowed"
    assert rows[0]["rate_limit_type"] == "five_hour"
    assert rows[0]["is_using_overage"] == 1
    assert rows[0]["overage_status"] == "rejected"


def test_a_later_event_overwrites_rather_than_accumulating(ags):
    def scenario():
        async def go():
            await ags.record_rate_limit_event("amos", _info(status="allowed"))
            await ags.record_rate_limit_event("amos", _info(status="allowed_warning"))
            async with ags.db.execute("SELECT * FROM rate_limit_state") as cursor:
                return [dict(r) for r in await cursor.fetchall()]
        return go()

    rows = _with_db(ags, lambda: scenario())
    assert len(rows) == 1
    assert rows[0]["status"] == "allowed_warning"


def _capture_alerts(ags):
    posted = []

    async def fake_post(agent, channel_id, content, **kwargs):
        posted.append((agent, channel_id, content))

    ags.post_to_discord = fake_post
    ags.RATE_LIMIT_ALERT_CHANNEL_ID = "555"
    return posted


def test_crossing_eighty_percent_within_one_window_posts_one_alert(ags):
    """The acceptance test, played out the way a real window plays out.

    `resetsAt` is FIXED for the life of a window — the same value arrives on
    every event in it while the elapsed fraction climbs. Advancing `now`
    against a fixed reset time is therefore the only faithful shape for this
    test, and it is the shape that matters: an earlier version varied
    `resetsAt` between the two events, which quietly made them two different
    windows and passed against an implementation that stamped the
    already-warned column on every write and so never warned at all.
    """
    posted = _capture_alerts(ags)
    resets_at = 2_000_000  # one fixed window

    def scenario():
        async def go():
            # 3h into the 5h window — under the threshold, nothing said.
            await ags.record_rate_limit_event(
                "amos", _info(resetsAt=resets_at), now=resets_at - 2 * 3600)
            assert posted == [], "warned before the threshold"
            # 4h in — 80%, the acceptance test's threshold.
            await ags.record_rate_limit_event(
                "amos", _info(resetsAt=resets_at), now=resets_at - 3600)
            # 4h30 in — still the same window, must not warn twice.
            await ags.record_rate_limit_event(
                "amos", _info(resetsAt=resets_at), now=resets_at - 1800)
        return go()

    _with_db(ags, lambda: scenario())
    assert len(posted) == 1
    assert posted[0][1] == "555"
    assert "rate-limit" in posted[0][2]


def test_the_alert_fires_once_per_window_not_once_per_event(ags):
    posted = _capture_alerts(ags)
    now = int(time.time())
    resets = now + 600

    def scenario():
        async def go():
            for _ in range(5):
                await ags.record_rate_limit_event("amos", _info(resetsAt=resets))
        return go()

    _with_db(ags, lambda: scenario())
    assert len(posted) == 1


def test_a_new_window_can_alert_again(ags):
    # Keyed on resetsAt rather than a boolean: a flag would fire once ever,
    # and the limit is a recurring window, so the second day would be silent.
    posted = _capture_alerts(ags)
    now = int(time.time())

    def scenario():
        async def go():
            # Two distinct windows, each late enough to be worth saying.
            await ags.record_rate_limit_event("amos", _info(resetsAt=now + 600))
            await ags.record_rate_limit_event("amos", _info(resetsAt=now + 300))
        return go()

    _with_db(ags, lambda: scenario())
    assert len(posted) == 2


def test_the_clis_own_warning_status_alerts_even_early_in_the_window(ags):
    # `allowed_warning` is the CLI saying headroom is short. Believing our own
    # elapsed-time arithmetic over that would ignore the better signal.
    posted = _capture_alerts(ags)
    now = int(time.time())

    def scenario():
        async def go():
            await ags.record_rate_limit_event(
                "amos", _info(status="allowed_warning", resetsAt=now + FIVE_HOURS))
        return go()

    _with_db(ags, lambda: scenario())
    assert len(posted) == 1


def test_a_rejected_status_alerts(ags):
    posted = _capture_alerts(ags)
    now = int(time.time())

    def scenario():
        async def go():
            await ags.record_rate_limit_event(
                "amos", _info(status="rejected", resetsAt=now + FIVE_HOURS))
        return go()

    _with_db(ags, lambda: scenario())
    assert len(posted) == 1


def test_a_healthy_reading_says_nothing(ags):
    posted = _capture_alerts(ags)
    now = int(time.time())

    def scenario():
        async def go():
            await ags.record_rate_limit_event("amos", _info(resetsAt=now + FIVE_HOURS))
        return go()

    _with_db(ags, lambda: scenario())
    assert posted == []


def test_an_empty_or_malformed_payload_is_ignored_not_stored(ags):
    def scenario():
        async def go():
            for junk in (None, {}, "string", []):
                await ags.record_rate_limit_event("amos", junk)
            async with ags.db.execute("SELECT COUNT(*) c FROM rate_limit_state") as cursor:
                return (await cursor.fetchone())["c"]
        return go()

    assert _with_db(ags, lambda: scenario()) == 0


def test_an_unset_alert_channel_logs_instead_of_raising(ags):
    # An alert with nowhere to go must not become an exception on the turn
    # that raised it.
    ags.RATE_LIMIT_ALERT_CHANNEL_ID = ""
    now = int(time.time())

    def scenario():
        async def go():
            await ags.record_rate_limit_event(
                "amos", _info(status="rejected", resetsAt=now + FIVE_HOURS))
            async with ags.db.execute("SELECT * FROM rate_limit_state") as cursor:
                return [dict(r) for r in await cursor.fetchall()]
        return go()

    rows = _with_db(ags, lambda: scenario())
    assert rows[0]["status"] == "rejected"


# ---------------------------------------------------------------------------
# The /usage surface
# ---------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"}


def _get_usage(ags):
    def scenario():
        async def go():
            response = await ags.handle_usage(FakeRequest(ags.AGENT_SERVER_TOKEN))
            return response.status, json.loads(response.text)
        return go()
    return _with_db(ags, lambda: scenario())


def test_usage_requires_the_bearer_token(ags):
    def scenario():
        async def go():
            return await ags.handle_usage(FakeRequest("wrong"))
        return go()

    assert _with_db(ags, lambda: scenario()).status == 401


def test_usage_reports_percent_of_the_window_consumed(ags):
    now = int(time.time())

    def scenario():
        async def go():
            await ags.record_rate_limit_event("amos", _info(resetsAt=now + 3600))
            response = await ags.handle_usage(FakeRequest(ags.AGENT_SERVER_TOKEN))
            return json.loads(response.text)
        return go()

    body = _with_db(ags, lambda: scenario())
    amos = body["agents"]["amos"]
    assert amos["percent_of_window_used"] == pytest.approx(80.0, abs=0.5)
    assert amos["status"] == "allowed"
    assert "80%" in amos["summary"]


def test_usage_before_any_reading_says_unknown_rather_than_zero(ags):
    status, body = _get_usage(ags)
    assert status == 200
    amos = body["agents"]["amos"]
    assert amos["percent_of_window_used"] is None
    assert "0%" not in amos["summary"]


def test_usage_names_overage_when_it_is_in_use(ags):
    now = int(time.time())

    def scenario():
        async def go():
            await ags.record_rate_limit_event(
                "amos", _info(resetsAt=now + FIVE_HOURS, isUsingOverage=True))
            response = await ags.handle_usage(FakeRequest(ags.AGENT_SERVER_TOKEN))
            return json.loads(response.text)
        return go()

    amos = _with_db(ags, lambda: scenario())["agents"]["amos"]
    assert amos["is_using_overage"] is True
    assert "overage" in amos["summary"]


# ---------------------------------------------------------------------------
# The wiring — in-band, off the stream that is already open
# ---------------------------------------------------------------------------

def test_the_stream_reader_records_rate_limit_events():
    """Everything above can pass while nothing ever calls it.

    `record_rate_limit_event` is correct in isolation, but if the stream loop
    never invokes it the table stays empty forever and `/usage` reports
    "no reading yet" on a healthy install — indistinguishable from the feature
    being absent. Checked through the AST so a mention in a comment (and this
    module is full of them) cannot satisfy it.
    """
    import ast

    tree = ast.parse(AGENT_SERVER.read_text())
    reader = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "read_agent_response"
    )
    called = {
        getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        for n in ast.walk(reader) if isinstance(n, ast.Call)
    }
    assert "record_rate_limit_event" in called


def test_no_poller_against_the_oauth_usage_endpoint_was_introduced():
    """The issue's fix shape named a poller; the in-band event is better.

    Pinned because the tempting implementation is the wrong one: a poller
    answers on separate auth, on its own schedule, and is stale between polls,
    while `rate_limit_event` updates on every turn the limit changes.
    """
    src = AGENT_SERVER.read_text()
    assert "api.anthropic.com/api/oauth/usage" not in src
    assert "oauth/usage" not in src


def test_the_relay_exposes_usage_as_a_sys_command():
    import ast

    tree = ast.parse(RELAY_PATH.read_text())
    handlers = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "sys_usage" in handlers

    # And it is actually reachable: the command must be in the allowed set,
    # or parse_sys_command drops it before any handler runs.
    namespace = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) == "SYS_COMMANDS":
                    namespace["SYS_COMMANDS"] = ast.literal_eval(
                        node.value.args[0] if isinstance(node.value, ast.Call) else node.value
                    )
    assert "usage" in namespace["SYS_COMMANDS"]
