"""Relay routing policy: which shard gets a Discord message.

Pure and stdlib only. The relay supplies the registry, the channel's name
(None when the channel is not in channels.json), the mentioned agent id (if
any) and whether the author is a bot; this module decides.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Route:
    shard: str
    agent: str
    reason: str  # "mention" | "channel" | "primary_fallback"


def route_message(registry, channel_name, mentioned_agent, is_bot,
                  channel_opt_out=False, parent_channel_name=None,
                  mention_only=False) -> Optional[Route]:
    """Decide which shard, if any, receives a message.

    `mention_only` (a channel's `"mention_only": true`) routes on an explicit
    mention and nothing else: no channel ownership, no primary fallback. It is
    checked after the mention rule, so a mention still routes, and it wins over
    both the channel's owner and the primary fallback.
    """
    # 1. Not in channels.json: never routed, mention or not (B24). A thread has
    # its own id, never listed: it resolves through its parent's name first, so
    # a thread under an unlisted parent is still refused.
    if channel_name is None:
        channel_name = parent_channel_name
    if channel_name is None:
        return None

    # 2. A mention of a known agent.
    if mentioned_agent and mentioned_agent in registry.ids():
        owner = registry.shard_for_channel(channel_name)
        if owner is not None and owner.agent == mentioned_agent:
            return Route(owner.id, owner.agent, "mention")
        theirs = registry.shards_of(mentioned_agent)
        if theirs:
            return Route(theirs[0].id, theirs[0].agent, "mention")

    # 2b. A mention_only channel answers mentions and nothing else.
    if mention_only:
        return None

    # 3. A bot never routes on a channel default.
    if is_bot:
        return None

    # 4. The shard that owns the channel.
    owner = registry.shard_for_channel(channel_name)
    if owner is not None:
        return Route(owner.id, owner.agent, "channel")

    # 5. Unowned channel: the primary agent's first shard.
    if not channel_opt_out:
        try:
            primary = registry.primary()
            first = registry.shards_of(primary.id)
        except Exception:
            return None
        if first:
            return Route(first[0].id, first[0].agent, "primary_fallback")

    # 6.
    return None
