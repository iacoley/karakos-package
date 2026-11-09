# Agent checks

A manifest of the capabilities an install must have, and a runner that checks
them. `make agent-check` runs the programmatic checks and prints one line per
capability.

```bash
make agent-check                                # every programmatic check
python3 bin/agent-check.py --only ask-roundtrip,outbox-roundtrip
python3 bin/agent-check.py --layer programmatic --json
```

Each line is `PASS`, `FAIL` or `SKIP`, the capability id, the detail, and the time
taken. The exit code is 0 when nothing FAILs and 1 otherwise. A skip never fails the
run. `--json` prints `{version, ran_at, results: [{id, layer, status, detail, ms}]}`,
where `status` is `PASS`, `FAIL`, or `skip` (lowercase, for a skipped entry). Every run appends
that document as one line to `data/agent-checks/results.jsonl` under the install's
data directory.

The manifest is `tests/capabilities/manifest.yaml`. Each entry has `id`, `layer`,
`desc`, `since` and `check`. `check` names a function in `lib/agent_checks.py`.
Entries for MCP tools also carry `covers:`, a list of tool names from
`tests/capabilities/expected_tools.json`.

## Layers

- **programmatic** (phase 1, shipped). A function asserts something with no model
  call: the MCP tool surface, the hook wiring, the scheduler table, the outbox, the
  graph memory, the ask flow, embed parsing, the live agent server, data freshness
  and the schema stamp.
- **agentic** (phase 2, planned). Model-driven checks. Entries in this layer are
  reported as `SKIP` with `skipped: phase 2` until phase 2 lands.

## Adding a capability

1. Write the check in `lib/agent_checks.py`. Take the workspace path, return
   `(ok, detail)`, and decorate it with `@_safe`. Return `ok=None` to report a skip
   (nothing to test here). Do every write in a temp directory.
2. Add an entry to `tests/capabilities/manifest.yaml` with `layer: programmatic`,
   `since` set to the version it ships in, and `check` set to the function name.
3. If the entry verifies MCP tools, list them under `covers:`. When a tool's name or
   required arguments change, update `tests/capabilities/expected_tools.json` by
   hand: it is generated from the servers' `tools/list`, and the tests fail if
   the file and the manifest disagree.
4. Run `python3 -m pytest tests/test_agent_checks.py -q`.

## Safety rules

- The runner only reads install state. Its one write is the results line above.
- Every write a check makes goes to a temp directory. The roundtrip checks have a
  test that the install's `data/` is not touched.
- Phase 1 calls no model.
- A check never raises. An exception becomes a `FAIL` whose detail is the error text.
- A check that starts a process (the MCP servers, through `bin/self-test.py`'s
  `Server`) gets a 20 second reply timeout.
- The ask check sends its Discord REST calls to a local stub on `127.0.0.1`. No real
  Discord call is made.
- A check that talks to a live agent server only sends `GET` requests to `/health` and
  `/agents`. With no server answering, it reports `SKIP` with `no live server; skipped`.
  A skip is not a failure.
- `bin/self-test.py` and `bin/self-diagnose.py` are imported as they are. They are not
  changed.

## Known limits

- `scheduler-jobs` registers the job table into a throwaway scheduler, using the same
  `apply_schedule` that `bin/scheduler.py` uses. It does not read the live
  `data/health/scheduler-jobs.json`.
- `hooks-installed` takes its script list from `bin/hooks-sync.py`'s `desired()`,
  given the flags in `config/hooks.json`. That file names flags, not scripts.
- `health-files-fresh` skips health files that match no entry in the job table, and
  names them in the detail.
- The runner creates `data/agent-checks/` on first use. `schema-stamp` does not count
  that directory as install state.
- `session-finalize-endpoint` checks whichever server `AGENT_SERVER_URL` or
  `AGENT_SERVER_PORT` points to. On a host with a server from another install, it
  reports on that server.
