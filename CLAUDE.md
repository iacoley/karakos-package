# Karakos Package

Self-contained, installable multi-agent household assistant system powered by
Claude. See `README.md` for what it does and `docs/ARCHITECTURE.md` for how
the pieces fit together.

## Adding tools ("skills")

This repo has its own skill convention: `skills/<name>/tools.json` +
`skills/<name>/scripts/`, discovered by `mcp/tools-server.py` at startup and
registered as MCP tools. Full guide: `docs/EXTENDING.md` ("Adding a Skill")
and `skills/README.md`.

This is **not** Claude Code's built-in Agent Skills feature (a `SKILL.md`
file with YAML frontmatter under `.claude/skills/`). The two share a name
and nothing else — a frontmatter-only `SKILL.md` under `skills/` will not load;
`discover_skills()` needs `tools.json` and `scripts/`.

## MCP server registration

MCP servers Claude Code should see must be registered in the root
`.mcp.json`, not anywhere under `mcp/` — Claude Code only reads `.mcp.json`
from the project root.
