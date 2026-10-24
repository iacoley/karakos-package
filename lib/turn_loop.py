"""The agent-server turn loop: claim a batch, run one turn, finish it.

Extracted from bin/agent-server.py (step 2.0, behaviour-neutral). Every function
takes a `shard`: the runtime key of the per-shard dicts, locks, states, queue
rows and sessions. Today a shard id is always an agent id; ServerState's
shard_ids()/agent_of()/cfg() are the one place that changes when shards arrive.

stdlib only, and no import of the server module: the server hands in a
ServerState built by make_state(), which reads the server's globals *at access
time* (so a test that rebinds ags.db or ags.post_to_discord is seen on the next
call).
"""

import asyncio
import collections
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import discord_ux
import hive
import identity
import msgqueue
import post_guard
import stealing
import steering

HOOK_NAMES = ("before_claim", "on_turn_start", "on_event", "on_turn_end")

# Steering (step 2.5). Module constants so a test can patch them.
STEER_FOLLOWON_TIMEOUT_S = 60   # a CLI-started follow-on turn must begin by then
SELF_TURN_WINDOW_S = 5          # wait for a self-started turn after a background task
CONTROL_TIMEOUT_S = 5           # wait for a control_response to an interrupt request
POST_SELF_TURNS = True          # post a self-started turn's text to the last channel

# Names read from the server module at access time. Dicts/sets are the live
# runtime state; the callables are looked up late so tests can patch them.
_SERVER_NAMES = (
    # runtime state
    "agent_processes", "agent_locks", "agent_states", "response_buffers",
    "agent_last_cost", "agent_sessions", "agent_turn_context",
    "agent_last_channel", "interrupted_agents", "typing_tasks",
    "agent_wall_strikes", "agent_hold_tasks", "agent_config",
    "db", "log", "ask_registry", "ux_threads",
    # collaborators
    "post_to_discord", "add_reaction", "ux_for_channel_id", "start_typing", "stop_typing", "write_agent_beacon",
    "post_cost_update", "mark_session_started", "update_session_tokens", "update_session_context",
    "hold_batch", "agent_hold_until", "schedule_hold_wake", "classify_wall",
    "wall_not_before", "format_attachments", "redact_for_log",
    "send_to_agent", "read_agent_response",
    "kill_agent_subprocess", "start_agent_subprocess",
    "ux_thread_cfg", "ux_create_thread", "ux_tool_streaming",
    # used by the stream reader
    "write_stream_log", "record_rate_limit_event", "write_turn_event",
    "describe_tool_call", "should_post_tool_line", "summarize_tool_call",
    "write_streaming_response", "write_partial_response",
    "usage_context_tokens", "extract_permission_denials", "THINKING_BLOCK_RE",
    # constants
    "STATUS_QUEUED", "STATUS_IN_PROGRESS", "STATUS_COMPLETE", "STATUS_CRASHED",
    "STATUS_SKIPPED", "AUTOMATED_TRAFFIC_SENTINEL", "GENERIC_TURN_ERROR",
    "TOOL_EVENT_MIN_INTERVAL", "TOOL_EVENT_MAX_PER_TURN",
)


class TurnHooks:
    """Ordered hook lists. Nothing is registered by the 2.0 refactor.

    before_claim(shard) -> Optional[str]   truthy return defers the drain
    on_turn_start(shard, batch)
    on_event(shard, event)
    on_turn_end(shard, result)             may set result.suppress_post and
                                           append async callables to followups
    """

    def __init__(self, log_getter: Callable[[], Any]):
        self._log = log_getter
        self.before_claim: List[Callable] = []
        self.on_turn_start: List[Callable] = []
        self.on_event: List[Callable] = []
        self.on_turn_end: List[Callable] = []

    def register(self, name: str, fn: Callable) -> None:
        if name not in HOOK_NAMES:
            raise ValueError(f"unknown hook {name!r}")
        getattr(self, name).append(fn)

    async def fire(self, name: str, *args) -> list:
        """Run each hook in order; return their results. A hook that raises is
        logged and skipped, never failing the turn or later hooks."""
        results = []
        for fn in list(getattr(self, name)):
            try:
                value = fn(*args)
                if inspect.isawaitable(value):
                    value = await value
                results.append(value)
            except Exception as e:
                self._log().error(f"turn hook {name} failed: {type(e).__name__}: {e}")
        return results


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


@dataclass
class TurnBatch:
    shard: str
    rows: list
    message_ids: List[str]
    channel_id: str
    content: str
    origin: str = "queue"   # "stolen" (2.4); "followon" or "self" for a turn the CLI started (2.5)
    # -- steering (2.5) ----------------------------------------------------
    merged_ids: List[str] = field(default_factory=list)  # message_ids steered into this turn
    channels: set = field(default_factory=set)
    call_id: Optional[str] = None       # copied from a call row; steerable() refuses it
    phase: str = "new"                  # new -> streaming -> closing
    interrupting: bool = False
    steered_count: int = 0              # rows steered into this turn (the allowance)
    result_index: Optional[int] = None
    subtype: Optional[str] = None
    terminal_reason: Optional[str] = None
    saw_bg_task: bool = False           # a backgrounded task started during the turn
    timed_out: bool = False             # a follow-on turn never began
    died: bool = False                  # the process hit EOF before any result event
    primary_entry: Any = None           # the ledger entry of this turn's own write

    def __post_init__(self):
        for r in self.rows:
            cid = _row_get(r, "channel_id")
            if cid is not None:
                self.channels.add(cid)
            if self.call_id is None and _row_get(r, "call_id"):
                self.call_id = _row_get(r, "call_id")


@dataclass
class TurnResult:
    shard: str
    batch: TurnBatch
    response_text: str
    metadata: Dict
    wall: Optional[str] = None
    suppress_post: bool = False
    followups: List[Callable] = field(default_factory=list)
    raw_response_text: str = ""   # before an errored turn's text is replaced (2.6)
    requeued: bool = False        # the primary write failed; rows went back to the queue


class ServerState:
    """A view over the server module. Attribute access reads the server's
    current value; nothing is copied."""

    def __init__(self, server_module):
        self._server = server_module
        self.hooks = TurnHooks(lambda: self._server.log)
        self.active_turns: Dict[str, TurnBatch] = {}
        self.hive = hive.HiveState()  # open hive calls (in memory only, step 2.3)
        self.stolen_total: Dict[str, int] = {}     # thief shard -> rows stolen (2.4)
        self.steal_timers: Dict[Tuple[str, str], Any] = {}  # (thief, victim) -> TimerHandle
        self.bg_tasks: set = set()   # drain/steal/steer tasks; shutdown cancels and awaits them
        self.closing = False
        # Steering (2.5), all keyed by shard id.
        self.steer: Dict[str, steering.Ledger] = {}          # lines the CLI has not replayed
        self.steer_lock: Dict[str, asyncio.Lock] = {}        # every stdin writer holds it
        self.steer_unmatched: Dict[str, int] = {}
        self.steered_total: Dict[str, int] = {}
        self.control_waiters: Dict[Tuple[str, str], Any] = {}  # (shard, request_id) -> Future
        self.control_seq: Dict[str, int] = {}
        self.turn_seq: Dict[str, int] = {}                   # results read so far
        self.enqueued_at: Dict[str, float] = {}              # first unclaimed arrival (epoch)
        self.pushback: Dict[str, collections.deque] = {}     # stdout lines read ahead
        self.bg_seen: Dict[str, bool] = {}                   # a background task may self-start a turn
        self.replay_seen: Dict[str, bool] = {}               # this process has emitted a replay

    def __getattr__(self, name):
        if name in _SERVER_NAMES:
            return getattr(self.__dict__["_server"], name)
        raise AttributeError(name)

    # -- shard seam (2.1 changes only these bodies) ------------------------

    # With no shard_specs loaded (a test that sets agent_config directly, or a
    # server that has not read a registry) every key of agent_config is its own
    # default shard, exactly as in 2.0.

    def _specs(self):
        return getattr(self.__dict__["_server"], "shard_specs", None) or []

    def shard_ids(self) -> List[str]:
        specs = self._specs()
        if not specs:
            return list(self.agent_config)
        return [s.id for s in specs]

    def agent_of(self, shard: str) -> str:
        for s in self._specs():
            if s.id == shard:
                return s.agent
        return shard

    def cfg(self, shard: str) -> dict:
        return self.agent_config.get(self.agent_of(shard), {})


