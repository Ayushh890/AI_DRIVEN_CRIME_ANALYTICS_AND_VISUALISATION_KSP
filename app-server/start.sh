#!/usr/bin/env sh
# Catalyst AppSail start command for the KSP CIP server.
# Catalyst runs this via `sh -c 'sh ./start.sh'` (see app-config.json).
#
# Because AppSail filesystems are ephemeral we:
#   1) Regenerate the synthetic SQLite database on cold start (fast: ~10s for 20k FIRs).
#   2) Point KSP_DB at /tmp so we're on a writable, per-container path.
#   3) Bind uvicorn to X_ZOHO_CATALYST_LISTEN_PORT (Catalyst's mandated port env var).

set -e

: "${X_ZOHO_CATALYST_LISTEN_PORT:=9000}"
: "${KSP_DB:=/tmp/ksp.db}"
: "${KSP_FIRS_ON_BOOT:=20000}"

# ---- rebuild synthetic DB if it doesn't already exist in /tmp ----
if [ ! -f "$KSP_DB" ]; then
    echo "▶ generating synthetic KSP DB ($KSP_FIRS_ON_BOOT FIRs) at $KSP_DB"
    # generate_data.py resolves data/schema.sql relative to cwd — run from buildPath root.
    python3 scripts/generate_data.py --db "$KSP_DB" --firs "$KSP_FIRS_ON_BOOT"
else
    echo "▶ DB present at $KSP_DB — skipping regeneration"
fi

export KSP_DB

echo "▶ starting uvicorn on 0.0.0.0:$X_ZOHO_CATALYST_LISTEN_PORT"
exec python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port "$X_ZOHO_CATALYST_LISTEN_PORT" \
    --workers 1 \
    --log-level info
