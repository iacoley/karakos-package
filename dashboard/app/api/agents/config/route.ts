import { NextRequest, NextResponse } from "next/server";
import { agentFetch, isAuthenticated, unauthorizedResponse } from "@/lib/api";

// Agent *configuration* — model, max_turns, timeout — as opposed to the
// runtime state that /api/agents reports.
//
// These are two different upstream endpoints and neither substitutes for the
// other. /api/agents reshapes the agent-server's /status (live state, queue
// depths, token usage); this proxies /agents (what config/agents.json says).
// The settings page previously read /api/agents and rendered cfg.model,
// cfg.max_turns and cfg.timeout off it — none of which /status carries — so
// every value on the page was undefined.
//
// Both endpoints return `{ agents: [...] }`, an ARRAY. Consumers must not
// Object.keys() it; see tests/test_dashboard_agents_contract.py.
export async function GET(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value || "")) {
    return unauthorizedResponse();
  }

  try {
    const response = await agentFetch("/agents");
    const data = await response.json();
    return NextResponse.json({ agents: data.agents ?? [] });
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to fetch agent config" },
      { status: 500 }
    );
  }
}
