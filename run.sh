#!/usr/bin/env bash
# KSP CIP one-command launcher.
#
#   ./run.sh              # install deps if missing, build DB if missing, start server on :8000
#   ./run.sh --rebuild    # force-regenerate synthetic DB (default 20k FIRs)
#   ./run.sh --firs 50000 # generate more FIRs at build time
#   ./run.sh --port 9000  # bind a different port
#   ./run.sh --stop       # stop the running instance (if started via this script)
#
# Environment overrides (optional):
#   KSP_LLM_BACKEND=ollama  OLLAMA_HOST=http://localhost:11434  KSP_LLM_MODEL=llama3.2:1b
#   KSP_LLM_BACKEND=openai  OPENAI_BASE_URL=http://.../v1       KSP_LLM_MODEL=phi-3-mini

set -Eeuo pipefail

# Resolve to script directory so relative paths work no matter where you invoke from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- defaults -----------------------------------------------------------------------
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
FIRS="${FIRS:-20000}"
REBUILD=0
STOP=0
PID_FILE="$SCRIPT_DIR/.ksp.pid"
LOG_FILE="$SCRIPT_DIR/.ksp.log"

# --- args ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rebuild)  REBUILD=1; shift ;;
        --stop)     STOP=1; shift ;;
        --firs)     FIRS="$2"; shift 2 ;;
        --port)     PORT="$2"; shift 2 ;;
        --host)     HOST="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# --- helpers ------------------------------------------------------------------------
color() { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
say()  { color "1;36" "▶ $*"; }
ok()   { color "1;32" "✓ $*"; }
warn() { color "1;33" "! $*"; }
die()  { color "1;31" "✗ $*"; exit 1; }

# --- stop mode ----------------------------------------------------------------------
if [[ "$STOP" == "1" ]]; then
    if [[ -f "$PID_FILE" ]]; then
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" && ok "stopped PID $pid"
        else
            warn "PID $pid not running"
        fi
        rm -f "$PID_FILE"
    else
        warn "no PID file — nothing to stop"
    fi
    exit 0
fi

# --- refuse to double-start ---------------------------------------------------------
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    die "server already running as PID $(cat "$PID_FILE"). Use ./run.sh --stop first."
fi

# --- python & pip -------------------------------------------------------------------
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || die "python3 not found. Install Python 3.10+."

$PYTHON - <<'PY' || die "Python 3.10+ required"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

# --- dependencies (install if missing) ----------------------------------------------
need_install=0
$PYTHON - <<'PY' 2>/dev/null || need_install=1
import fastapi, uvicorn, httpx, networkx  # noqa: F401
PY

if [[ "$need_install" == "1" ]]; then
    say "installing Python dependencies…"
    $PYTHON -m pip install --quiet -r backend/requirements.txt \
        || die "pip install failed. Try: pip install -r backend/requirements.txt"
    ok "dependencies installed"
else
    ok "dependencies present"
fi

# --- build synthetic DB if missing --------------------------------------------------
DB_PATH="$SCRIPT_DIR/data/ksp.db"
if [[ "$REBUILD" == "1" ]] || [[ ! -f "$DB_PATH" ]]; then
    say "generating synthetic KSP database ($FIRS FIRs)…"
    $PYTHON scripts/generate_data.py --db data/ksp.db --firs "$FIRS"
    ok "database built at $DB_PATH"
else
    ok "database present ($(du -h "$DB_PATH" | cut -f1))"
fi

# --- start server -------------------------------------------------------------------
export KSP_DB="$DB_PATH"

say "starting KSP CIP on http://${HOST}:${PORT}"
: > "$LOG_FILE"
nohup $PYTHON -m uvicorn backend.main:app \
    --host "$HOST" --port "$PORT" \
    >"$LOG_FILE" 2>&1 &

SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# --- wait for readiness -------------------------------------------------------------
for i in {1..30}; do
    sleep 0.5
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "--- last log lines ---"
        tail -20 "$LOG_FILE"
        die "server failed to start (see $LOG_FILE)"
    fi
done

curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 || {
    tail -20 "$LOG_FILE"
    die "server did not come up within 15s"
}

ok "server ready · PID $SERVER_PID"
color "1;36" ""
color "1;36" "  Open   →  http://localhost:${PORT}"
color "1;36" "  API    →  http://localhost:${PORT}/docs"
color "1;36" "  Log    →  $LOG_FILE"
color "1;36" "  Stop   →  ./run.sh --stop"
color "1;36" ""

# --- LLM backend hint ---------------------------------------------------------------
backend_json=$(curl -fsS "http://127.0.0.1:${PORT}/assistant/health" 2>/dev/null || echo '{}')
if echo "$backend_json" | grep -q '"offline"'; then
    warn "Assistant is in OFFLINE mode (rule-based intent templates)."
    warn "For LLM answers, install Ollama and re-run:"
    warn "  ollama pull llama3.2:1b"
    warn "  KSP_LLM_BACKEND=ollama OLLAMA_HOST=http://localhost:11434 KSP_LLM_MODEL=llama3.2:1b ./run.sh"
fi
