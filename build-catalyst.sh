#!/usr/bin/env bash
# Sync the app source into app-server/ so `catalyst deploy` can bundle it.
# If .env.deploy exists (created by ./enable-online-llm.sh) its KEY=VALUE lines
# are merged into app-server/app-config.json.env_variables JUST for the deploy,
# then reverted so the committed app-config.json never contains secrets.
#
#   ./build-catalyst.sh          # sync sources only
#   ./build-catalyst.sh --deploy # sync + catalyst deploy + revert config

set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

DEPLOY=0
[[ "${1:-}" == "--deploy" ]] && DEPLOY=1

echo "▶ syncing source → app-server/"

mkdir -p app-server/{backend,frontend,data,scripts}
find app-server/backend  -mindepth 1 -delete 2>/dev/null || true
find app-server/frontend -mindepth 1 -delete 2>/dev/null || true
find app-server/scripts  -mindepth 1 -delete 2>/dev/null || true

cp -R backend/. app-server/backend/
cp -R frontend/. app-server/frontend/
cp data/schema.sql app-server/data/schema.sql
cp scripts/generate_data.py app-server/scripts/generate_data.py
cp backend/requirements.txt app-server/requirements.txt

find app-server -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find app-server -type f -name '*.pyc' -delete 2>/dev/null || true
rm -f app-server/data/ksp.db

echo "✓ sync complete"

if [[ "$DEPLOY" != "1" ]]; then
    exit 0
fi

command -v catalyst >/dev/null || {
    echo "❌ catalyst CLI missing. add ~/.npm-global/bin to PATH."; exit 2; }

CONFIG=app-server/app-config.json
BACKUP=$(mktemp)
cp "$CONFIG" "$BACKUP"
cleanup() {
    if [[ -f "$BACKUP" ]]; then
        mv "$BACKUP" "$CONFIG"
        echo "▶ reverted $CONFIG to non-secret form"
    fi
}
trap cleanup EXIT INT TERM

if [[ -f .env.deploy ]]; then
    command -v jq >/dev/null || { echo "❌ jq required to merge .env.deploy. Install with: apt install jq (or brew install jq)"; exit 2; }
    echo "▶ merging .env.deploy → $CONFIG.env_variables (deploy-only)"
    # Build a JSON object from KEY=VALUE lines, then jq-merge into env_variables.
    ENV_JSON=$(python3 - <<'PY'
import json, os
env = {}
with open(".env.deploy") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
print(json.dumps(env))
PY
)
    tmp=$(mktemp)
    jq --argjson e "$ENV_JSON" '.env_variables = (.env_variables + $e)' "$CONFIG" > "$tmp"
    mv "$tmp" "$CONFIG"
fi

echo "▶ catalyst deploy"
catalyst deploy --only appsail