def make_state(server_module) -> ServerState:
    return ServerState(server_module)


# =============================================================================
# Claim and format
# =============================================================================

AUTOMATED_SENTINEL = "[KARAKOS_AUTOMATED]"  # default; the server passes its own


def location_tag(msg) -> str:
    """Where a queued row was posted: channel name, channel id and server.
    The name is left out when the relay could not resolve one."""
    channel = msg["channel"]
    if channel and channel != "unknown":
        return f"#{channel} (channel {msg['channel_id']}, server {msg['server']})"
    return f"channel {msg['channel_id']}, server {msg['server']}"


def format_batch(rows, format_attachments, sentinel: str = AUTOMATED_SENTINEL) -> str:
    """Render claimed rows as one user line. All-bot batches get the
    automated-traffic sentinel (a batch with even one human message stays
    unmarked, so a human reply still gets a fresh recall block)."""
    parts = []
    prev_tag = None
    for msg in rows:
        tag = location_tag(msg)
        # The location is printed only where it changes, to keep batches short.
        where = f"[{tag}] " if tag != prev_tag else ""
        prev_tag = tag
        part = (f"[{msg['created_at']}] {where}"
                f"{identity.row_label(msg)}: {msg['content']}")
        attachment_lines = format_attachments(msg["attachments"])
        if attachment_lines:
            part = f"{part}\n{attachment_lines}"
        parts.append(part)
    content = "\n\n".join(parts)
    if all(msg["is_bot"] for msg in rows):
        content = f"{sentinel}\n{content}"
    return content


def batch_from_rows(state: ServerState, shard: str, rows: list,
                    origin: str = "queue") -> TurnBatch:
    """Format claimed rows into a TurnBatch for `shard`."""
    content = format_batch(rows, state.format_attachments,
                           state.AUTOMATED_TRAFFIC_SENTINEL)
    return TurnBatch(
        shard=shard, rows=rows,
        message_ids=[m["message_id"] for m in rows],
        channel_id=rows[0]["channel_id"], content=content, origin=origin)


async def claim_next(state: ServerState, shard: str) -> Optional[TurnBatch]:
    """Claim the next batch (priority DESC, created_at, id; expired rows are
    skipped inside claim_batch) and format it. None for an empty queue."""
    rows = await msgqueue.claim_batch(state.db, shard, 20)
    if not rows:
        return None
    return batch_from_rows(state, shard, rows)


# =============================================================================
# Work stealing (step 2.4)
# =============================================================================

def _specs_of(state: ServerState):
    """ShardSpec-like objects for every shard (default shards when none loaded)."""
    specs = state._specs()
    if specs:
        return specs
    return [type("S", (), {"id": s, "agent": s})() for s in state.agent_config]


def _gate_paused(state: ServerState, shard: str) -> bool:
    gate = getattr(state, "usage_gate", None)
    return bool(gate and shard in gate.paused)


def _arm_steal_timer(state: ServerState, thief: str, victim: str, delay: float) -> None:
    """One timer per (thief, victim); it starts a drain on the thief when it fires."""
    key = (thief, victim)
    if key in state.steal_timers:
        return
    loop = asyncio.get_running_loop()

    def fire():
        state.steal_timers.pop(key, None)
        spawn(state, drain_shard(state, thief))

    state.steal_timers[key] = loop.call_later(max(delay, 0.0) + 0.05, fire)


async def steal_next(state: ServerState, thief: str) -> Optional[TurnBatch]:
    """Take waiting rows from a busy sibling shard of the same agent, or None.
    Off unless the agent's `work_stealing.enabled`. The rows keep agent =
    victim; the turn (session, cost, post) is the thief's."""
    cfg = state.cfg(thief)
    sc = stealing.steal_config(cfg)
    if not sc.enabled or _gate_paused(state, thief):
        return None
    specs = _specs_of(state)
    agent = state.agent_of(thief)
    mine = [s.id for s in specs if s.agent == agent]
    if len(mine) < 2:
        return None
    depths = await msgqueue.queued_depths(state.db, mine)
    held = await state.agent_hold_until(thief)
    if not stealing.thief_ready(state.agent_states.get(thief), held,
                                depths.get(thief, 0)):
        return None
    age = stealing.min_age_s(sc, cfg)
    victims = stealing.candidate_victims(specs, thief, state.agent_states, depths)
    for victim in victims:
        rows = await msgqueue.claim_stolen(state.db, thief, victim, sc.max_rows, age)
        if rows:
            state.stolen_total[thief] = state.stolen_total.get(thief, 0) + len(rows)
            state.log.info(f"steal thief={thief} victim={victim} rows={len(rows)}")
            return batch_from_rows(state, thief, rows, origin="stolen")
    # Nothing takeable yet: a row of a stealable kind may just be too young.
    for victim in victims:
        wait = await msgqueue.steal_wait_s(state.db, victim, age)
        if wait is not None:
            _arm_steal_timer(state, thief, victim, wait)
    return None


def _stealing_on(state: ServerState, shard: str) -> bool:
    return stealing.steal_config(state.cfg(shard)).enabled


async def maybe_steal_wake(state: ServerState, shard: str,
                           busy: Optional[str] = None) -> None:
    """Arm one timer per (idle ready sibling, busy victim) so a row waiting
    behind a busy shard is picked up after min_age. No polling: nothing is
    scheduled while no victim has waiting rows. `busy` names a shard that has
    just claimed a batch and is about to be PROCESSING (its state flips a
    moment later)."""
    agent = state.agent_of(shard)
    specs = _specs_of(state)
    mine = [s.id for s in specs if s.agent == agent]
    if len(mine) < 2:
        return
    cfg = state.cfg(shard)
    sc = stealing.steal_config(cfg)
    if not sc.enabled:
        return
    depths = await msgqueue.queued_depths(state.db, mine)
    age = stealing.min_age_s(sc, cfg)
    states = dict(state.agent_states)
    if busy:
        states[busy] = "PROCESSING"
    for thief in mine:
        if _gate_paused(state, thief):
            continue
        held = await state.agent_hold_until(thief)
        if not stealing.thief_ready(states.get(thief), held, depths.get(thief, 0)):
            continue
        for victim in stealing.candidate_victims(specs, thief, states, depths):
            _arm_steal_timer(state, thief, victim, age)


# =============================================================================
# Write / read
# =============================================================================

def spawn(state: ServerState, coro) -> Optional[asyncio.Task]:
    """create_task for work that touches the db or a shard's process (drain,
    steal wake, steer). Tracked so `cancel_background` can end it before the db
    closes; refused (coroutine closed) once shutdown has begun."""
    if state.closing:
        coro.close()
        return None
    task = asyncio.get_running_loop().create_task(coro)
    state.bg_tasks.add(task)
    task.add_done_callback(state.bg_tasks.discard)
    return task


