import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import AccountStrip from "./AccountStrip";
import ShardTable from "./ShardTable";
import TokenBudgets from "./TokenBudgets";
import { HiveCallTable } from "./HiveCallLog";
import { HIVE_STATUSES } from "@/lib/fleetView";
import type { HiveCall } from "@/lib/packageBackend";
import type { AgentShardRow } from "@/lib/agentStatus";

const NOW = 1_791_000_000;

const shard = (over: Partial<AgentShardRow>): AgentShardRow => ({
  id: "alpha", is_default: true, state: "IDLE", alive: true, pid: 1, session_id: "", queue_depth: 0,
  context_tokens: 0, channels: [], last_channel: null, paused: null, ...over,
});

describe("ShardTable", () => {
  it("renders a paused row with its reason, sorted first, and 0 tokens as unknown", () => {
    const html = renderToStaticMarkup(
      <ShardTable
        now={NOW}
        agents={[{ name: "alpha", shards: [shard({ id: "alpha" }), shard({ id: "alpha-2", is_default: false, paused: { reason: "budget", until: NOW + 600 } })] }]}
      />
    );
    expect(html).toContain("budget: until 2026-10-03");
    expect(html).toContain("unknown");
    expect(html.indexOf("alpha-2")).toBeLessThan(html.indexOf("alpha<span"));
    expect(html).toContain("default");
  });
  it("shows PID, uptime and last turn, and 'never' for a shard with no turn yet", () => {
    const html = renderToStaticMarkup(
      <ShardTable
        now={NOW}
        agents={[{ name: "a", shards: [
          shard({ id: "a", alive: false, pid: null }),
          shard({ id: "a-2", is_default: false, alive: true, pid: 4242, uptime_seconds: 125, last_turn_at: NOW }),
        ] }]}
      />
    );
    expect(html).toContain("4242");
    expect(html).toContain("2m");
    expect(html).toContain("never");
  });
  it("says 'until usage drops' for an open-ended pause", () => {
    const html = renderToStaticMarkup(
      <ShardTable now={NOW} agents={[{ name: "a", shards: [shard({ paused: { reason: "governor", until: null } })] }]} />
    );
    expect(html).toContain("governor: until usage drops");
  });
});

describe("HiveCallTable", () => {
  const call = (status: string, i: number): HiveCall => ({
    call_id: `c${i}`, from_agent: "a", to_agent: "b", depth: 1, status,
    created_at: "2026-10-03 10:00:00", started_at: null, answered_at: null, duration_ms: 1500,
    question: "q?", answer: status === "answered" ? "yes" : null, error: status === "error" ? "callee_error" : null,
  });
  it("renders all six status chips", () => {
    const html = renderToStaticMarkup(<HiveCallTable calls={HIVE_STATUSES.map((s, i) => call(s, i))} />);
    for (const s of HIVE_STATUSES) expect(html).toContain(`>${s}<`);
  });
  it("renders the empty state", () => {
    expect(renderToStaticMarkup(<HiveCallTable calls={[]} />)).toContain("No hive calls in this window.");
  });
});

describe("AccountStrip", () => {
  it("renders the breaker banner, windows and the weekly figure", () => {
    const html = renderToStaticMarkup(
      <AccountStrip
        now={NOW}
        usage={{
          windows: { five_hour: { status: "rejected", resets_at: NOW + 60, utilization_pct: 41, updated_at: "x" }, seven_day: { status: "allowed", resets_at: null, utilization_pct: null, updated_at: null } },
          breaker: { paused: true, until: NOW + 60, types: ["five_hour"] },
          governor: { weekly_pct: 73, enabled: true, policy_broken: false },
        }}
      />
    );
    expect(html).toContain("Account limit: dispatch paused until 2026-10-03");
    expect(html).toContain(", five_hour");
    expect(html).toContain("41%");
    expect(html).toContain("no reading");
    expect(html).toContain("Weekly usage 73%");
  });
  it("renders 'governor off' and 'policy broken'", () => {
    const off = renderToStaticMarkup(
      <AccountStrip now={NOW} usage={{ windows: {}, breaker: { paused: false, until: null, types: [] }, governor: { weekly_pct: null, enabled: false, policy_broken: false } }} />
    );
    expect(off).toContain("governor off");
    expect(off).not.toContain("Account limit");
    const broken = renderToStaticMarkup(
      <AccountStrip now={NOW} usage={{ governor: { weekly_pct: 10, enabled: false, policy_broken: true } }} />
    );
    expect(broken).toContain("policy broken");
  });
});

describe("TokenBudgets", () => {
  it("renders the empty state", () => {
    expect(renderToStaticMarkup(<TokenBudgets budgets={{}} />)).toContain("No token budgets set.");
    expect(renderToStaticMarkup(<TokenBudgets budgets={undefined} />)).toContain("No token budgets set.");
  });
  it("renders a bar with percent and paused since", () => {
    const html = renderToStaticMarkup(
      <TokenBudgets budgets={{ alpha: { used: 900_000, budget: 1_000_000, paused_since: NOW, until: null } }} />
    );
    expect(html).toContain("90%");
    expect(html).toContain("paused since 2026-10-03");
  });
});
