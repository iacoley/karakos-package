#!/usr/bin/env bash
# Native (no-Docker) install for a dedicated host. Run from the checkout after
# ./setup.sh --native has written config/. Safe to re-run.
#   bin/native-install.sh [--skip-dashboard] [--no-start]
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"
cd "$WORKSPACE_ROOT"

SKIP_DASH=0; NO_START=0
for a in "$@"; do
    case "$a" in
        --skip-dashboard) SKIP_DASH=1 ;;
        --no-start) NO_START=1 ;;
        -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "native-install: unknown option $a" >&2; exit 2 ;;
    esac
done
say() { echo "==> $*"; }
die() { echo "native-install: $*" >&2; exit 1; }

# --- prerequisites ---
PY="${KARAKOS_PYTHON:-python3}"
command -v "$PY" >/dev/null || die "python3 not found (3.11 or newer needed)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "$("$PY" --version) is too old; 3.11 or newer is needed (set KARAKOS_PYTHON to another interpreter)"
command -v git >/dev/null || die "git not found"
command -v claude >/dev/null || die "the Claude CLI is not on PATH (npm install -g @anthropic-ai/claude-code)"
if [ "$SKIP_DASH" = 0 ]; then
    command -v node >/dev/null && command -v npm >/dev/null || die "node and npm are needed for the dashboard (or pass --skip-dashboard)"
fi
command -v systemctl >/dev/null || die "systemctl not found; native mode needs systemd"
systemctl --user show-environment >/dev/null 2>&1 \
    || die "no systemd user session. On WSL enable systemd in /etc/wsl.conf; on a server run: loginctl enable-linger \$USER"
[ -f config/.env ] || die "config/.env missing; run ./setup.sh --native first"

# --- python environment ---
say "Python environment (.venv)"
[ -x .venv/bin/python3 ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# --- dashboard ---
if [ "$SKIP_DASH" = 0 ]; then
    say "Dashboard build"
    (cd dashboard && npm ci --no-audit --no-fund && npm run build)
fi

# --- one-time setup the container entrypoint would do ---
bash bin/native-bootstrap.sh

# --- units ---
say "systemd user units"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
.venv/bin/python lib/native_units.py --home "$WORKSPACE_ROOT" --python "$WORKSPACE_ROOT/.venv/bin/python" --out "$UNIT_DIR" >/dev/null
systemctl --user daemon-reload
systemctl --user enable karakos.target >/dev/null
if command -v loginctl >/dev/null; then
    loginctl enable-linger "$USER" 2>/dev/null || echo "WARNING: could not enable linger; units stop at logout" >&2
fi
if [ "$NO_START" = 0 ]; then
    systemctl --user restart karakos.target
    say "Started. Logs: journalctl --user -u 'karakos-*' -f"
else
    say "Installed, not started. Start with: systemctl --user start karakos.target"
fi