async def cancel_background(state: ServerState) -> None:
    """Shutdown: stop new spawns and steal timers, cancel every tracked task and
    wait for them, so none touches the db after it closes."""
    state.closing = True
    for handle in list(state.steal_timers.values()):
        handle.cancel()
    state.steal_timers.clear()
    me = asyncio.current_task()
    tasks = [t for t in state.bg_tasks if t is not me]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def steering_on(state: ServerState, shard: str) -> bool:
    return steering.steer_config(state.cfg(shard)).enabled


def ledger_of(state: ServerState, shard: str) -> steering.Ledger:
    led = state.steer.get(shard)
    if led is None:
        led = state.steer[shard] = steering.Ledger(shard, state.log)
    return led


def lock_of(state: ServerState, shard: str) -> asyncio.Lock:
    lock = state.steer_lock.get(shard)
    if lock is None:
        lock = state.steer_lock[shard] = asyncio.Lock()
    return lock


async def write_user_line(state: ServerState, shard: str, content: str,
                          message_ids: List[str], steer: bool = False):
    """Send one user message to the shard's subprocess. Returns False when the
    write failed (the primary path has already logged it and flipped the shard
    to ERROR_RECOVERY). A `steer` write goes into a turn already in flight: it
    touches no shard state, and raises on failure so the caller can take its
    ledger entry back."""
    proc = state.agent_processes.get(shard)
    if not proc or not proc.stdin:
        state.log.error(f"No subprocess for {shard}")
        if steer:
            raise RuntimeError(f"no subprocess for {shard}")
        return False

    if not steer:
        state.agent_states[shard] = "PROCESSING"
        state.write_agent_beacon(shard, "PROCESSING", force=True)
        state.response_buffers[shard] = ""

    # Send message — Claude Code stream-json input envelope.
    # Format: {"type": "user", "message": {"role": "user", "content": <str>}}
    # The bare {"type":"user","content":...} form is rejected by the SDK.
    msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
    }) + "\n"
    try:
        proc.stdin.write(msg.encode())
        await proc.stdin.drain()
        # The CLI creates a session on its first user message, so from here on
        # the id is taken: a respawn must --resume it, never --session-id it.
        await state.mark_session_started(shard)
        if not steer:
            state.log.info(f"Sent message to {shard} ({len(message_ids)} queued messages)")
    except Exception as e:
        state.log.error(f"Error sending to {shard}: {e}")
        if steer:
            raise
        state.agent_states[shard] = "ERROR_RECOVERY"
        state.write_agent_beacon(shard, "ERROR_RECOVERY", force=True)
        return False
    return True


async def write_control(state: ServerState, shard: str, obj: dict) -> None:
    """Write one raw JSON line (a control_request) to the shard's stdin."""
    proc = state.agent_processes.get(shard)
    if not proc or not proc.stdin:
        raise RuntimeError(f"no subprocess for {shard}")
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    await proc.stdin.drain()


async def _next_line(state: ServerState, shard: str, proc, timeout: Optional[float] = None):
    """The next stdout line: a read-ahead line first, else the pipe. None on
    timeout; b"" at EOF."""
    pb = state.pushback.get(shard)
    if pb:
        return pb.popleft()
    if timeout is None:
        return await proc.stdout.readline()
    try:
        return await asyncio.wait_for(proc.stdout.readline(), timeout)
    except asyncio.TimeoutError:
        return None


def _unread(state: ServerState, shard: str, line: bytes) -> None:
    state.pushback.setdefault(shard, collections.deque()).appendleft(line)


def _is_init(line: bytes) -> bool:
    try:
        ev = json.loads(line.decode())
    except (ValueError, UnicodeDecodeError):
        return False
    return ev.get("type") == "system" and ev.get("subtype") == "init"


async def await_init(state: ServerState, shard: str, timeout: float) -> bool:
    """Wait up to `timeout` for the CLI to start a turn by itself. A
    `system/init` is left to be read by the turn; anything before it is
    dropped (the stream is still tee'd to the log)."""
    proc = state.agent_processes.get(shard)
    if not proc or not proc.stdout:
        return False
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        line = await _next_line(state, shard, proc, left)
        if not line:
            return False
        state.write_stream_log(shard, line)
        if _is_init(line):
            _unread(state, shard, line)
            return True


