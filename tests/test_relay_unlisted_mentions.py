"""`"unlisted_channels": "mention_only"` in channels.json (B24 opt-in).

Reuses the stub-message fixtures from test_relay_routing.py.
"""

import json
import logging

from test_relay_routing import (  # noqa: F401  (fixtures + helpers)
    UNLISTED, Msg, adapter, channels_json, deliver, mention_b, relay, workspace,
)


def configure(relay, workspace, **top):
    cfg = channels_json()
    cfg.update(top)
    (workspace / "config" / "channels.json").write_text(json.dumps(cfg))
    relay.load_config()


def test_opt_in_human_mention_routes_once(adapter, relay, workspace):
    configure(relay, workspace, unlisted_channels="mention_only")
    deliver(adapter, Msg(UNLISTED, "<@222> hi", mentions=mention_b()))
    assert [(r.shard, r.reason) for r in adapter.routed] == [("b", "mention_unlisted")]


def test_opt_in_plain_message_does_not_route(adapter, relay, workspace):
    configure(relay, workspace, unlisted_channels="mention_only")
    deliver(adapter, Msg(UNLISTED, "just talking"))
    assert adapter.routed == []


def test_opt_in_foreign_bot_mention_is_refused(adapter, relay, workspace):
    configure(relay, workspace, unlisted_channels="mention_only")
    deliver(adapter, Msg(UNLISTED, "<@222> hi", author=999, bot=True, mentions=mention_b()))
    assert adapter.routed == []


def test_key_absent_mention_does_not_route(adapter, relay, workspace):
    configure(relay, workspace)
    deliver(adapter, Msg(UNLISTED, "<@222> hi", mentions=mention_b()))
    assert adapter.routed == []


def test_ignore_value_mention_does_not_route(adapter, relay, workspace):
    configure(relay, workspace, unlisted_channels="ignore")
    deliver(adapter, Msg(UNLISTED, "<@222> hi", mentions=mention_b()))
    assert adapter.routed == []


def test_bogus_value_acts_as_ignore_and_logs_one_error(adapter, relay, workspace, caplog):
    with caplog.at_level(logging.ERROR):
        configure(relay, workspace, unlisted_channels="bogus")
        deliver(adapter, Msg(UNLISTED, "<@222> hi", mentions=mention_b()))
    assert adapter.routed == []
    assert caplog.text.count("unlisted_channels='bogus' is not supported") == 1
