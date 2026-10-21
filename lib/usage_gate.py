"""before_claim gate: account breaker, per-agent token budget, weekly governor
(spec 2.7).

`usage_gate_before_claim(state, shard)` is registered once at startup with
`state.hooks.register("before_claim", ...)` (see `install`). It returns the
first reason to defer the drain, else None. A deferral changes nothing in the
queue or in shard states (the 2.0 contract), so every deferral also arms a
resume timer: `before_claim` has no wake of its own.

Order: breaker (every row, human included), budget (every row, human included,
evaluated on the agent so every shard of it defers together), governor
(machine-started rows only). Rows of a `role: monitor` agent are never gated:
a paused monitor could not report the pause.

Runtime state is keyed by shard id (`paused`, `resume_tasks`) or agent id
(`budget_paused_since`). stdlib only.
"""
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import msgqueue
import rate_limits
import token_budget
import usage_governor
import turn_loop

# Patched by tests to move time.
_now = time.time
RESUME_POLL_S = 30.0
BUDGET_RECHECK_S = 60
GOVERNOR_RECHECK_S = 300

_current: Optional["GateState"] = None


class GateState:
    def __init__(self):
        self.paused: Dict[str, Tuple[str, Optional[float]]] = {}   # shard -> (reason, until)
        self.resume_tasks: Dict[str, asyncio.Task] = {}            # key -> task
        self.budget_paused_since: Dict[str, float] = {}            # agent -> epoch
        self.budget_last_notice: Dict[str, float] = {}             # agent -> epoch
        self.budget_announced: set = set()                         # agents whose pause was announced
        self.notified: set = set()                                 # (shard, reason) human notices sent
        self.governor_deferred: Dict[str, bool] = {}               # job -> was deferred
        self.breaker_until: Optional[float] = None
        self.policy_broken: Optional[str] = None


def install(state) -> GateState:
    """Create the gate state on `state` and register the hook (once)."""
    global _current
    gate = getattr(state, "usage_gate", None)
    if isinstance(gate, GateState):
        return gate
    gate = GateState()
    state.usage_gate = gate
    _current = gate

    async def hook(shard):
        return await usage_gate_before_claim(state, shard)

    state.hooks.register("before_claim", hook)
    return gate


def account_paused() -> bool:
    """True while the breaker is rejecting (2.6's reset skips its handoff turn)."""
    g = _current
    return bool(g and g.breaker_until and g.breaker_until > _now())


def _srv(state, name, default=None):
    try:
        return getattr(state._server, name)
    except AttributeError:
        return default


def _workspace(state) -> Path:
    return Path(_srv(state, "WORKSPACE_ROOT", "."))


async def read_rate_rows(state) -> list:
    try:
        return list(await state.db.execute_fetchall("SELECT * FROM rate_limit_state"))
    except Exception as e:  # noqa: BLE001 — fail open
        state.log.debug(f"rate_limit_state unreadable: {type(e).__name__}: {e}")
        return []


def update_breaker(state, rows) -> None:
    """Refresh the cached breaker end `account_paused` reads."""
    gate = getattr(state, "usage_gate", None)
    if isinstance(gate, GateState):
        b = rate_limits.breaker_state(rows, _now())
        gate.breaker_until = b.until if b.paused else None


def _hhmm(epoch) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M UTC")


def _notice(what: str, until) -> str:
    return f"⏸️ {what} — your message is held and will be processed after {_hhmm(until)}."


def _agent_shards(state, agent):
    return [s for s in state.shard_ids() if state.agent_of(s) == agent]


def schedule_resume(state, key: str, when: float, shards) -> None:
    """Arm one task per key that sleeps to `when + 1` and drains every shard in
    `shards`. An armed timer at or before `when` is kept (an early wake just
    re-defers and re-arms); a later one is replaced."""
    gate = state.usage_gate
    shards = list(shards)
    existing = gate.resume_tasks.get(key)
    if existing and not existing.done():
        if existing.when <= when:
            return
        existing.cancel()

    async def wake():
        while True:
            remaining = when + 1 - _now()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, RESUME_POLL_S))
        if gate.resume_tasks.get(key) is task:
            gate.resume_tasks.pop(key, None)
        await asyncio.gather(
            *(turn_loop.drain_shard(state, s) for s in shards), return_exceptions=True)

    task = asyncio.create_task(wake())
    task.when = when
    gate.resume_tasks[key] = task


