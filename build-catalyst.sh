#!/usr/bin/env bash
# Sync the app source into app-server/ so `catalyst deploy` can bundle it.
# Run this before every Catalyst deployment when you've made local changes.
#
#   ./build-catalyst.sh          # sync sources
#   ./build-catalyst.sh --deploy # sync then invoke catalyst deploy

set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

DEPLOY=0
[[ "${1:-}" == "--deploy" ]] && DEPLOY=1

echo "▶ syncing source → app-server/"

mkdir -p app-server/{backend,frontend,data,scripts}

# Wipe out any previous copies (except the Catalyst-only files we generated) then copy.
find app-server/backend -mindepth 1 -delete 2>/dev/null || true
find app-server/frontend -mindepth 1 -delete 2>/dev/null || true
find app-server/scripts -mindepth 1 -delete 2>/dev/null || true

cp -R backend/. app-server/backend/
cp -R frontend/. app-server/frontend/
cp data/schema.sql app-server/data/schema.sql
cp scripts/generate_data.py app-server/scripts/generate_data.py
cp backend/requirements.txt app-server/requirements.txt

# Purge caches Catalyst would otherwise upload.
find app-server -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find app-server -type f -name '*.pyc' -delete 2>/dev/null || true

# Do NOT ship the local .db — the container regenerates on first boot.
rm -f app-server/data/ksp.db

echo "✓ sync complete"
ls -la app-server/

if [[ "$DEPLOY" == "1" ]]; then
    command -v catalyst >/dev/null || { echo "catalyst CLI not installed. Run: npm install -g zcatalyst-cli"; exit 1; }
    echo "▶ catalyst deploy"
    catalyst deploy
fi
