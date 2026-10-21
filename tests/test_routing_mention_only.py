"""Pure routing for a `"mention_only": true` channel (lib/routing.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import registry  # noqa: E402
from routing import Route, route_message  # noqa: E402

AGENTS = {
    "a": {"name": "a", "role": "primary", "shards": [
        {"id": "a", "channels": ["general"]},
        {"id": "a-2", "channels": ["ops"]}]},
    "b": {"name": "b", "role": "custom", "shards": [
        {"id": "b", "channels": ["b-room"]}]},
    "m": {"name": "m", "role": "monitor"},
}


def reg():
    return registry.parse_registry({"version": 2, "agents": AGENTS})


def mo(chan, mention=None, bot=False):
    return route_message(reg(), chan, mention, bot, mention_only=True)


def test_plain_message_in_an_owned_channel_is_silent():
    assert mo("ops") is None


def test_plain_message_with_no_owner_gets_no_primary_fallback():
    assert mo("lobby") is None


def test_own_agent_mention_routes_to_the_owning_shard():
    assert mo("ops", "a") == Route("a-2", "a", "mention")


def test_other_agent_mention_routes_to_its_shard():
    assert mo("ops", "b") == Route("b", "b", "mention")


def test_bot_mention_routes():
    assert mo("ops", "b", bot=True) == Route("b", "b", "mention")


def test_unknown_mention_is_silent():
    assert mo("ops", "zzz") is None


def test_unlisted_channel_stays_silent_with_a_mention():
    assert route_message(reg(), None, "b", False, mention_only=True) is None


def test_default_leaves_channel_routing_unchanged():
    assert route_message(reg(), "ops", None, False) == Route("a-2", "a", "channel")
    assert route_message(reg(), "lobby", None, False) == Route("a", "a", "primary_fallback")
    assert route_message(reg(), "ops", None, False, mention_only=False) == \
        Route("a-2", "a", "channel")


def um(mention=None, bot=False):
    return route_message(reg(), None, mention, bot, unlisted_mentions=True)


def test_unlisted_opt_in_routes_a_mention():
    assert um("b") == Route("b", "b", "mention_unlisted")


def test_unlisted_opt_in_without_a_mention_is_silent():
    assert um() is None


def test_unlisted_opt_in_unknown_mention_is_silent():
    assert um("zzz") is None


def test_unlisted_opt_in_bot_filtering_is_not_done_here():
    assert um("b", bot=True) == Route("b", "b", "mention_unlisted")
