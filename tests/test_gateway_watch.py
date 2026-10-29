"""Gateway outage watch: lib/gateway_watch.py and its wiring in bin/relay.py."""

import asyncio
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import gateway_watch as gw  # noqa: E402

RELAY_PATH = Path(__file__).parent.parent / "bin" / "relay.py"


def kinds(alerts):
    return [a.kind for a in alerts]


def test_outage_alert_fires_once_after_threshold():
    w = gw.GatewayWatch("home", 300, now=0, start_down=False)
    w.down(100)
    assert w.tick(399) == []
    assert kinds(w.tick(400)) == ["outage"]
    assert "unreachable for 5 min" in w.pending[0].text
    w.delivered(w.pending[0])
    assert w.tick(5000) == []          # still down: no second alert


def test_undelivered_alert_stays_pending_and_is_not_duplicated():
    w = gw.GatewayWatch("home", 60, now=0, start_down=False)
    w.down(0)
    assert kinds(w.tick(61)) == ["outage"]
    assert kinds(w.tick(120)) == ["outage"]      # same pending alert, retried
    assert len(w.pending) == 1


def test_recovery_line_only_after_an_outage_alert():
    w = gw.GatewayWatch("home", 300, now=0, start_down=False)
    w.down(10)
    w.up(60)                                      # blip under the threshold
    assert w.tick(61) == []
    w.down(100)
    w.tick(500)
    w.delivered(w.pending[0])
    w.up(700)
    assert kinds(w.tick(701)) == ["recovery"]
    assert "back after 10 min" in w.pending[0].text
    w.delivered(w.pending[0])
    w.down(800)                                   # a new outage may alert again
    assert kinds(w.tick(1200)) == ["outage"]


def test_boot_that_never_connects_counts_as_down():
    w = gw.GatewayWatch("home", 300, now=1000)
    assert kinds(w.tick(1300)) == ["outage"]


def test_dead_letter_rise_alerts_once_and_first_reading_is_the_baseline():
    w = gw.GatewayWatch("home", 300, now=0, start_down=False)
    assert w.tick(1, dead_letters=4) == []        # old dead rows are not news
    assert w.tick(2, dead_letters=4) == []
    assert kinds(w.tick(3, dead_letters=6)) == ["dead"]
    assert "2 Discord post(s) gave up" in w.pending[0].text
    w.delivered(w.pending[0])
    assert w.tick(4, dead_letters=6) == []
    assert w.tick(5, dead_letters=None) == []     # unreadable: no change
    assert w.tick(6, dead_letters=2) == []        # purge lowers the bar
    assert kinds(w.tick(7, dead_letters=3)) == ["dead"]


def test_dead_letter_count_reads_the_outbox_read_only(tmp_path):
    db = tmp_path / "outbox.db"
    assert gw.dead_letter_count(db) is None
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE outbox (status TEXT)")
    conn.executemany("INSERT INTO outbox VALUES (?)", [("dead",), ("dead",), ("pending",)])
    conn.commit()
    conn.close()
    assert gw.dead_letter_count(db) == 2
    db.write_bytes(b"not sqlite")
    assert gw.dead_letter_count(db) is None


def test_webhook_rejects_non_http_urls_and_survives_errors():
    assert gw.post_webhook("file:///etc/passwd", "x") is False
    assert gw.post_webhook("http://127.0.0.1:9/none", "x", timeout=1) is False


# --- relay wiring -----------------------------------------------------------

discord = pytest.importorskip("discord", reason="relay.py imports discord.py")


@pytest.fixture(scope="module")
def relay(tmp_path_factory):
    ws = tmp_path_factory.mktemp("workspace")
    (ws / "logs").mkdir()
    prev = os.environ.get("WORKSPACE_ROOT")
    os.environ["WORKSPACE_ROOT"] = str(ws)
    try:
        spec = importlib.util.spec_from_file_location("relay_gateway_watch_under_test", RELAY_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["relay_gateway_watch_under_test"] = mod
        spec.loader.exec_module(mod)
    finally:
        if prev is None:
            os.environ.pop("WORKSPACE_ROOT", None)
        else:
            os.environ["WORKSPACE_ROOT"] = prev
    return mod


def test_relay_gateway_events_drive_the_watch_and_the_alert_leaves_by_webhook(relay, monkeypatch):
    sent = []
    monkeypatch.setattr(relay, "OUTAGE_ALERT_WEBHOOK_URL", "http://hook.invalid/x")
    monkeypatch.setattr(relay.gateway_watch, "post_webhook", lambda url, text, timeout=10.0: sent.append((url, text)) or True)
    monkeypatch.setattr(relay.gateway_watch, "dead_letter_count", lambda p: None)
    clock = [1000.0]
    monkeypatch.setattr(relay.time, "time", lambda: clock[0])

    async def go():
        a = relay.DiscordAdapter()
        a.gateway_watch = gw.GatewayWatch("home", 300, now=clock[0], start_down=False)
        await a.on_disconnect()
        clock[0] += 301
        await a.outage_tick()
        await a.outage_tick()                    # no second post while still down
        assert len(sent) == 1 and "unreachable" in sent[0][1]
        clock[0] += 60
        await a.on_resumed()
        await a.outage_tick()
        assert len(sent) == 2 and "back after" in sent[1][1]
        await a.close()

    asyncio.run(go())
    assert all(u == "http://hook.invalid/x" for u, _ in sent)


def test_relay_without_a_webhook_logs_and_does_not_retry_forever(relay, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(relay, "OUTAGE_ALERT_WEBHOOK_URL", "")
    with caplog.at_level(logging.WARNING):
        a = relay.DiscordAdapter()
        assert a.outage_send("down") is True
    assert any("no OUTAGE_ALERT_WEBHOOK_URL" in r.message for r in caplog.records)