async def read_events(
    state: "ServerState", shard: str, channel_id: str,
    message_ids: Optional[List[str]] = None,
) -> Tuple[str, Dict]:
    """Read and process a shard's response stream (one turn)."""
    proc = state.agent_processes.get(shard)
    if not proc or not proc.stdout:
        return "", {}

    config = state.cfg(shard)
    # Default ON as of #91. It was False and, more to the point, dead: no
    # config file, template, doc or test in this repo ever set it, so the
    # tool_use branch below could not fire on any install. The issue's
    # acceptance test requires the lines to appear, and an opt-in nobody
    # knows about does not answer "is it broken?" for the people asking.
    # Set "tool_streaming": false in agents.yaml to go back to silence.
    # Precedence: the channel's ux.tool_streaming, then the agent's, then True.
    agent_ts = config.get("tool_streaming", True)
    try:
        override = state.ux_tool_streaming(channel_id)
    except Exception:       # a UX lookup must never cost the turn
        override = None
    tool_streaming = agent_ts if override is None else override
    stream_to_channel = config.get("stream_to_channel", False)
    # A copy: steered rows join it when the CLI replays them, so the turn's
    # events reach every row the turn answers.
    msg_ids = list(message_ids or [])
    batch = state.active_turns.get(shard)
    if batch is None:
        batch = TurnBatch(shard=shard, rows=[], message_ids=msg_ids,
                          channel_id=channel_id, content="")
    ledger = state.steer.get(shard)
    eof = False
    # A follow-on turn is started by the CLI, not by a write: it must begin soon.
    awaiting_start = batch.origin == "followon"

    # Throttle state is per-turn, not global: each turn starts with its
    # first tool line free so a long turn says something quickly. None, not
    # 0.0 — see state.should_post_tool_line().
    tool_lines_posted = 0
    last_tool_line_at: Optional[float] = None

    # Long-turn threads (6.2): None unless the switch is on for this channel,
    # in which case the tool-line branch below takes the LongTurn path and
    # the code for a channel without it is unchanged.
    long_turn: Optional[discord_ux.LongTurn] = None
    if tool_streaming and channel_id != "0":
        try:
            thread_cfg = state.ux_thread_cfg(channel_id)
        except Exception as e:   # a UX lookup must never cost the turn
            state.log.warning(f"ux thread lookup failed, threads off for this turn: {e}")
            thread_cfg = None
        if thread_cfg is not None:
            active = state.active_turns.get(shard)
            first_text = ""
            for r in (active.rows if active else []):
                first_text = r["content"] or ""
                if first_text:
                    break
            long_turn = discord_ux.LongTurn(
                thread_cfg, channel_id, time.monotonic(), first_text,
                state.TOOL_EVENT_MIN_INTERVAL, state.TOOL_EVENT_MAX_PER_TURN)

    final_text = ""
    metadata = {}
    last_posted_chunk = ""
    rejected_rl_info = None  # last rate_limit_event with status=rejected this turn
    # usage block of the last main-thread assistant event; its sum is the
    # session's context size (the result's usage is summed across the turn).
    last_usage = None

    # turn_events sequence number for this turn, and burst-collapse state
    # for content-less thinking blocks. Some builds strip thinking TEXT from
    # the transcript (signature only, empty body) — that still means "the
    # shard is thinking," so it's surfaced as presence: one empty row per
    # burst rather than one per block, which the chat page renders as a
    # pulsing "thinking" label instead of a flood of identical empty rows.
    event_seq = 0
    in_empty_think_burst = False
    decode_errors = 0

    try:
        while True:
            if awaiting_start:
                line = await _next_line(state, shard, proc, STEER_FOLLOWON_TIMEOUT_S)
                if line is None:
                    batch.timed_out = True
                    state.log.warning(
                        f"steer shard={shard} follow-on turn did not start within "
                        f"{STEER_FOLLOWON_TIMEOUT_S}s")
                    break
            else:
                line = await _next_line(state, shard, proc)
            if not line:
                eof = True
                break

            # Tee before parsing, so a malformed line is still on the record.
            state.write_stream_log(shard, line)

            try:
                event = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                decode_errors += 1
                state.log.warning(
                    f"{shard} stream-json decode error ({type(e).__name__}): "
                    f"{state.redact_for_log(line.decode(errors='replace').strip())!r}"
                )
                continue

            event_type = event.get("type")

            # Steering (2.5). A replay is the CLI saying it has consumed a user
            # line: not a tool result, no usage, nothing the handlers below may
            # count. A control_response answers an interrupt request.
            if event_type == "user" and event.get("isReplay"):
                awaiting_start = False
                await _on_replay(state, shard, batch, event, msg_ids)
                await state.hooks.fire("on_event", shard, event)
                continue
            if event_type == "control_response":
                resp = event.get("response") or {}
                waiter = state.control_waiters.get((shard, resp.get("request_id")))
                if waiter is not None and not waiter.done():
                    waiter.set_result(resp)
                await state.hooks.fire("on_event", shard, event)
                continue
            if event_type == "system":
                sub = event.get("subtype")
                if sub == "init":
                    awaiting_start = False
                elif sub == "task_started" and event.get("is_backgrounded"):
                    batch.saw_bg_task = True

            # Every event is proof the turn is still moving. This is the whole
            # beacon: a SIGSTOPped claude emits nothing, readline() blocks
            # here, and the timestamp stops advancing while the state stays
            # PROCESSING — which is precisely the pair wedge-check.py looks
            # for. Throttled internally, so a chatty turn is cheap.
            state.write_agent_beacon(shard, "PROCESSING", message_id=msg_ids[0] if msg_ids else None)

            # One `system`/`init` event opens the stream, listing the tool
            # set the CLI actually resolved for this session. A tool named
            # in permissions.deny (#99) is dropped from this list entirely
            # rather than surfacing as a runtime denial — logging it here is
            # the only place a full-tool deny is ever visible after the
            # fact.
            if event_type == "system" and event.get("subtype") == "init":
                tools = event.get("tools")
                if tools is not None:
                    state.log.info(f"{shard} session tools ({len(tools)}): {tools}")

            # The CLI reports rate-limit headroom in-band, on the stream that
            # is already open. Recorded rather than polled — see
            # state.record_rate_limit_event. Wrapped because a bookkeeping failure
            # must never cost the shard's actual reply, which is still
            # arriving on this same loop.
            if event_type == "rate_limit_event":
                _rl = event.get("rate_limit_info")
                if isinstance(_rl, dict) and _rl.get("status") == "rejected":
                    rejected_rl_info = _rl
                try:
                    await state.record_rate_limit_event(shard, event.get("rate_limit_info"))
                except Exception as e:
                    state.log.error(f"Failed to record rate limit event for {shard}: {e}")

            # Claude Code stream-json output: each turn emits one or more
            # `assistant` events with content blocks (thinking/text/tool_use),
            # then a single `result` event closes the turn.
            if event_type == "assistant":
                message = event.get("message", {}) or {}
                # Subagent (Task) sidechains carry parent_tool_use_id; their
                # smaller context must not replace the session's.
                if (event.get("parent_tool_use_id") is None
                        and isinstance(message.get("usage"), dict)):
                    last_usage = message["usage"]
                got_text = False
                for block in message.get("content", []) or []:
                    btype = block.get("type")
                    if btype == "thinking":
                        body = (block.get("thinking") or "").strip()
                        if body:
                            event_seq += 1
                            await state.write_turn_event(msg_ids, event_seq, "thinking", body)
                            in_empty_think_burst = False
                        elif not in_empty_think_burst:
                            event_seq += 1
                            await state.write_turn_event(msg_ids, event_seq, "thinking", "")
                            in_empty_think_burst = True
                    elif btype == "text":
                        text = block.get("text", "")
                        if text:
                            final_text += text
                            state.response_buffers[shard] = final_text
                            got_text = True
                            in_empty_think_burst = False
                            if stream_to_channel and channel_id != "0":
                                # TODO: Implement chunked streaming
                                pass
                            # Recorded as a turn event too, even though this
                            # may turn out to BE the final answer — a block
                            # can't be known final until the turn ends. The
                            # dashboard chat page dedupes an interstitial
                            # against the final body it matches.
                            stripped = text.strip()
                            if stripped and not post_guard.is_pass(stripped):
                                event_seq += 1
                                await state.write_turn_event(msg_ids, event_seq, "interstitial", stripped)
                    elif btype == "tool_use":
                        tool_name = block.get("name", "unknown")
                        state.log.info(f"{shard} called tool: {tool_name}")
                        in_empty_think_burst = False
                        event_seq += 1
                        await state.write_turn_event(
                            msg_ids, event_seq, "tool",
                            state.describe_tool_call(tool_name, block.get("input")),
                        )
                        if long_turn is not None:
                            now = time.monotonic()
                            plan = long_turn.plan(now, tool_lines_posted, last_tool_line_at)
                            if plan == "thread":
                                thread_id = await state.ux_create_thread(
                                    shard, channel_id, long_turn.anchor, long_turn.name)
                                if thread_id:
                                    long_turn.thread_created(thread_id)
                                else:
                                    long_turn.thread_failed()
                                    plan = long_turn.plan(now, tool_lines_posted,
                                                          last_tool_line_at)
                            if plan != "skip":
                                tool_lines_posted += 1
                                last_tool_line_at = now
                                posted_id = await state.post_to_discord(
                                    shard, long_turn.target,
                                    state.summarize_tool_call(tool_name, block.get("input")),
                                )
                                long_turn.note_post(posted_id)
                        elif tool_streaming and channel_id != "0":
                            now = time.monotonic()
                            if state.should_post_tool_line(tool_lines_posted,
                                                     last_tool_line_at, now):
                                tool_lines_posted += 1
                                last_tool_line_at = now
                                # dead_letter stays False: a tool line is a
                                # liveness signal, worthless once the turn
                                # has ended, and replaying it later would be
                                # noise. post_to_discord's own docstring
                                # already names these as an incidental.
                                await state.post_to_discord(
                                    shard, channel_id,
                                    state.summarize_tool_call(tool_name, block.get("input")),
                                )

                if got_text:
                    cleaned = state.THINKING_BLOCK_RE.sub("", final_text)
                    await state.write_streaming_response(msg_ids, cleaned)
                    if config.get("partial_response"):   # off by default; 2.3 turns it on
                        await state.write_partial_response(msg_ids, cleaned)

            elif event_type == "result":
                awaiting_start = False
                batch.phase = "closing"
                batch.result_index = event.get("result_index")
                batch.subtype = event.get("subtype")
                batch.terminal_reason = event.get("terminal_reason")
                state.turn_seq[shard] = state.turn_seq.get(shard, 0) + 1
                if (batch.terminal_reason == "aborted_tools"
                        and (batch.interrupting or shard in state.interrupted_agents)):
                    # The CLI's answer to our own interrupt request: not a failed turn.
                    state.log.info(f"{shard} turn aborted by interrupt request "
                                   f"(subtype={batch.subtype})")
                # Extract metadata. Token counts live under `usage`,
                # cost/duration are top-level. Final text is in `result`
                # for success, or `error` field for failures.
                usage = event.get("usage", {}) or {}
                metadata = {
                    "session_id": event.get("session_id"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "context_tokens": state.usage_context_tokens(last_usage),
                    "total_cost_usd": event.get("total_cost_usd", 0.0),
                    "duration_ms": event.get("duration_ms", 0),
                    "is_error": event.get("is_error", False),
                    "rate_limit_rejected": rejected_rl_info,
                }
                # If the assistant stream produced nothing, fall back to
                # the result's flat `result` string (success) or `error`.
                if not final_text:
                    final_text = event.get("result", "") or event.get("error", "")

                # A fine-grained deny rule (e.g. "Bash(curl:*)") lets the
                # tool stay in the session's list but declines the specific
                # call at request time — that shows up here, not in the
                # init event's tool list. Each denial is the acceptance
                # test's "logged" half; #99.
                denials = state.extract_permission_denials(event)
                metadata["permission_denials"] = denials
                for denial in denials:
                    state.log.warning(
                        f"{shard} permission denied: tool={denial.get('tool_name')} "
                        f"input={denial.get('tool_input')}"
                    )
                await state.hooks.fire("on_event", shard, event)
                break

            await state.hooks.fire("on_event", shard, event)

    except SystemExit as e:
        # Not a crash: something in the reader asked to exit. Say so, and end
        # the turn with whatever has been read.
        state.log.warning(f"Reader for {shard} ended by SystemExit (code={e.code!r})")
    except Exception as e:
        state.log.error(f"Error reading response from {shard}: {e}")

    if decode_errors and metadata:
        metadata["decode_errors"] = decode_errors

    batch.phase = "closing"
    if eof and ledger is not None and ledger.entries:
        # The process is gone: lines it never replayed go back to the queue.
        await release_pending(state, shard, "eof")
    elif (ledger is not None and batch.primary_entry is not None
            and not state.replay_seen.get(shard)):
        # This process has never replayed a line, so the entry cannot be waiting
        # on one (an old CLI, a stub): it must not linger as a phantom follow-on.
        # Once replays are seen, an unconsumed primary stays pending: the CLI
        # started a turn of its own (a self turn) ahead of it, and the line is
        # read as a follow-on.
        ledger.remove(batch.primary_entry)

    # Strip any inline thinking blocks (defense in depth)
    final_text = state.THINKING_BLOCK_RE.sub("", final_text).strip()

    # A turn that /interrupt ended has no answer, only a fragment of one.
    # Returning it would post half a sentence to the channel and bill it as
    # the reply — so it is dropped here, at the single point every caller of
    # read_agent_response goes through.
    if eof and not metadata and shard not in state.interrupted_agents:
        batch.died = True
        state.log.error(f"{shard} subprocess ended before producing a result")
    if shard in state.interrupted_agents:
        state.interrupted_agents.discard(shard)
        state.log.info(f"{shard} turn discarded (interrupted)")
        final_text, metadata = "", {}

    if (steering_on(state, shard) and not eof
            and (batch.saw_bg_task or (ledger is not None and ledger.entries))):
        # More of this process's work is coming (a queued line, a self-started
        # turn): the shard stays PROCESSING until settle() says otherwise.
        return final_text, metadata
    state.agent_states[shard] = "IDLE"
    state.write_agent_beacon(shard, "IDLE", force=True)
    return final_text, metadata

# =============================================================================
# Steering (step 2.5)
# =============================================================================

async def _on_replay(state: ServerState, shard: str, batch: TurnBatch, event: dict,
                     msg_ids: List[str]) -> None:
    """The CLI consumed a user line. Match it against the ledger; rows of any
    steered entry it covers belong to the turn this replay appears in. They
    stay in progress until that turn's `result`."""
    ledger = ledger_of(state, shard)
    state.replay_seen[shard] = True
    message = event.get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    before = ledger.unmatched
    taken = ledger.match_replay(content)
    if ledger.unmatched != before:
        state.steer_unmatched[shard] = ledger.unmatched
    rows = 0
    for entry in taken:
        if entry.kind != steering.STEER:
            continue
        for mid in entry.message_ids:
            if mid not in batch.merged_ids:
                batch.merged_ids.append(mid)
            msg_ids.append(mid)
        rows += len(entry.row_ids)
    if rows:
        state.log.info(f"steer shard={shard} rows={rows} "
                       f"turn={state.turn_seq.get(shard, 0)} replayed")


async def release_pending(state: ServerState, shard: str, reason: str = "") -> int:
    """Return every steered line the CLI never replayed to the queue (they keep
    created_at, so their place). Primary entries are dropped: their rows follow
    today's in-flight crash handling. Returns the number of rows released."""
    ledger = state.steer.get(shard)
    if ledger is None or not ledger.entries:
        return 0
    entries = ledger.drain_all()
    ids = [i for e in entries if e.kind == steering.STEER for i in e.row_ids]
    if not ids:
        return 0
    n = await msgqueue.release(state.db, ids)
    state.log.info(f"steer shard={shard} released {n} unreplayed rows ({reason})")
    return n


async def steer_enqueued(state: ServerState, shard: str, channel_id: str) -> None:
    """A row arrived while `shard` is mid-turn: write it into the running turn
    if it may be, else leave it QUEUED for after the turn."""
    sc = steering.steer_config(state.cfg(shard))
    if not sc.enabled:
        return
    async with lock_of(state, shard):
        batch = state.active_turns.get(shard)
        if batch is None or batch.phase != "streaming":
            return
        head = await msgqueue.peek_claimable(state.db, shard, 1)
        if not head or not steering.steerable(state, shard, head[0]):
            return
        # A steered write bypasses the drain, so the gate is asked here too.
        if any(v for v in await state.hooks.fire("before_claim", shard)):
            return
        rows = await msgqueue.claim_steerable(
            state.db, shard, batch.channel_id, steering.allowance(state, shard))
        if not rows:
            return
        ids = [r["id"] for r in rows]
        # The claim awaited: the turn may have closed meanwhile.
        if (state.active_turns.get(shard) is not batch
                or not steering.steerable(state, shard, rows[0])):
            await msgqueue.release(state.db, ids)
            spawn(state, drain_shard(state, shard))
            return
        text = format_batch(rows, state.format_attachments,
                            state.AUTOMATED_TRAFFIC_SENTINEL)
        entry = steering.SteerLine(
            row_ids=ids, text=text, channel_id=batch.channel_id,
            written_at=time.time(), kind=steering.STEER,
            message_ids=[r["message_id"] for r in rows])
        ledger = ledger_of(state, shard)
        ledger.append(entry)
        batch.steered_count += len(rows)
        try:
            await write_user_line(state, shard, text, entry.message_ids, steer=True)
        except Exception as e:
            ledger.remove(entry)
            batch.steered_count -= len(rows)
            await msgqueue.release(state.db, ids)
            state.log.error(f"steer write to {shard} failed: {type(e).__name__}: {e}")
            return
        state.steered_total[shard] = state.steered_total.get(shard, 0) + len(rows)
        state.log.info(f"steer shard={shard} rows={len(rows)} "
                       f"turn={state.turn_seq.get(shard, 0)}")


async def _steer_task(state: ServerState, shard: str, channel_id: str) -> None:
    try:
        await steer_enqueued(state, shard, channel_id)
    except Exception as e:  # noqa: BLE001 — never lose a row's wake-up to a bug here
        state.log.error(f"steer_enqueued failed for {shard}: {type(e).__name__}: {e}")


async def interrupt_with_message(state: ServerState, shard: str, message: str,
                                 channel_id: str = "0", author: str = "interrupt",
                                 fallback: Optional[Callable] = None) -> dict:
    """Queue `message` at INTERRUPT_PRIORITY and, if the shard is mid-turn, end
    that turn with a control_request (the process stays alive and the priority
    row runs next). A missing or failed control_response falls back to
    `fallback(shard)` (today's kill-and-respawn interrupt)."""
    import uuid
    message_id = f"interrupt-{uuid.uuid4()}"
    await msgqueue.write_commit(
        state.db,
        "INSERT INTO message_queue (agent, channel, channel_id, server, author,"
        " author_id, is_bot, content, message_id, priority)"
        " VALUES (?, 'interrupt', ?, 'local', ?, '0', 0, ?, ?, ?)",
        (shard, channel_id, author, message, message_id, steering.INTERRUPT_PRIORITY))
    msgqueue.notify(shard)
    out = {"message_id": message_id, "interrupted": False, "mode": "queued"}

    batch = state.active_turns.get(shard)
    if state.agent_states.get(shard) != "PROCESSING":
        # IDLE: the normal drain runs it.
        notify_enqueued(state, shard, channel_id)
        return out
    if not steering_on(state, shard):
        # No control-request path (2.0 behaviour): today's interrupt, then the
        # priority row is first in line on the respawned process.
        interrupted = bool(fallback is not None and await fallback(shard))
        out.update(interrupted=interrupted, mode="kill" if interrupted else "queued")
        return out
    if batch is None or batch.phase != "streaming":
        # The turn is starting or already closing: first in line when it ends.
        notify_enqueued(state, shard, channel_id)
        return out

    state.control_seq[shard] = n = state.control_seq.get(shard, 0) + 1
    rid = f"int-{shard}-{n}"
    waiter = asyncio.get_running_loop().create_future()
    state.control_waiters[(shard, rid)] = waiter
    batch.interrupting = True                 # steering stops
    state.interrupted_agents.add(shard)       # finish_turn treats it as interrupted
    ok = False
    try:
        try:
            async with lock_of(state, shard):
                await write_control(state, shard, {
                    "type": "control_request", "request_id": rid,
                    "request": {"subtype": "interrupt"}})
            resp = await asyncio.wait_for(waiter, CONTROL_TIMEOUT_S)
            ok = resp.get("subtype") == "success"
        except (asyncio.TimeoutError, OSError, RuntimeError) as e:
            state.log.warning(f"interrupt request to {shard} got no answer "
                              f"({type(e).__name__}); falling back")
    finally:
        state.control_waiters.pop((shard, rid), None)
    if ok:
        out.update(interrupted=True, mode="control")
        return out
    if fallback is not None:
        out["interrupted"] = bool(await fallback(shard))
        out["mode"] = "kill"
    return out


async def settle(state: ServerState, shard: str, last: Optional[TurnBatch] = None,
                 ready: bool = False) -> Optional[TurnBatch]:
    """After a turn's `result`: is there more of this process's work to read?
    A turn is framed from system/init to result; nothing equates one stdin line
    with one result.

    - ledger entries remain (lines the CLI queued after the last tool
      boundary): the shard stays PROCESSING and a read-only follow-on batch is
      returned;
    - a backgrounded task started in the turn: wait up to SELF_TURN_WINDOW_S
      for the CLI to start a turn by itself (`ready`: its init is already
      read ahead) and return a self batch;
    - else None (the shard may go IDLE)."""
    if not steering_on(state, shard):
        return None
    proc = state.agent_processes.get(shard)
    if proc is None or getattr(proc, "returncode", None) is not None:
        return None
    nxt = await _settle_next(state, shard, last, ready)
    if nxt is None and state.agent_states.get(shard) == "PROCESSING":
        state.agent_states[shard] = "IDLE"
        state.write_agent_beacon(shard, "IDLE", force=True)
    return nxt


async def _settle_next(state: ServerState, shard: str, last: Optional[TurnBatch],
                       ready: bool) -> Optional[TurnBatch]:
    ledger = ledger_of(state, shard)
    async with lock_of(state, shard):
        pending = ledger.pending()
    if pending:
        state.agent_states[shard] = "PROCESSING"
        state.write_agent_beacon(shard, "PROCESSING", force=True)
        return TurnBatch(shard=shard, rows=[], message_ids=[],
                         channel_id=pending[0].channel_id, content="",
                         origin="followon")
    if ready or (last is not None and last.saw_bg_task and not last.timed_out):
        if ready or await await_init(state, shard, SELF_TURN_WINDOW_S):
            state.agent_states[shard] = "PROCESSING"
            state.write_agent_beacon(shard, "PROCESSING", force=True)
            return TurnBatch(shard=shard, rows=[], message_ids=[],
                             channel_id=state.agent_last_channel.get(shard, "0"),
                             content="", origin="self")
    return None


async def _settle_loop(state: ServerState, shard: str, last: Optional[TurnBatch],
                       ready: bool = False) -> list:
    """Read follow-on and self-started turns until nothing more is pending.
    Returns the hook followups those turns produced (run after the lock is
    released, like the first turn's)."""
    followups: list = []
    while True:
        nxt = await settle(state, shard, last, ready=ready)
        ready = False
        if nxt is None:
            return followups
        await state.hooks.fire("on_turn_start", shard, nxt)
        result = await run_turn(state, shard, nxt, write=False)
        if nxt.timed_out:
            await _bounce(state, shard)
            return followups
        await finish_turn(state, shard, result)
        followups.extend(result.followups)
        last = nxt


async def _bounce(state: ServerState, shard: str) -> None:
    """A follow-on turn never started: restart the process through the existing
    kill-and-respawn path. Kill releases the ledger, and a dead process cannot
    deliver a line twice."""
    state.log.warning(f"steer shard={shard} bouncing the process (follow-on never started)")
    await state.kill_agent_subprocess(shard)
    state.pushback.pop(shard, None)
    await state.start_agent_subprocess(shard)
    spawn(state, drain_shard(state, shard))


async def _drain_stale_self_turn(state: ServerState, shard: str) -> list:
    """A self-started turn that began after the shard went IDLE is read before
    anything is written, so its events cannot leak into the next reply."""
    if not state.bg_seen.pop(shard, False) or not steering_on(state, shard):
        return []
    proc = state.agent_processes.get(shard)
    if not proc or not proc.stdout or getattr(proc, "returncode", None) is not None:
        return []
    while True:
        line = await _next_line(state, shard, proc, 0.01)
        if not line:
            return []
        state.write_stream_log(shard, line)
        if _is_init(line):
            _unread(state, shard, line)
            return await _settle_loop(state, shard, None, ready=True)


# =============================================================================
# One turn
# =============================================================================

PROCESS_WAIT_S = 15.0   # how long a write waits for a pending respawn


def _proc_alive(state: ServerState, shard: str) -> bool:
    proc = state.agent_processes.get(shard)
    return bool(proc and proc.stdin and getattr(proc, "returncode", None) is None)


async def wait_for_process(state: ServerState, shard: str,
                           timeout: float = PROCESS_WAIT_S) -> bool:
    """Wait (bounded) for the shard to have a live subprocess. A drain can run
    between an interrupt's kill and its respawn; writing then hits no pipe."""
    deadline = time.monotonic() + timeout
    while not _proc_alive(state, shard):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _redrain_when_alive(state: ServerState, shard: str) -> None:
    """After a failed primary write: once the respawn lands, drain again. Bounded,
    so a shard that is deliberately down does not spin."""
    if await wait_for_process(state, shard):
        await drain_shard(state, shard)


async def run_turn(state: ServerState, shard: str, batch: TurnBatch,
                   write: bool = True) -> TurnResult:
    """Run one claimed batch through the subprocess and read its reply. With
    `write=False` (a follow-on or self-started turn) nothing is written: the
    CLI started the turn and this only reads it."""
    channel_id = batch.channel_id
    message_ids = batch.message_ids
    messages = batch.rows

    # Record where this turn came from before the subprocess can act on
    # it. POST /ask has no request context of its own — the MCP tool that
    # calls it knows only the agent name — so the channel a question is
    # posted into and the people entitled to answer it both come from
    # here (#101).
    state.agent_turn_context[shard] = {
        "channel_id": channel_id,
        "author_ids": [msg["author_id"] for msg in messages if not msg["is_bot"]],
        "message_ids": message_ids,
    }
    state.active_turns[shard] = batch

    # Remember where this agent is talking. A subprocess that dies between
    # turns has no turn context to borrow a channel from, so this is what
    # the respawn notice is addressed to (#90).
    if channel_id != "0":
        state.agent_last_channel[shard] = channel_id

    try:
        # Start typing indicator
        await state.start_typing(shard, channel_id)

        write_failed = False
        if write:
            await wait_for_process(state, shard)
        if not write:
            batch.phase = "streaming"
        elif steering_on(state, shard):
            # Every line the shard writes is matched the same way: the entry goes
            # in before the write is awaited, under the lock every writer holds.
            async with lock_of(state, shard):
                entry = steering.SteerLine(
                    row_ids=[r["id"] for r in messages if _row_get(r, "id") is not None],
                    text=batch.content, channel_id=channel_id,
                    written_at=time.time(), kind=steering.PRIMARY,
                    message_ids=list(message_ids))
                ledger_of(state, shard).append(entry)
                ok = await state.send_to_agent(shard, batch.content, message_ids)
                if ok is False:
                    ledger_of(state, shard).remove(entry)
                    write_failed = True
                else:
                    batch.primary_entry = entry
                    batch.phase = "streaming"
        else:
            # Send to agent. Through the server's wrapper, not write_user_line
            # directly: tests patch it there.
            write_failed = (await state.send_to_agent(
                shard, batch.content, message_ids)) is False

        # Read response (nothing to read when the line never reached the CLI)
        try:
            if write_failed:
                response_text, metadata = "", {}
            else:
                response_text, metadata = await state.read_agent_response(
                    shard, channel_id, message_ids)
        finally:
            # The turn is over: any question still on screen belongs to a
            # subprocess that has stopped waiting for it, and answering it
            # would feed a reply into a turn that no longer exists.
            state.ask_registry.discard_agent(shard)
            state.agent_turn_context.pop(shard, None)
            state.active_turns.pop(shard, None)

            # Stop typing. A batch can span multiple channels when messages
            # queued up behind this turn in a channel other than channel_id
            # (see notify_enqueued, #121) — each of those got its own
            # start_typing() call at arrival time, so each needs to be
            # stopped here too, not just the reply channel, or that
            # indicator spins forever with no reply landing to end it.
            #
            # In the finally, not after it: read raising is the one case
            # where nothing downstream will ever clear these, and #121
            # turned that from one stuck indicator into one per channel in
            # the batch.
            for cid in {msg["channel_id"] for msg in messages} | batch.channels:
                await state.stop_typing(cid)
            if not write:
                await state.stop_typing(channel_id)
            if batch.saw_bg_task:
                state.bg_seen[shard] = True
    finally:
        state.active_turns.pop(shard, None)

    if write_failed:
        # The CLI never saw these rows: they are not answered, so they are not
        # complete. Back to the queue; the next drain after the respawn runs them.
        ids = [r["id"] for r in messages if _row_get(r, "id") is not None]
        await msgqueue.release(state.db, ids)
        if state.agent_states.get(shard) == "PROCESSING":
            state.agent_states[shard] = "IDLE"
            state.write_agent_beacon(shard, "IDLE", force=True)
        state.log.warning(f"{shard} write failed; {len(ids)} row(s) returned to the queue")
        spawn(state, _redrain_when_alive(state, shard))
        return TurnResult(shard=shard, batch=batch, response_text="", metadata={},
                          requeued=True)

    return TurnResult(shard=shard, batch=batch, response_text=response_text,
                      metadata=metadata)


async def react_to_trigger(state, agent: str, channel_id: str, message_ids: List[str],
                           emoji_path: str) -> bool:
    """Add the reaction for a `REACT:` reply to the turn's last real Discord
    message. Synthetic ids (`reaction:`, `edit:`, hive and interrupt rows) have
    no Discord message to react to: one INFO line, no request."""
    target = next((m for m in reversed(message_ids) if str(m).isdigit()), None)
    if target is None:
        state.log.info(f"{agent} REACT skipped: no Discord message in {message_ids}")
        return False
    return await state.add_reaction(agent, channel_id, target, emoji_path)


async def finish_turn(state: ServerState, shard: str, result: TurnResult):
    """Cost, wall check, hooks, Discord post, mark complete, redrain.

    Called with the shard lock held; followups run after it is released."""
    # Every wrapper below (cost, session, Discord token, hold) is keyed by the
    # shard id: rows are per shard, and AGENT_TOKENS holds the owning agent's
    # token under each shard id. For a default shard this is the agent id.
    if result.requeued:
        return
    agent = shard
    batch = result.batch
    channel_id = batch.channel_id
    message_ids = batch.message_ids
    # Rows steered into this turn are answered by its one reply, and complete
    # (or fail, or are held) with it.
    all_ids = list(message_ids) + list(batch.merged_ids)
    self_turn = batch.origin == "self"
    if self_turn:
        # A turn the CLI started by itself answers no row: it speaks where the
        # shard last spoke.
        channel_id = state.agent_last_channel.get(shard, "0")
        if not POST_SELF_TURNS:
            result.suppress_post = True
    response_text = result.response_text
    metadata = result.metadata

    # Post cost update
    if metadata:
        await state.post_cost_update(agent, metadata)
        await state.update_session_tokens(agent, metadata.get("input_tokens", 0))
        await state.update_session_context(agent, metadata.get("context_tokens", 0))
        state.log.info(f"{agent} ctx={metadata.get('context_tokens', 0)} "
                       f"turn_in={metadata.get('input_tokens', 0)} "
                       f"out={metadata.get('output_tokens', 0)}")

    # Usage / model wall: hold the batch instead of consuming it.
    wall = state.classify_wall(response_text, metadata.get("is_error", False),
                               metadata.get("rate_limit_rejected")) if metadata else None
    if wall and all_ids:
        result.wall = wall
        until = state.wall_not_before(wall, response_text,
                                      metadata.get("rate_limit_rejected"),
                                      state.agent_wall_strikes.get(shard, 0))
        state.agent_wall_strikes[shard] = state.agent_wall_strikes.get(shard, 0) + 1
        await state.hold_batch(agent, channel_id, all_ids, wall, until)
        return
    state.agent_wall_strikes.pop(shard, None)

    # Any other errored turn: the `result` text is raw CLI output (an
    # OAuth failure body, a stack trace), not a reply. It never goes to
    # the channel; the server log keeps a redacted copy and the row is
    # marked failed rather than complete.
    final_status = state.STATUS_COMPLETE
    result.raw_response_text = response_text
    if metadata and metadata.get("is_error"):
        state.log.error(f"{agent} turn ended with is_error; raw result (redacted): "
                        f"{state.redact_for_log(response_text, 500)!r}")
        response_text = state.GENERIC_TURN_ERROR
        final_status = state.STATUS_CRASHED
        result.response_text = response_text
        if self_turn:
            result.suppress_post = True

    if batch.died and all_ids and not self_turn:
        # The subprocess died mid-turn: there is no answer. Recording the rows
        # complete with an empty response would silently drop the messages.
        response_text = state.GENERIC_TURN_ERROR
        final_status = state.STATUS_CRASHED
        result.response_text = response_text

    await state.hooks.fire("on_turn_end", shard, result)

    # Post response to Discord
    discord_msg_id = None
    react_emoji = (discord_ux.parse_react_reply(response_text)
                   if response_text and channel_id != "0" and not result.suppress_post
                   and final_status == state.STATUS_COMPLETE else None)
    if react_emoji and state.ux_for_channel_id(channel_id).reactions:
        # `REACT: <emoji>` is a reaction on the triggering message, not a post.
        await react_to_trigger(state, agent, channel_id, all_ids, react_emoji)
    elif response_text and channel_id != "0" and not result.suppress_post:
        discord_msg_id = await state.post_to_discord(agent, channel_id, response_text,
                                                     dead_letter=True,
                                                     queue_message_id=(all_ids[0] if all_ids else None))

    # Mark complete
    if all_ids:
        await msgqueue.write_commit(
            state.db,
            f"""
            UPDATE message_queue
            SET processed = ?, response = ?, discord_response_id = COALESCE(?, discord_response_id), processed_at = CURRENT_TIMESTAMP
            WHERE message_id IN ({','.join('?' * len(all_ids))})
            """,
            (final_status, response_text, discord_msg_id, *all_ids)
        )

    state.log.info(f"{agent} processed {len(all_ids)} messages")

    # Anything that arrived while this turn was running is still QUEUED,
    # and notify_enqueued is the ONLY caller of drain_shard — it fires
    # solely on the IDLE branch. So without this, a message that landed
    # mid-turn waits not for the turn to end but for the *next* inbound
    # message to arrive and happen to sweep it up. That is the second
    # half of #121: the first half puts a typing indicator in the
    # waiting channel, and this is what makes it a promise the server
    # can keep rather than an indicator that spins until someone else
    # speaks.
    #
    # create_task, not a direct call: the lock is still held here and it
    # is not reentrant. The new task blocks on it until this `async
    # with` exits. It cannot spin — every drain moves its batch out of
    # STATUS_QUEUED, so the count strictly decreases, and the `if not
    # batch: return` in drain_shard is the floor.
    rows = await state.db.execute_fetchall(
        "SELECT COUNT(*) AS count FROM message_queue WHERE agent = ? AND processed = ?",
        (shard, state.STATUS_QUEUED))
    row = rows[0] if rows else None
    if row and row["count"]:
        state.log.info(f"{agent} has {row['count']} messages still queued — draining again")
        spawn(state, drain_shard(state, shard))
    elif _stealing_on(state, shard):
        # This shard is about to be free with nothing of its own: a busy
        # sibling's waiting rows may be stolen (drain_shard decides).
        await maybe_steal_idle(state, shard)


async def maybe_steal_idle(state: ServerState, shard: str) -> None:
    """If a sibling has waiting rows, start a drain on `shard` (it queues on
    the lock held by the caller, then claims or steals)."""
    mine = [s.id for s in _specs_of(state) if s.agent == state.agent_of(shard)]
    if len(mine) < 2:
        return
    depths = await msgqueue.queued_depths(state.db, mine)
    if any(depths[sid] for sid in mine if sid != shard):
        spawn(state, drain_shard(state, shard))


async def _coalesce(state: ServerState, shard: str) -> None:
    """Burst coalescing at idle: sleep out the rest of the window, once, so a
    burst is claimed as one batch. Measured from the oldest queued row's
    arrival (not sliding), so the added latency is at most coalesce_ms."""
    sc = steering.steer_config(state.cfg(shard))
    if not sc.coalesce_ms:
        return
    rows = await msgqueue.peek_claimable(state.db, shard, 20)
    if not rows:
        return
    head = rows[0]
    created = steering._epoch(_row_get(head, "created_at")) or 0.0
    arrived = max(state.enqueued_at.get(shard, 0.0), created)
    view = {"call_id": _row_get(head, "call_id"), "priority": _row_get(head, "priority"),
            "created_at": arrived}
    wait = steering.coalesce_wait_s(view, time.time(), sc, queued=len(rows))
    if wait > 0:
        await asyncio.sleep(wait)


async def drain_shard(state: ServerState, shard: str):
    """Process pending messages for a shard."""
    lock = state.agent_locks.get(shard)
    if not lock:
        return

    result = None
    extra_followups: list = []
    async with lock:
        if state.agent_states.get(shard) != "IDLE":
            return

        # Held behind a usage wall: do not dispatch (it would hit the wall
        # again). New arrivals wait with the held batch; the wake timer
        # replays all of it after the reset.
        held_until = await state.agent_hold_until(shard)
        if held_until:
            state.schedule_hold_wake(shard, held_until)
            return

        # A truthy before_claim result defers the drain: no claim, no state
        # change (2.7's budget and governor use this).
        if any(v for v in await state.hooks.fire("before_claim", shard)):
            return

        if steering_on(state, shard):
            proc = state.agent_processes.get(shard)
            if proc is not None and getattr(proc, "returncode", None) is not None:
                # The process is gone: the respawn watcher brings it back and
                # redrains. Claiming now would write into a dead pipe.
                return
            extra_followups.extend(await _drain_stale_self_turn(state, shard))
            await _coalesce(state, shard)

        batch = await claim_next(state, shard)
        if batch is None:
            batch = await steal_next(state, shard)
        if batch is not None:
            state.enqueued_at.pop(shard, None)
            if batch.origin == "queue" and _stealing_on(state, shard):
                # Backlog behind this (possibly long) turn: an idle sibling may take it.
                spawn(state, maybe_steal_wake(state, shard, busy=shard))

            await state.hooks.fire("on_turn_start", shard, batch)
            result = await run_turn(state, shard, batch)
            await finish_turn(state, shard, result)
            # The CLI may have more of this process's work to report (lines it
            # queued after the last tool boundary, a self-started turn).
            extra_followups.extend(await _settle_loop(state, shard, batch))

    # Lock released: hook followups (e.g. 2.6's reset) may take it themselves.
    if result is not None or extra_followups:
        for fn in (list(result.followups) if result is not None else []) + extra_followups:
            try:
                value = fn()
                if inspect.isawaitable(value):
                    await value
            except Exception as e:
                state.log.error(f"turn followup failed: {type(e).__name__}: {e}")


def notify_enqueued(state: ServerState, shard: str, channel_id: str) -> None:
    """A row was just inserted for `shard`: wake the loop or show typing."""
    st = state.agent_states.get(shard)
    state.enqueued_at.setdefault(shard, time.time())
    if st == "IDLE":
        spawn(state, drain_shard(state, shard))
    elif st in ("PROCESSING", "ERROR_RECOVERY") and _stealing_on(state, shard):
        spawn(state, maybe_steal_wake(state, shard))
    if st == "PROCESSING":
        # Agent is mid-turn in another channel. Without this, a message
        # landing behind a busy turn shows no typing indicator and no ack
        # until the drain happens to reach it — indistinguishable from being
        # ignored (#121). start_typing() is a no-op for channel_id "0" and
        # for a channel that already has a task running, so it composes
        # safely with the drain's own start_typing() once this channel is
        # picked up.
        #
        # PROCESSING specifically, not "anything but IDLE": the indicator is
        # a promise that a turn is in flight and will end. In ERROR_RECOVERY
        # — or for an agent with no state at all, i.e. one that never
        # started — no turn is running, nothing will call stop_typing(), and
        # the indicator would spin until the process restarts.
        asyncio.create_task(state.start_typing(shard, channel_id))
        if steering_on(state, shard):
            spawn(state, _steer_task(state, shard, channel_id))
