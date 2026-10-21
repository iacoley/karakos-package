"""Account-level rate-limit state: parsing, upsert and the breaker (spec 2.7).

The CLI reports headroom in-band as `rate_limit_event`. A limit is a fact about
the *account*, so `rate_limit_state` is keyed by `rate_limit_type`
(`five_hour`, `seven_day`, ...), never by agent or shard.

stdlib only. Pure except `upsert_windows`, which writes through an aiosqlite
connection the caller owns.
"""
import math
import re
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

DEFAULT_PAUSE_S = 300      # a rejection with no usable reset: pause briefly, let the next event decide
MAX_PAUSE_S = 21600        # clamp any single pause (a ms value read as seconds)
RESET_MARGIN_S = 30

KNOWN_STATUSES = frozenset({"allowed", "allowed_warning", "rejected"})
UNKNOWN_TYPE = "unknown"

BreakerState = namedtuple("BreakerState", "paused until types")


@dataclass
class WindowUpdate:
    type: str
    status: Optional[str] = None
    resets_at: Optional[float] = None
    overage_status: Optional[str] = None
    is_using_overage: Optional[bool] = None
    utilization: Optional[float] = None


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    return None


def parse_resets_at(v) -> Optional[float]:
    """Epoch seconds from a number, numeric string or ISO string. Missing, null,
    empty, booleans, junk and non-positive values are None. A past time is
    returned as is (the caller decides what a past reset means)."""
    if v is None or isinstance(v, bool):
        return None
    n = _num(v)
    if n is not None:
        return n if n > 0 else None
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    try:
        f = float(s)
        return f if math.isfinite(f) and f > 0 else None
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def normalize_pct(utilization) -> Optional[float]:
    """0..1 is a fraction (x100); above 1 up to 100 is already a percentage;
    anything else (negative, over 100, non-numeric, bool) is None."""
    n = _num(utilization)
    if n is None or n < 0 or n > 100:
        return None
    return n * 100.0 if n <= 1 else n


def stored_pct(utilization) -> Optional[float]:
    """A `rate_limit_state.utilization` value. parse_event already stored it as
    a percent (0..100), so it is never scaled again; anything else is None."""
    n = _num(utilization)
    if n is None or n < 0 or n > 100:
        return None
    return float(n)


def _type_name(v) -> str:
    return v.strip() if isinstance(v, str) and v.strip() else UNKNOWN_TYPE


def parse_event(info, now=None) -> List[WindowUpdate]:
    """One update per window named by a `rate_limit_info` dict. The top-level
    form (`rateLimitType` + `status`) and the `unifiedWindows` entries merge by
    type; only the top-level one carries a status."""
    if not isinstance(info, dict):
        return []
    status = info.get("status")
    status = status.strip().lower() if isinstance(status, str) else None
    top = WindowUpdate(
        type=_type_name(info.get("rateLimitType")),
        status=status or None,
        resets_at=parse_resets_at(info.get("resetsAt")),
        overage_status=info.get("overageStatus")
        if isinstance(info.get("overageStatus"), str) else None,
        is_using_overage=bool(info.get("isUsingOverage"))
        if "isUsingOverage" in info else None)
    merged = {top.type: top}
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        for name, w in windows.items():
            if not isinstance(w, dict):
                continue
            t = _type_name(name)
            util = normalize_pct(w.get("utilization"))
            reset = parse_resets_at(w.get("resetsAt"))
            cur = merged.get(t)
            if cur is None:
                merged[t] = WindowUpdate(type=t, resets_at=reset, utilization=util)
            else:
                if cur.utilization is None:
                    cur.utilization = util
                if cur.resets_at is None:
                    cur.resets_at = reset
    return list(merged.values())


_UPSERT = """
INSERT INTO rate_limit_state
    (rate_limit_type, status, resets_at, overage_status, is_using_overage,
     utilization, alerted_for_resets_at, updated_at)
VALUES (?, ?, ?, ?, COALESCE(?, 0), ?, NULL, CURRENT_TIMESTAMP)
ON CONFLICT(rate_limit_type) DO UPDATE SET
    status = COALESCE(excluded.status, status),
    resets_at = CASE WHEN excluded.resets_at IS NULL AND ? THEN resets_at
                     ELSE excluded.resets_at END,
    overage_status = COALESCE(excluded.overage_status, overage_status),
    is_using_overage = COALESCE(?, is_using_overage),
    utilization = COALESCE(excluded.utilization, utilization),
    updated_at = CURRENT_TIMESTAMP
"""


async def upsert_windows(db, updates, now=None) -> None:
    """Write each update. A window-only update never erases a status and a
    status-less (or unknown-status) event never clears one; `resets_at` follows
    the event, except that a window-only update without a reset keeps the old."""
    for u in updates:
        status = u.status if u.status in KNOWN_STATUSES else None
        reset = int(u.resets_at) if u.resets_at is not None else None
        using = None if u.is_using_overage is None else int(bool(u.is_using_overage))
        window_only = u.status is None and u.overage_status is None and using is None
        await db.execute(_UPSERT, (
            u.type, status, reset, u.overage_status, using, u.utilization,
            1 if window_only else 0, using))
    await db.commit()


def _field(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _epoch(v) -> Optional[float]:
    n = _num(v)
    if n is not None:
        return n
    if isinstance(v, str) and v.strip():
        try:
            dt = datetime.strptime(v.strip()[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def _pause_end(row, now: float) -> Optional[float]:
    """When this row's rejection stops pausing; None when it is not rejecting."""
    if _field(row, "status") != "rejected":
        return None
    reset = parse_resets_at(_field(row, "resets_at"))
    if reset is not None and reset > now:
        end = min(reset + RESET_MARGIN_S, now + MAX_PAUSE_S)
    else:
        stamped = _epoch(_field(row, "updated_at"))
        if stamped is None:
            return None  # unreadable: fail open
        end = stamped + DEFAULT_PAUSE_S
    return end if end > now else None


def breaker_state(rows, now) -> BreakerState:
    """Paused while any window type is rejecting. `overage_status` is not the
    signal; `allowed` for a type clears only that type."""
    ends = {}
    for r in rows or ():
        end = _pause_end(r, now)
        if end is not None:
            ends[_type_name(_field(r, "rate_limit_type"))] = end
    if not ends:
        return BreakerState(False, None, [])
    return BreakerState(True, max(ends.values()), sorted(ends))


def weekly_utilization(rows, now) -> Optional[float]:
    """The `seven_day` reading as a percentage, or None when it is unknown (no
    row, no utilization, or its window has rolled over — unknown, not zero)."""
    for r in rows or ():
        if _field(r, "rate_limit_type") != "seven_day":
            continue
        reset = parse_resets_at(_field(r, "resets_at"))
        if reset is None or reset <= now:
            return None
        return stored_pct(_field(r, "utilization"))
    return None
