"""Cross-channel send (#278): policy, rate limit and mention safety. Pure, stdlib only.

An agent may post to a channel other than its active turn's only when the
channel id is in its `send.channels` list in agents.yaml (empty by default, so
sending is off). Nothing here talks to Discord; bin/agent-server.py `POST /send`
calls these and enqueues an outbox row.

    send:
      channels: ["1234567890"]    # ids the agent may post to
      per_hour: 20                # sends per rolling hour (default 20)
      unlimited_channels: []      # ids from `channels` that skip the rate limit
"""
import re
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

DEFAULT_PER_HOUR = 20
MAX_PER_HOUR = 200
MAX_CONTENT_CHARS = 4000
WINDOW_S = 3600.0

# Discord's allowed_mentions with an empty `parse`: nothing in the text pings.
NO_MENTIONS = {"parse": []}

ZWSP = "​"
_BROADCAST_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)
_ROLE_RE = re.compile(r"<@&(\d+)>")
_SNOWFLAKE_RE = re.compile(r"\A[0-9]{15,22}\Z")


def parse_config(raw: Any) -> Tuple[List[str], int]:
    """(allowed channel ids, per-hour limit) from an agent's `send` block.
    Tolerant: a malformed block means sending is off (registry validation
    reports the error at load; this is the runtime backstop)."""
    if not isinstance(raw, dict):
        return [], DEFAULT_PER_HOUR
    ids: List[str] = []
    chans = raw.get("channels")
    if isinstance(chans, list):
        for c in chans:
            if isinstance(c, bool):
                continue
            c = str(c).strip()
            if _SNOWFLAKE_RE.match(c) and c not in ids:
                ids.append(c)
    per = raw.get("per_hour", DEFAULT_PER_HOUR)
    if isinstance(per, bool) or not isinstance(per, int) or not 1 <= per <= MAX_PER_HOUR:
        per = DEFAULT_PER_HOUR
    return ids, per


def parse_unlimited(raw: Any, allowed: List[str]) -> List[str]:
    """Ids from `send.unlimited_channels` that are also in `allowed`. The
    exemption never widens the allowlist: an id that is not allowed is dropped
    here (registry validation rejects it at load)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("unlimited_channels"), list):
        return []
    out: List[str] = []
    for c in raw["unlimited_channels"]:
        if isinstance(c, bool):
            continue
        c = str(c).strip()
        if c in allowed and c not in out:
            out.append(c)
    return out


def neutralise_mentions(text: str) -> str:
    """Break `@everyone`, `@here` and role mentions so they cannot ping even if
    allowed_mentions were ignored. User mentions are left readable."""
    text = _BROADCAST_RE.sub(lambda m: "@" + ZWSP + m.group(1), text)
    return _ROLE_RE.sub(lambda m: "<@" + ZWSP + "&" + m.group(1) + ">", text)


def check_content(content: Any) -> Optional[str]:
    """An error string, or None when `content` may be sent."""
    if not isinstance(content, str) or not content.strip():
        return "content must be a non-empty string"
    if len(content) > MAX_CONTENT_CHARS:
        return f"content is {len(content)} characters; the limit is {MAX_CONTENT_CHARS}"
    return None


class RateLimiter:
    """Rolling-window counter per agent. In memory: a restart clears it, which
    only ever errs towards allowing a few more sends."""

    def __init__(self, window_s: float = WINDOW_S):
        self.window_s = window_s
        self._hits: Dict[str, Deque[float]] = {}

    def _trim(self, agent: str, now: float) -> Deque[float]:
        q = self._hits.setdefault(agent, deque())
        while q and now - q[0] >= self.window_s:
            q.popleft()
        return q

    def allow(self, agent: str, limit: int, now: float) -> Tuple[bool, float]:
        """(allowed, seconds until a slot frees). Counts the send when allowed."""
        q = self._trim(agent, now)
        if len(q) >= limit:
            return False, max(self.window_s - (now - q[0]), 0.0)
        q.append(now)
        return True, 0.0


def audit_line(agent: str, channel_id: str, chars: int, outbox_id: str,
               exempt: bool = False) -> str:
    """One log line per cross-channel send. Length only, never the text.
    `exempt=1` marks a send that skipped the rate limit."""
    line = f"cross-channel send agent={agent} channel={channel_id} chars={chars} outbox={outbox_id}"
    return line + " exempt=1" if exempt else line
