import { alpha, StatusDot, Tag, WARN } from "@/app/components/lamplight-ui";
import type { AgentShardRow } from "@/lib/agentStatus";
import { formatTokens, pausedLabel, sortShardRows, stateTone } from "@/lib/fleetView";
import { TONE_COLOR } from "./tone";

export interface ShardAgent {
  name: string;
  label?: string;
  shards: AgentShardRow[];
}

const TH = "text-left px-3 py-2 text-[11px] uppercase font-medium";
// Under 640px each shard row stacks as label/value lines (the cell's data-label
// is drawn by ::before), so no column is clipped on a phone.
const TD =
  "px-3 py-2 text-sm max-sm:flex max-sm:justify-between max-sm:gap-3 max-sm:py-1 max-sm:before:content-[attr(data-label)] max-sm:before:text-[11px] max-sm:before:uppercase max-sm:before:text-[var(--text-muted)]";

/** Rows for one agent's shards (paused first). Used by the fleet table and
 * the agent cards. `now` is epoch seconds. */
export function ShardRows({ shards, now }: { shards: AgentShardRow[]; now: number }) {
  return (
    <>
      {sortShardRows(shards).map((s) => {
        const paused = pausedLabel(s.paused, now);
        return (
          <tr key={s.id} className="max-sm:block max-sm:py-2" style={{ borderTop: "1px solid var(--border)" }}>
            <td data-label="Shard" className={`${TD} font-mono`} style={{ color: "var(--text-primary)" }}>
              {s.id}
              {s.is_default && <span className="ml-2"><Tag label="default" color="var(--text-muted)" /></span>}
            </td>
            <td data-label="State" className={TD}>
              <span className="inline-flex items-center gap-2" style={{ color: TONE_COLOR[stateTone(s.state)] }}>
                <StatusDot color={TONE_COLOR[stateTone(s.state)]} />
                {s.state}
              </span>
            </td>
            <td
              data-label="Alive"
              className={TD}
              style={s.alive ? { color: "var(--text-secondary)" } : { color: "var(--danger, #c0392b)", fontWeight: 700 }}
            >
              {s.alive ? "yes" : "no"}
            </td>
            <td data-label="PID" className={TD} style={{ color: "var(--text-secondary)" }}>{s.pid ?? "-"}</td>
            <td data-label="Up" className={TD} style={{ color: "var(--text-secondary)" }}>
              {s.uptime_seconds == null ? "-" : `${Math.floor(s.uptime_seconds / 60)}m`}
            </td>
            <td data-label="Last turn" className={TD} style={{ color: "var(--text-secondary)" }}>
              {s.last_turn_at == null ? "never" : new Date(s.last_turn_at * 1000).toISOString().slice(0, 19) + "Z"}
            </td>
            <td data-label="Paused" className={TD} style={{ color: paused ? WARN : "var(--text-muted)" }}>
              {paused}
            </td>
            <td data-label="Context" className={TD} style={{ color: "var(--text-secondary)" }}>{formatTokens(s.context_tokens)}</td>
            <td data-label="Queue" className={TD} style={{ color: "var(--text-secondary)" }}>{s.queue_depth}</td>
            <td data-label="Channels" className={TD} style={{ color: "var(--text-secondary)" }}>
              {s.channels.length ? s.channels.join(", ") : ""}
            </td>
            <td data-label="Last channel" className={TD} style={{ color: "var(--text-secondary)" }}>{s.last_channel ?? ""}</td>
          </tr>
        );
      })}
    </>
  );
}

export function ShardHead() {
  return (
    <thead className="max-sm:hidden" style={{ backgroundColor: "var(--bg-elevated)", color: "var(--text-muted)" }}>
      <tr>
        <th className={TH}>Shard</th>
        <th className={TH}>State</th>
        <th className={TH}>Alive</th>
        <th className={TH}>PID</th>
        <th className={TH}>Up</th>
        <th className={TH}>Last turn</th>
        <th className={TH}>Paused</th>
        <th className={TH}>Context</th>
        <th className={TH}>Queue</th>
        <th className={TH}>Channels</th>
        <th className={TH}>Last channel</th>
      </tr>
    </thead>
  );
}

/** Shard rows grouped by agent. */
export default function ShardTable({ agents, now }: { agents: ShardAgent[]; now: number }) {
  return (
    <section aria-label="Shards" className="mb-8">
      <h2 className="text-lg font-semibold mb-3" style={{ color: "var(--text-primary)" }}>Shards</h2>
      {agents.length === 0 && <p className="text-sm" style={{ color: "var(--text-muted)" }}>No agents reported.</p>}
      <div className="grid gap-4">
        {agents.map((a) => (
          <div
            key={a.name}
            className="rounded-lg border overflow-hidden"
            style={{ backgroundColor: "var(--bg-surface)", borderColor: alpha("var(--ink)", 13) }}
          >
            <div className="px-3 py-2 text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
              {a.label && a.label !== a.name ? `${a.label} (${a.name})` : a.name}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full sm:min-w-[640px] max-sm:block">
                <ShardHead />
                <tbody className="max-sm:block">
                  <ShardRows shards={a.shards} now={now} />
                </tbody>
              </table>
            </div>
            {a.shards.length === 0 && (
              <p className="px-3 py-2 text-sm" style={{ color: "var(--text-muted)" }}>No shard rows.</p>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
