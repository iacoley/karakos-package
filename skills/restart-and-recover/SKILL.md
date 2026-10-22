---
name: restart-and-recover
description: Get a stuck or silent agent responding again, escalating from least to most destructive, and confirm it recovered
---

Use this when an agent is not answering, a turn has run far too long, or the health
monitor reported a wedge. Fixing something broken is yours to do and report. Changing
a setting the owner chose (cost limits, models, schedules) or destroying data is not:
ask first.

## 1. Look before touching anything

- `get_health` and `get_findings` (admin tools): what the monitor already knows.
- `get_queue` for the agent: is work waiting, and how old is the oldest row?
- `tail_log` with `agent-server`, then `relay`: errors around the time it went quiet.
- A paused agent looks stuck. `list_agents` shows its state; `resume_agent` if
  it was paused by mistake. Do not resume a pause the owner set on purpose.

Write down what you saw. A recovery with no record teaches nobody anything.

## 2. Escalate one step at a time

Stop at the first step that works.

1. `resume_agent`: only if it was paused.
2. Wait one more turn if the agent is `PROCESSING` and under about two minutes.
   A long tool call is not a wedge.
3. `reload_agent`: respawns the process and keeps the session context. This also
   picks up changed `config/agents.yaml` env allowlist entries.
4. `reset_agent`: starts a new session. **The context is destroyed.** Write a handoff
   note first if the agent can still run a turn (`session` tool, `finalize`).
5. Wedged server or relay, not just one agent: this needs the host
   (`make down && make up`). You cannot do that from inside; tell the owner.

`reload_agent`, `reset_agent`, `pause_agent` and `resume_agent` act on the calling
agent only, unless you are the primary agent. A non-primary agent asks the primary
or the owner.

## 3. Confirm it recovered

- `get_queue` shows the queue draining, not growing.
- `get_health` shows a fresh beacon for the agent.
- Send one harmless message and read the answer. No answer in a few minutes means
  go back to step 1 with what you know now.

## 4. Report

One short message to the owner: what failed, which step fixed it, what was lost
(for a reset: the context), and what you could not verify. If the cause looks like
a platform bug, contribute the fix upstream rather than working around it.
