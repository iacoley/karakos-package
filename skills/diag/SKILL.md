---
name: diag
description: Diagnose a Karakos install from the inside with the read-only self-diagnose and self-test scripts, then read the findings
---

Use this before changing anything on a sick install, and after a fix to prove it worked.
Both scripts are read-only.

## 1. Run the diagnosis

```bash
python3 bin/self-diagnose.py            # human readable
python3 bin/self-diagnose.py --json     # for parsing
python3 bin/self-diagnose.py --sections health,disk       # a subset
```

Sections: `health`, `findings`, `heartbeats`, `hook`, `disk`, `schema`, `logs`.
Exit 0 means nothing worse than info; exit 1 means at least one warn or critical.
Findings are ranked critical, then warn, then info. Read the critical ones first.

## 2. Run the self-test when the diagnosis is clean but something still feels wrong

```bash
python3 bin/self-test.py
```

It runs the diagnosis, then starts each MCP server over stdio and calls one
read-only tool on each. It prints PASS or FAIL per step. A FAIL names the server
that did not answer.

## 3. Read the findings in context

- A stale heartbeat for one agent: use the `restart-and-recover` skill.
- Disk above the warning line: report it. Do not delete data to make the number go down.
- Schema stamp missing or wrong: stop. That is the migrator's job (`docs/UPGRADING.md`).
- Many log errors: `tail_log` the named service and find the first one, not the loudest.

## 4. Report

State which sections ran, what each found, what you changed (nothing, unless the
owner asked), and what the scripts cannot see (the host, Docker, Discord itself).
Run it again after a fix and quote the second result.
