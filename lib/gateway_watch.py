"""Gateway outage watch: say so, off Discord, when the relay cannot reach Discord.

An agent behind a dead network link cannot tell Discord is unreachable: no turns
arrive, so nothing runs to notice. The relay is the one process that holds the
gateway connection, so it is the one that can. This module is the decision part,
pure and clock-injected so it tests without a network or a Discord client.

Three triggers, each producing one message per episode:
  * `outage`   the gateway has been down for longer than `threshold_s`
  * `dead`     the outbox's dead-letter count rose since the last alert
  * `recovery` the gateway is back after an `outage` message went out

An alert leaves by a sink that is not Discord: an HTTP webhook (`webhook_url`;
ntfy, Gotify and Slack-style endpoints all accept a text POST). A message that
fails to deliver stays pending and is retried on the next tick, so it is not
lost and not repeated. With no sink configured the relay only logs the message.

A webhook cannot help when the whole uplink is down; it covers the common case
of Discord, or the path to it, failing while another route still works. The
recovery line is delivered once the sink is reachable again.
"""
from __future__ import annotations

import sqlite3
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

DEFAULT_THRESHOLD_S = 300.0


@dataclass
class Alert:
    kind: str   # "outage" | "dead" | "recovery"
    text: str


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


class GatewayWatch:
    """State machine. Feed it `down()`/`up()` from gateway events and `tick()`
    from a timer; `tick()` returns the alerts newly due, and `pending` holds any
    that have not been confirmed delivered."""

    def __init__(self, name: str, threshold_s: float = DEFAULT_THRESHOLD_S, *, now: float,
                 start_down: bool = True):
        self.name = name
        self.threshold_s = threshold_s
        # A relay that has not yet logged in counts as down from process start, so
        # a boot that never connects is reported too.
        self.down_since: Optional[float] = now if start_down else None
        self.outage_alerted = False
        self.dead_alerted: Optional[int] = None  # baseline: first reading, no alert
        self.pending: List[Alert] = []

    def down(self, now: float) -> None:
        if self.down_since is None:
            self.down_since = now

    def up(self, now: float) -> None:
        if self.down_since is None:
            return
        if self.outage_alerted:
            self.pending.append(Alert(
                "recovery",
                f"{self.name}: Discord gateway is back after {_fmt_duration(now - self.down_since)}."))
        self.down_since = None
        self.outage_alerted = False

    def tick(self, now: float, dead_letters: Optional[int] = None) -> List[Alert]:
        """Decide what is due now. `dead_letters` is None when it cannot be read."""
        if (self.down_since is not None and not self.outage_alerted
                and now - self.down_since >= self.threshold_s):
            self.outage_alerted = True
            self.pending.append(Alert(
                "outage",
                f"{self.name}: Discord gateway has been unreachable for "
                f"{_fmt_duration(now - self.down_since)}. Agents cannot be reached "
                f"or answer on Discord until it is back."))
        if dead_letters is not None:
            if self.dead_alerted is not None and dead_letters > self.dead_alerted:
                self.pending.append(Alert(
                    "dead",
                    f"{self.name}: {dead_letters - self.dead_alerted} Discord post(s) gave up "
                    f"after retries ({dead_letters} dead in the outbox)."))
            # Track the latest count, so a purge or a retry lowers the bar again.
            self.dead_alerted = dead_letters
        return list(self.pending)

    def delivered(self, alert: Alert) -> None:
        if alert in self.pending:
            self.pending.remove(alert)


def dead_letter_count(outbox_db: Path) -> Optional[int]:
    """Dead rows in the outbox, read-only; None if the file is absent or unreadable."""
    path = Path(outbox_db)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            return conn.execute("SELECT COUNT(*) FROM outbox WHERE status='dead'").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def post_webhook(url: str, text: str, timeout: float = 10.0) -> bool:
    """POST `text` to a webhook; True on a 2xx. Never raises."""
    if not url.lower().startswith(("http://", "https://")):
        return False
    req = urllib.request.Request(url, data=text.encode("utf-8"), method="POST",
                                 headers={"Content-Type": "text/plain; charset=utf-8",
                                          "Title": "Karakos relay"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except Exception:
        return False
