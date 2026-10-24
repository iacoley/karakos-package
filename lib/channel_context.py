"""Recent channel messages that an agent's turn would otherwise not carry (#276).

The relay hands an agent only the message that woke it. In a channel where the
agent is addressed on purpose (`mention_only`) the messages around that mention
never reach it, so "take this feedback" or "that" has no referent. This keeps a
small in-memory ring per channel, fed by every message the relay sees, and builds
the block the relay prepends: the messages that arrived after the agent's own
last post there, oldest first, one line each.

In memory only, never logged or written. Not shared with the reply gate's ring,
which is fed only for gated channels and only by their human messages.
"""
from __future__ import annotations

import re
import time
from collections import deque
from datetime import datetime, timezone

try:
    import identity
except ImportError:  # imported as `lib.channel_context`
    from lib import identity

RING = 20
TEXT_CAP = 300
DEFAULT_LIMIT = 8
MAX_LIMIT = 20
MORE = "[context] (earlier messages not shown; call discord history for more)"


def context_limit(channel_config) -> int:
    """`context_messages` from the channel's own config: default 8, 0 to 20, 0 is off.
    Anything that is not a whole number in range falls back to the default."""
    raw = (channel_config or {}).get("context_messages", DEFAULT_LIMIT)
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= MAX_LIMIT:
        return DEFAULT_LIMIT
    return raw


def _one_line(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


class ChannelContext:
    def __init__(self):
        self._ring = {}   # channel id -> deque of (message id, author, text, own, ts)

    def note(self, channel_id, message_id, author, text, own=False, ts=None,
             author_id=None, visible=True):
        """Record one message. `own` marks a post by the agent itself; `author_id`
        lets `block` decide that per agent (several agents, several bot accounts).
        `visible=False` keeps a message the relay refused to deliver (a bot it
        does not route) in the ring, for the reset only: it is never shown."""
        ring = self._ring.setdefault(int(channel_id), deque(maxlen=RING))
        ring.append((str(message_id), str(author or ""), _one_line(text)[:TEXT_CAP],
                     bool(own), ts if ts is not None else time.time(),
                     None if author_id is None else str(author_id), bool(visible)))

    def block(self, channel_id, trigger_id, limit=DEFAULT_LIMIT, own_ids=()) -> str:
        """The context lines for a delivery, or "" when there is nothing to add.

        Entries before the triggering message and after the last own post, at
        most `limit` of them, oldest first. A closing note says when older
        messages were left out, either by `limit` or because the ring is full and
        holds no own post to start from. `own_ids` are the bot account ids this
        agent posts as; a post by one of them is the agent's own."""
        if limit <= 0:
            return ""
        own_ids = {str(i) for i in own_ids}
        entries = [(m, a, t, o or (aid in own_ids), ts, vis, aid)
                   for m, a, t, o, ts, aid, vis in self._ring.get(int(channel_id), ())]
        ids = [e[0] for e in entries]
        if str(trigger_id) in ids:
            entries = entries[:ids.index(str(trigger_id))]
        start = 0
        for i in range(len(entries) - 1, -1, -1):
            if entries[i][3]:
                start = i + 1
                break
        else:
            start = 0
        window = [e for e in entries[start:] if e[5]]
        cut = (start == 0 and len(entries) >= RING and not any(e[3] for e in entries)) \
            or len(window) > limit
        window = window[-limit:]
        if not window:
            return ""
        lines = [MORE] if cut else []
        for _mid, author, text, _own, ts, _vis, aid in window:
            hhmm = datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")
            lines.append(f"[context] {identity.label(author, aid)} ({hhmm}): {text}")
        return "\n".join(lines)
