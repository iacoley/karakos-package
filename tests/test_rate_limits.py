"""lib/rate_limits.py: parse, upsert, breaker, utilization (spec 2.7). Pure."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import aiosqlite
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import rate_limits as rl  # noqa: E402

NOW = 1_800_000_000.0
FIXTURE = ROOT / "tests/harness/fixtures/real-cli/1-second-user-line-mid-turn/stdout.jsonl"


def real_info():
    for f in sorted((ROOT / "tests/harness/fixtures/real-cli").glob("*/stdout.jsonl")):
        for line in f.read_text().splitlines():
            if '"rate_limit_event"' in line:
                ev = json.loads(line)
                ev = ev.get("event", ev)
                return ev["rate_limit_info"]
    raise AssertionError("no fixture event")


def row(t, status=None, resets=None, updated=NOW, **kw):
    return {"rate_limit_type": t, "status": status, "resets_at": resets,
            "updated_at": updated, **kw}


def test_parse_real_fixture_event():
    ups = {u.type: u for u in rl.parse_event(real_info(), NOW)}
    assert set(ups) == {"five_hour", "seven_day"}
    five = ups["five_hour"]
    assert five.status == "allowed" and five.overage_status == "rejected"
    assert five.utilization == pytest.approx(11.0)
    assert ups["seven_day"].utilization == pytest.approx(73.0)
    assert ups["seven_day"].status is None


def test_parse_edge_inputs():
    assert rl.parse_event("nope", NOW) == []
    assert rl.parse_event(None, NOW) == []
    u = rl.parse_event({"status": 5}, NOW)[0]
    assert u.type == "unknown" and u.status is None
    assert rl.parse_event({"rateLimitType": "  "}, NOW)[0].type == "unknown"
    assert rl.parse_event({"status": "REJECTED"}, NOW)[0].status == "rejected"


@pytest.mark.parametrize("v,want", [
    (None, None), ("", None), (True, None), (False, None), ("junk", None),
    (0, None), (-5, None), (1.8e12, 1.8e12), ("1790000000", 1790000000.0),
    (1790000000, 1790000000.0), ("2026-10-03T00:00:00Z", 1790985600.0),
    ("2026-10-03T00:00:00+00:00", 1790985600.0)])
def test_parse_resets_at(v, want):
    assert rl.parse_resets_at(v) == want


@pytest.mark.parametrize("v,want", [
    (0.73, 73.0), (73, 73.0), (1, 100.0), (0, 0.0), (100, 100.0), (-1, None),
    (101, None), ("x", None), (None, None), (True, None)])
def test_normalize_pct(v, want):
    assert rl.normalize_pct(v) == want


def test_overage_status_is_not_the_signal():
    rows = [row("five_hour", "allowed", NOW + 100, overage_status="rejected")]
    assert not rl.breaker_state(rows, NOW).paused


def test_rejected_pauses_until_reset_plus_margin():
    b = rl.breaker_state([row("seven_day", "rejected", NOW + 3600)], NOW)
    assert b.paused and b.until == NOW + 3600 + rl.RESET_MARGIN_S
    assert b.types == ["seven_day"]


def test_reset_shapes():
    rej = lambda r, upd=NOW: rl.breaker_state([row("five_hour", "rejected", r, upd)], NOW)
    # null / missing reset: short default from updated_at, then lets go
    assert rej(None).until == NOW + rl.DEFAULT_PAUSE_S
    assert not rej(None, NOW - 1000).paused
    # a past reset is not usable either
    assert rej(NOW - 5).until == NOW + rl.DEFAULT_PAUSE_S
    # milliseconds read as seconds are clamped
    assert rej(1.8e12).until == NOW + rl.MAX_PAUSE_S
    # ISO strings come through parse_resets_at before they are stored
    iso = rl.parse_resets_at("2027-01-01T00:00:00Z")
    assert rej(iso).paused


def test_allowed_clears_only_its_own_type():
    rows = [row("five_hour", "allowed", NOW + 100), row("seven_day", "rejected", NOW + 500)]
    b = rl.breaker_state(rows, NOW)
    assert b.paused and b.types == ["seven_day"]


def test_warning_never_pauses_and_latest_pause_wins():
    assert not rl.breaker_state([row("five_hour", "allowed_warning", NOW + 9)], NOW).paused
    rows = [row("five_hour", "rejected", NOW + 100), row("seven_day", "rejected", NOW + 900)]
    assert rl.breaker_state(rows, NOW).until == NOW + 900 + rl.RESET_MARGIN_S


def test_utilization_reads():
    f = lambda **k: rl.weekly_utilization([row("seven_day", resets=NOW + 10, **k)], NOW)
    # stored values are already percents: never scaled again
    assert f(utilization=0.73) == 0.73
    assert f(utilization=73) == 73.0
    assert f(utilization=1.0) == 1.0 and f(utilization=0.5) == 0.5
    assert f(utilization=-1) is None and f(utilization="x") is None
    rolled = [row("seven_day", resets=NOW - 1, utilization=0.9)]
    assert rl.weekly_utilization(rolled, NOW) is None
    assert rl.weekly_utilization([], NOW) is None


DDL = """CREATE TABLE rate_limit_state (rate_limit_type TEXT PRIMARY KEY, status TEXT,
 resets_at INTEGER, overage_status TEXT, is_using_overage INTEGER DEFAULT 0,
 utilization REAL, alerted_for_resets_at INTEGER,
 updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""


def with_db(tmp_path, fn):
    async def go():
        async with aiosqlite.connect(tmp_path / "x.db") as db:
            db.row_factory = aiosqlite.Row
            await db.execute(DDL)
            return await fn(db)
    return asyncio.run(go())


async def read(db):
    return {r["rate_limit_type"]: dict(r) for r in
            await db.execute_fetchall("SELECT * FROM rate_limit_state")}


def test_window_only_update_keeps_status_and_events_do_not_cross(tmp_path):
    async def go(db):
        await rl.upsert_windows(db, rl.parse_event(
            {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": NOW + 50}, NOW))
        await rl.upsert_windows(db, rl.parse_event(
            {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": NOW + 9,
             "unifiedWindows": {"seven_day": {"utilization": 0.9, "resetsAt": NOW + 50}}}, NOW))
        rows = await read(db)
        assert rows["seven_day"]["status"] == "rejected"      # not erased
        assert rows["seven_day"]["utilization"] == 90.0
        assert rows["five_hour"]["status"] == "allowed"        # not overwritten either
        # a status-less / unknown-status event never clears a live pause
        await rl.upsert_windows(db, rl.parse_event(
            {"status": "mystery", "rateLimitType": "seven_day", "resetsAt": NOW + 50}, NOW))
        await rl.upsert_windows(db, rl.parse_event({"rateLimitType": "seven_day"}, NOW))
        assert (await read(db))["seven_day"]["status"] == "rejected"
        return list((await read(db)).values())
    rows = with_db(tmp_path, go)
    assert rl.breaker_state(
        [r for r in rows], NOW).paused


def test_weekly_utilization_one_percent_reads_as_one_percent():
    # parse_event stores 0.01 as 1.0; the reader must not scale it to 100
    rows = [row("seven_day", resets=NOW + 10, utilization=1.0)]
    assert rl.weekly_utilization(rows, NOW) == 1.0


def test_weekly_utilization_half_percent_reads_as_stored():
    rows = [row("seven_day", resets=NOW + 10, utilization=0.5)]
    assert rl.weekly_utilization(rows, NOW) == 0.5


@pytest.mark.parametrize("v,want", [
    (1.0, 1.0), (0.5, 0.5), (0, 0.0), (73, 73.0), (100, 100.0),
    (-1, None), (101, None), ("x", None), (None, None), (True, None)])
def test_stored_pct_never_rescales(v, want):
    assert rl.stored_pct(v) == want


def test_round_trip_one_percent_weekly(tmp_path):
    info = {"unifiedWindows": {"seven_day": {"utilization": 0.01, "resetsAt": NOW + 3600}}}

    async def go(db):
        await rl.upsert_windows(db, rl.parse_event(info, NOW), NOW)
        return rl.weekly_utilization(list((await read(db)).values()), NOW)
    assert with_db(tmp_path, go) == 1.0
