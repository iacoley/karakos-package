/**
 * Agent roster types. /api/agents asks `getAgentStatusAdapter()`
 * (lib/agentAdapter.ts) for the roster: lib/agentStatus.package.ts reads the
 * agent-server /agents and /health plus the registry file at
 * KARAKOS_REGISTRY_PATH. Contract: docs/package-backend-contract.md.
 */

/** Why a shard (or agent) is held: 2.7 `reason` is breaker, budget or
 * governor; `until` is epoch seconds, or null when it lasts until usage drops. */
export type PausedInfo = { reason: "breaker" | "budget" | "governor" | string; until: number | null };

/** One shard row of GET /agents (agent-server 2.1), defaults filled. */
export interface AgentShardRow {
  id: string;
  is_default: boolean;
  /** Open string: IDLE, PROCESSING, ERROR_RECOVERY, UNKNOWN, or anything newer. */
  state: string;
  alive: boolean;
  pid: number | null;
  session_id: string;
  queue_depth: number;
  /** 0 = unknown. */
  context_tokens: number;
  channels: string[];
  last_channel: string | null;
  paused: PausedInfo | null;
  /** Whole seconds since the current subprocess spawned; null when no live process. */
  uptime_seconds?: number | null;
  /** Epoch seconds of the last turn that ended without is_error; null until one does. */
  last_turn_at?: number | null;
  [key: string]: unknown;
}

/** One row of GET /api/agents. */
export interface AgentRosterRow {
  name: string;
  state: string;
  host: string;
  label?: string;
  messages_processed: number;
  session_age_seconds?: number;
  token_usage?: { input: number; output: number };
  cost?: number;
  subprocess_alive?: boolean;
  subprocess_pid?: number;
  queue_depths: Record<string, number>;
  total_pending: number;
  session_id?: string;
  compaction_count: number;
  last_message?: string;
  last_message_at?: string;
  last_message_direction?: string;
  /** agent-server `context_tokens` (max over shards; 0 = unknown). */
  context_tokens?: number;
  /** the agent's shard rows (agent-server `shards`, plus registry
   * channels and registry-only shards). */
  shards?: AgentShardRow[];
  /** false for an agent the registry declares but the server has not reported. */
  reported?: boolean;
  /** set when every shard of the agent is paused. */
  paused?: PausedInfo | null;
  /** registry `role` (primary, monitor, builder, reviewer, custom). */
  role?: string;
  /** false for relay agents not meant for direct chat. */
  dashboard_chat?: boolean;
  /** configured model. */
  model?: string | null;
}

export interface AgentStatusAdapter {
  /** The roster visible to the caller. `allowed` is the per-account agent
   * allowlist (lib/permissions.ts); the route supplies it. Throws if the
   * primary source is unreachable (the route turns that into a 500). */
  listAgents(allowed: (name: string) => boolean): Promise<AgentRosterRow[]>;
}