async def _human_notice(state, shard, reason_key, what, until, claimable):
    gate = state.usage_gate
    key = (shard, reason_key)
    if key in gate.notified:
        return
    human = next((r for r in claimable if not r["is_bot"]
                  and str(r["channel_id"]) not in ("", "0")), None)
    if human is None:
        return
    gate.notified.add(key)
    try:
        await state.post_to_discord(shard, human["channel_id"], _notice(what, until))
    except Exception as e:  # noqa: BLE001
        state.log.warning(f"usage notice failed for {shard}: {e}")


async def _enter_pause(state, shard, reason, until, detail, claimable):
    gate = state.usage_gate
    gate.paused[shard] = (reason, until)
    # A caller must not wait for queue expiry on a paused callee. Cheap when
    # there is no queued call row, so it runs on every deferral: a call that
    # lands during a pause is answered on the next evaluation.
    # POST /hive/call answers 409 callee_paused and pick_callee skips this
    # shard while it is in `paused` (agent-server `_hive_pick`).
    try:
        await msgqueue.fail_calls(state.db, shard, "callee_paused", detail)
    except Exception as e:  # noqa: BLE001
        state.log.debug(f"fail_calls skipped: {e}")


def _alert_channel(state) -> str:
    ch = _srv(state, "RATE_LIMIT_ALERT_CHANNEL_ID", "") or ""
    return "" if ch in ("", "0") else ch


async def _post_alert(state, agent, text):
    ch = _alert_channel(state)
    if ch:
        try:
            await state.post_to_discord(agent, ch, text)
        except Exception as e:  # noqa: BLE001
            state.log.warning(f"budget notice failed for {agent}: {e}")


async def _budget_eval(state, shard, now):
    """-> (paused, until, budget, used, min_pause). Evaluated on the agent."""
    gate = state.usage_gate
    agent = state.agent_of(shard)
    cfg = state.cfg(shard)
    budget = cfg.get("token_budget_4h")
    if not budget:
        return False, None, None, 0, None
    min_pause = cfg.get("token_budget_min_pause_s") or token_budget.TOKEN_BUDGET_MIN_PAUSE_S
    used = await token_budget.usage_in_window(
        state.db, _agent_shards(state, agent), token_budget.TOKEN_BUDGET_WINDOW_S, now,
        use_cache=agent not in gate.budget_paused_since)
    # Everything below is synchronous: two shards of one agent evaluating
    # concurrently cannot both see "not paused" and both announce.
    prev = gate.budget_paused_since.get(agent)
    paused, since = token_budget.pause_state(now, used, budget, prev, min_pause)
    if paused and prev is None:
        gate.budget_paused_since[agent] = since
        if token_budget.should_announce_pause(now, gate.budget_last_notice.get(agent)):
            gate.budget_last_notice[agent] = now
            gate.budget_announced.add(agent)
            asyncio.ensure_future(_post_alert(
                state, agent,
                f"⏸️ `{agent}` paused: token budget reached ({used:,} of {budget:,} "
                f"tokens in 4h)."))
    elif not paused and prev is not None:
        gate.budget_paused_since.pop(agent, None)
        if agent in gate.budget_announced:
            gate.budget_announced.discard(agent)
            asyncio.ensure_future(_post_alert(
                state, agent, f"▶️ `{agent}` resumed: token usage is under budget."))
    until = None
    if paused:
        until = max(since + min_pause, now + BUDGET_RECHECK_S)
    return paused, until, budget, used, min_pause


