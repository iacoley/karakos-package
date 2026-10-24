#!/usr/bin/env bash
# Native mode upgrade: replaces pulling a new image. Fetches the release,
# refreshes the venv and dashboard, re-runs the bootstrap and restarts the units.
#   bin/native-upgrade.sh            # fast-forward the current branch
#   KARAKOS_REF=v2.5.0 bin/native-upgrade.sh   # a tag or branch
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"
cd "$WORKSPACE_ROOT"
say() { echo "==> $*"; }
die() { echo "native-upgrade: $*" >&2; exit 1; }

[ -d .git ] || die "$WORKSPACE_ROOT is not a git checkout"
# hooks-sync rewrites this tracked file on every bootstrap; it is regenerated below.
git checkout -- config/claude-settings.json 2>/dev/null || true
[ -z "$(git status --porcelain --untracked-files=no)" ] \
    || die "tracked files have local changes; commit or stash them first: $(git status --porcelain --untracked-files=no | head -5 | tr '\n' ' ')"

git fetch -q --tags origin
if [ -n "${KARAKOS_REF:-}" ]; then
    say "Checking out $KARAKOS_REF"
    git checkout -q "$KARAKOS_REF"
else
    branch=$(git symbolic-ref -q --short HEAD) || die "detached HEAD; set KARAKOS_REF or check out a branch"
    say "Fast-forwarding $branch"
    git merge -q --ff-only "origin/$branch"
fi
say "Now at $(git describe --tags --always)"

.venv/bin/pip install -q -r requirements.txt
if [ "${KARAKOS_SKIP_DASHBOARD:-0}" != 1 ]; then
    (cd dashboard && npm ci --no-audit --no-fund && npm run build)
fi
bash bin/native-bootstrap.sh
.venv/bin/python lib/native_units.py --home "$WORKSPACE_ROOT" --python "$WORKSPACE_ROOT/.venv/bin/python" \
    --out "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" >/dev/null
systemctl --user daemon-reload
systemctl --user restart karakos.target
say "Restarted."
