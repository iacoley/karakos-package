# Contributing to Karakos

## Getting set up

```bash
gh repo fork mcarmody/karakos-package --clone && cd karakos-package   # origin = your fork, upstream = this repo
git fetch upstream && git switch -c my-change upstream/develop
pip install -r requirements.txt pytest
```

(No `gh`? Fork on GitHub, clone the fork, `git remote add upstream
https://github.com/mcarmody/karakos-package.git`.) An agent working on an install made by
`install.sh` should read [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md#contribute-upstream);
the layout and conventions are in [AGENTS.md](AGENTS.md).

Docker (24+, Compose v2) is only needed for the fast/live tests that exercise
the built container — pure Python/shell tests run without it.

## Running tests

```bash
pytest                          # full suite
pytest -m "not slow"            # skip the Docker-dependent tests
pytest tests/test_setup.py -v   # a single file
```

`ci.yml` runs the same lint/syntax and test steps on every PR to `develop` and
`main` (and once a day on a schedule) — check it locally with `bash -n` on shell scripts and `python -m
py_compile` on Python scripts before opening a PR if you touched either.

## The dashboard

The web dashboard is `dashboard/` in this repo, a Next.js app with its own
`package.json` and lockfile. Work on it like any other part of the package:

```
cd dashboard
npm ci
npm test
npm run build
```

The CI `dashboard` job runs the same three commands; the `docker-smoke` job
builds the image, which builds `dashboard/` too. Neither needs a secret. See
`dashboard/README.md` for the environment it reads and "Dashboard build" in
`docs/EXTENDING.md` for how the image is built. A change to a route the
dashboard calls on the agent server must keep
`tests/test_agent_server_routes.py` green.

**Tests.** `tests/test_docs.py` checks that the docs match the code: links and
anchors, backticked paths, environment variables, registry keys, routes, MCP
tools, the migrator's `--help` flags, shell blocks, and the coupling denylist.
Change a flag or route and it tells you which doc to update. A few harness tests
time out under full-suite load and pass alone ([EXTENDING.md](docs/EXTENDING.md#writing-a-harness-test)).

## Branches

| Branch | Purpose | Changes through |
|---|---|---|
| `develop` | Integration. Every feature and fix lands here first. | PRs from forks or topic branches |
| `main` | Releases only. `install.sh` and `install.ps1` clone it. | A release PR from `develop`, approved by the maintainer |

Both branches are protected by rulesets: a PR is required; the checks
`lint-and-syntax`, `unit-tests` and `docker-smoke` must pass; force-push and
deletion are blocked. Nobody commits directly to either. GitHub's default branch
may still show `main`, so name the base explicitly.

## Making a change

1. Fork the repo and branch from `develop`.
2. Keep changes focused — one logical change per PR.
3. Add or update tests for behavior you change. Run the tests for what you touched,
   and `bash system/check-coupling.sh` (must print `coupling: clean`).
4. Push to your fork and open a PR **against `develop`**, filling in the PR template:
   `gh pr create --repo mcarmody/karakos-package --base develop`.
5. Make sure CI is green before requesting review. Fix failures with new commits;
   do not force-push a branch under review.
6. The maintainer reviews and merges. Do not merge your own PR.

Note `config/protected-paths.json`: some paths (`system/`, `config/`,
`bin/agent-server.py`, `bin/relay.py`, `bin/entrypoint.sh`,
`bin/scheduler.py`, `.karakos/`, `Dockerfile`) are tier-1 protected in
deployed instances — changes there get extra scrutiny since they affect
process lifecycle and security boundaries. In a deployed install the pre-commit
hook refuses them; if you are an agent and one blocks you, stop and report the path.

## Release process

Maintainers only. Releases go from `develop` to `main` and nowhere else.

1. **Prepare on `develop`.** Move the `## [Unreleased]` entries in `CHANGELOG.md`
   under a new `## [X.Y.Z] - YYYY-MM-DD` heading (three-part version; see the
   note at the top of the changelog), set `VERSION` to the release number, update
   the compare links at the bottom, and merge that through a normal PR. Run the real-CLI smoke by hand if a credential
   is available: `python3 -m pytest tests -q -m "slow and realcli"`.
2. **Open the release PR** from `develop` to `main`:
   `gh pr create --base main --head develop --title "Release vX.Y.Z"` with the changelog entry
   as the body. The required checks run; `release-gate.yml` (fresh install and
   upgrade from the container) also runs when a file that can break it changed.
3. **The maintainer approves and merges.** Use a merge commit: it keeps
   `develop`'s commits in `main`'s history, so the next release PR lists only new
   work. A squash re-lists everything already released.
4. **Tag `main`** at the merge commit: `git fetch origin && git tag -a vX.Y.Z origin/main -m "vX.Y.Z" && git push origin vX.Y.Z`.
5. **`release.yml` runs on the tag** (any `v*` tag). It calls the release gate, then builds
   the multi-arch image and pushes it to `ghcr.io/mcarmody/karakos` as `vX.Y`,
   `vX` and `latest` (there are no patch-level image tags). A tag whose gate fails publishes nothing. Check the run before
   announcing the release.

## Reporting bugs / requesting features

Use the issue templates on GitHub (Issues → New Issue). Include your OS,
Docker version, and relevant logs from `logs/` where applicable.
