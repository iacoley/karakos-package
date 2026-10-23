---
name: skill-author
description: Add your own skill to an install, load it, and validate it; what stays local and what goes upstream
---

Two kinds of skill live under `skills/`. Pick the smaller one that does the job.

- **Procedure skill**: a `SKILL.md` with `name` and `description` frontmatter and no
  `tools.json`. Instructions only. Agents find it with the `skill` tool. Choose this when
  the job is "do these steps carefully", which is most jobs.
- **Tool skill**: `tools.json` plus `scripts/`, discovered by `mcp/tools-server.py` and
  registered as MCP tools. Choose this only when an agent needs to call code.

This is not Claude Code's built-in Agent Skills feature, despite the shared name.

## Add a procedure skill

1. Make `skills/<name>/SKILL.md`. The directory name is lowercase letters, digits, `-`
   and `_`, and must equal `name` in the frontmatter.
2. Write the frontmatter, then the steps:

   ```markdown
   ---
   name: my-procedure
   description: One line saying when to use it
   ---

   1. First step ...
   ```

3. Check it: `skill {action: validate, name: my-procedure}`. Then
   `skill {action: list}` should show it with its description.

A procedure skill needs no reload; the next `skill list` finds it.

## Add a tool skill

1. Copy `skills/hello-world` and edit `tools.json` and the script (guide: `skills/README.md`).
2. Run `reload_agent` so the tools server rediscovers `skills/`.
3. Run `skill {action: validate, name: <name>}`. It checks `tools.json`, then runs each
   tool through `--test-tool` in a subprocess with a 20 second timeout.
4. A tool skill that ships code is not loaded on your word alone: ask the owner
   (`ask_user`) before relying on it.

## What stays local and what goes upstream

- A skill for your own install lives in `skills/` or in the private overlay
  (`docs/OVERLAY.md`). Keep household names, ids and secrets out of anything tracked.
- Anything that needs a change to `bin/`, `lib/` or `mcp/` is a platform change. It goes
  upstream as a PR against `develop`, with tests (see the `worktree-setup` skill).

## Finish

Report what you added, the `validate` output, and what you did not test.
