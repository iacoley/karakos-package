#!/usr/bin/env bash
# Source this to get WORKSPACE_ROOT (and KARAKOS_HOME) set. Same order as lib/paths.py:
# KARAKOS_HOME, then WORKSPACE_ROOT, then the checkout this file sits in.
# Usage: source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"   (path relative to the caller)
_karakos_checkout="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE_ROOT="${KARAKOS_HOME:-${WORKSPACE_ROOT:-$_karakos_checkout}}"
KARAKOS_HOME="$WORKSPACE_ROOT"
unset _karakos_checkout
export WORKSPACE_ROOT KARAKOS_HOME