async def usage_gate_before_claim(state, shard) -> Optional[str]:
    gate = state.usage_gate
    agent = state.agent_of(shard)
    roles = _srv(state, "agent_roles", {}) or {}
    if roles.get(agent) == "monitor":
        return None
    now = _now()

    # 1. breaker (account-wide)
    rows = await read_rate_rows(state)
    b = rate_limits.breaker_state(rows, now)
    gate.breaker_until = b.until if b.paused else None
    if b.paused:
        claimable = await msgqueue.peek_claimable(state.db, shard, 20)
        await _enter_pause(state, shard, "breaker", b.until, "account usage limit",
                           claimable)
        await _human_notice(state, shard, "breaker", "account usage limit", b.until,
                            claimable)
        schedule_resume(state, "breaker", b.until, state.shard_ids())
        return "breaker"

    # 2. budget (per agent)
    paused, until, budget, used, _mp = await _budget_eval(state, shard, now)
    if paused:
        claimable = await msgqueue.peek_claimable(state.db, shard, 20)
        await _enter_pause(state, shard, "budget", until, "token budget", claimable)
        await _human_notice(state, shard, "budget", "token budget", until, claimable)
        schedule_resume(state, f"budget:{agent}", until, _agent_shards(state, agent))
        return "budget"

    # 3. governor (machine-started rows only)
    reason = await _governor_eval(state, shard, rows, now)
    if reason:
        return reason

    if shard in gate.paused:
        gate.paused.pop(shard, None)
        gate.notified = {k for k in gate.notified if k[0] != shard}
    return None


async def _governor_eval(state, shard, rate_rows, now) -> Optional[str]:
    gate = state.usage_gate
    ws = _workspace(state)
    policy = usage_governor.Policy.load(ws / "config" / "governor.yaml")
    gate.policy_broken = policy.broken
    claimable = await msgqueue.peek_claimable(state.db, shard, 20)
    if not claimable or not all(usage_governor.is_machine_started(r) for r in claimable):
        return None
    pct = rate_limits.weekly_utilization(rate_rows, now)
    decisions = []
    for r in claimable:
        job = usage_governor.job_name(r)
        d = usage_governor.decide(job, pct, policy, gate.governor_deferred.get(job, False))
        decisions.append(d)
        usage_governor.log_decision(ws / "logs" / "governor.jsonl", d, shard, now)
    for d in decisions:
        if d.reason not in ("never-gated", "disabled", "fail-open: usage unreadable"):
            gate.governor_deferred[d.job] = d.decision == "defer"
    if not all(d.decision == "defer" for d in decisions):
        return None
    gate.paused[shard] = ("governor", None)
    try:
        await usage_governor.age_out(state.db, shard, policy, now)
    except Exception as e:  # noqa: BLE001
        state.log.debug(f"governor age_out skipped: {e}")
    wake = now + GOVERNOR_RECHECK_S
    for r in rate_rows:
        if r["rate_limit_type"] == "seven_day":
            reset = rate_limits.parse_resets_at(r["resets_at"])
            if reset is not None and now < reset < wake:
                wake = reset
    schedule_resume(state, "governor", wake, state.shard_ids())
    return "governor"


async def usage_report(state) -> dict:
    """Additive /usage sections: windows, breaker, budgets, governor."""
    now = _now()
    rows = await read_rate_rows(state)
    b = rate_limits.breaker_state(rows, now)
    gate = getattr(state, "usage_gate", None)
    windows = {}
    for r in rows:
        t = r["rate_limit_type"]
        pct = rate_limits.stored_pct(r["utilization"])
        windows[t] = {
            "status": r["status"], "resets_at": r["resets_at"],
            "utilization_pct": pct,
            "updated_at": r["updated_at"],
        }
    budgets = {}
    seen = set()
    for shard in state.shard_ids():
        agent = state.agent_of(shard)
        if agent in seen:
            continue
        seen.add(agent)
        cfg = state.cfg(shard)
        budget = cfg.get("token_budget_4h")
        if not budget:
            continue
        used = await token_budget.usage_in_window(
            state.db, _agent_shards(state, agent), token_budget.TOKEN_BUDGET_WINDOW_S, now)
        since = gate.budget_paused_since.get(agent) if gate else None
        mp = cfg.get("token_budget_min_pause_s") or token_budget.TOKEN_BUDGET_MIN_PAUSE_S
        budgets[agent] = {"used": used, "budget": budget, "paused_since": since,
                          "until": (since + mp) if since is not None else None}
    ws = _workspace(state)
    policy = usage_governor.Policy.load(ws / "config" / "governor.yaml")
    return {
        "windows": windows,
        "breaker": {"paused": b.paused, "until": b.until, "types": b.types},
        "budgets": budgets,
        "governor": {"weekly_pct": rate_limits.weekly_utilization(rows, now),
                     "enabled": policy.enabled and not policy.broken,
                     "policy_broken": bool(policy.broken)},
        "_rows": rows,
    }
