---
name: worktree-setup
description: Set up a separate working copy for upstream package work so the live install directory is never touched
---

The install directory carries generated state (`config/.env`, `config/agents.yaml`,
`config/channels.json`, `agents/<id>/`), and the container mounts parts of it. Switching
branches there changes files the running system reads. Do upstream work in a separate
working copy.

## 1. Pick a place that survives restarts

Use a directory under `data/` (a named volume), for example `data/work/`. A path in
`/tmp` or a home directory can vanish when the container is rebuilt, and with it any
unpushed commit.

## 2. Create the working copy

From a fork you can push to:

```bash
git clone https://github.com/<you>/karakos-package.git data/work/karakos-package
cd data/work/karakos-package
git remote add upstream https://github.com/mcarmody/karakos-package.git
git fetch upstream
git switch -c fix/short-description upstream/develop
```

Or, from an existing install clone that has your fork added as a remote named `fork`:

```bash
git fetch origin develop
git worktree add -b fix/short-description data/work/karakos-pr origin/develop
# push with: git push -u fork fix/short-description
```

Every PR targets `develop`. Never push to `main` or `develop`.

## 3. Set the identity and the checks

- `git config user.name` and `user.email` must be set by the owner's choice, not guessed.
  Use the owner's noreply address if they have one.
- Install the test dependencies in a venv next to the working copy, not in the system
  Python: `pip install -r requirements.txt pytest`.

## 4. Work in small, pushed steps

- One logical change per PR, with tests. Stage files by name, never `git add -A`.
- Run only the tests you touched, chained so a failure stops the push:
  `python3 -m pytest tests/test_x.py -q && git push`.
- Run `bash system/check-coupling.sh` (it must print `coupling: clean`) and
  `python3 -m pytest tests/test_docs.py -q` after any docs change.
- Push the branch early. Unpushed work in a working copy is one cleanup away from gone.
- Rebase on `upstream/develop` (or `origin/develop`) before asking for review.

## 5. Protected paths

Tier 1 paths (`system/`, `config/`, `bin/agent-server.py`, `bin/relay.py` and the others
listed in `config/protected-paths.json`) need the maintainer's explicit go-ahead and a
review note. If a commit is blocked, stop and report the path. Do not use `--no-verify`.

## 6. Clean up

When the PR merges, remove the working copy (`git worktree remove` for a worktree) and
delete the merged branch from your fork.
