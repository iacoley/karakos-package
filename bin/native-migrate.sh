#!/usr/bin/env bash
# Move a container install's state into this checkout so it can run natively.
#   bin/native-migrate.sh [--dry-run] [--image IMAGE] [--project NAME]
# Copies the three named volumes (<project>_karakos-data, -logs, -inbox) into
# data/, logs/ and inbox/ with cp -a, and drops WORKSPACE_ROOT from config/.env.
# The container must be stopped. Volumes are never removed: keep them until a
# day on native has gone fine.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"

DRY=0; IMAGE=""; PROJECT="config"
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --image) IMAGE="${2:?--image needs a value}"; shift ;;
        --project) PROJECT="${2:?--project needs a value}"; shift ;;
        *) echo "native-migrate: unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

command -v docker >/dev/null || { echo "native-migrate: docker not found (needed to read the volumes)" >&2; exit 1; }
cid=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=karakos" || true)
[ -z "$cid" ] || { echo "native-migrate: the container is still running; stop it first (make down)" >&2; exit 1; }

if [ -z "$IMAGE" ]; then
    IMAGE=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.service=karakos" \
        --format '{{.Image}}' | head -1)
fi
[ -n "$IMAGE" ] || { echo "native-migrate: no karakos container found for project '$PROJECT'; pass --image" >&2; exit 1; }

for kind in data logs inbox; do
    vol="${PROJECT}_karakos-$kind"
    docker volume inspect "$vol" >/dev/null 2>&1 || { echo "native-migrate: volume $vol not found" >&2; exit 1; }
    dest="$WORKSPACE_ROOT/$kind"
    if [ -d "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
        echo "native-migrate: $dest is not empty; refusing to overwrite" >&2; exit 1
    fi
done

for kind in data logs inbox; do
    vol="${PROJECT}_karakos-$kind"
    dest="$WORKSPACE_ROOT/$kind"
    echo "copy $vol -> $dest"
    [ "$DRY" = 1 ] && continue
    mkdir -p "$dest"
    docker run --rm --entrypoint cp -v "$vol:/from:ro" -v "$dest:/to" "$IMAGE" -a /from/. /to/
done

ENV_FILE="$WORKSPACE_ROOT/config/.env"
if [ -f "$ENV_FILE" ] && grep -q '^WORKSPACE_ROOT=' "$ENV_FILE"; then
    echo "remove WORKSPACE_ROOT from $ENV_FILE"
    [ "$DRY" = 1 ] || sed -i '/^WORKSPACE_ROOT=/d' "$ENV_FILE"
fi

[ "$DRY" = 1 ] && { echo "dry run: nothing changed"; exit 0; }
echo "done. Volumes ${PROJECT}_karakos-{data,logs,inbox} are kept; remove them after a day on native."
echo "next: bin/native-bootstrap.sh, then the units (docs/NATIVE.md)"
