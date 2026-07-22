#!/usr/bin/env bash
# Turn on the online LLM backend for the KSP CIP Assistant and redeploy.
#
# Usage:
#   ./enable-online-llm.sh groq  gsk_abcdef123456           # Groq (free tier, Llama-3.3 70B)
#   ./enable-online-llm.sh openai sk_openai_...  https://api.openai.com/v1  gpt-4o-mini
#   ./enable-online-llm.sh hf     hf_token       meta-llama/Llama-3.3-70B-Instruct
#   ./enable-online-llm.sh ollama http://your-ollama-host:11434  llama3.2:1b
#   ./enable-online-llm.sh --show                  # print current LLM env from app-config.json
#   ./enable-online-llm.sh --clear                 # revert to offline mode
#
# The script writes the env vars into app-server/app-config.json (never committed
# because .gitignore excludes the appsail vendored deps), then runs
# `catalyst deploy --only appsail`. Values are also written to .env.deploy for
# convenience.

set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CONFIG=app-server/app-config.json
DEPLOY_ENV=.env.deploy

require_jq() { command -v jq >/dev/null || { echo "❌ jq not installed — install with: apt install jq (or brew install jq)"; exit 2; }; }

case "${1:-}" in
"") sed -n '2,15p' "$0"; exit 0 ;;
--show)
    require_jq
    jq -r '.env_variables // {} | to_entries[] | "\(.key)=\(.value)"' "$CONFIG" | grep -E '^KSP_LLM_|^GROQ_|^OPENAI_|^HF_|^OLLAMA_' || echo '(no LLM env vars set)'
    exit 0 ;;
--clear)
    require_jq
    jq 'del(.env_variables.KSP_LLM_BACKEND, .env_variables.KSP_LLM_MODEL,
            .env_variables.GROQ_API_KEY,
            .env_variables.OPENAI_API_KEY, .env_variables.OPENAI_BASE_URL,
            .env_variables.HF_TOKEN,
            .env_variables.OLLAMA_HOST)' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
    rm -f "$DEPLOY_ENV"
    echo "✓ LLM env cleared. Redeploying…"
    ;;
groq)
    require_jq
    KEY="${2:?groq api key required}"
    MODEL="${3:-llama-3.3-70b-versatile}"
    jq --arg k "$KEY" --arg m "$MODEL" \
       '.env_variables += {KSP_LLM_BACKEND:"groq", GROQ_API_KEY:$k, KSP_LLM_MODEL:$m}' \
       "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
    { echo "KSP_LLM_BACKEND=groq"; echo "GROQ_API_KEY=$KEY"; echo "KSP_LLM_MODEL=$MODEL"; } > "$DEPLOY_ENV"
    echo "✓ Groq backend configured. Redeploying…"
    ;;
openai)
    require_jq
    KEY="${2:?openai api key required}"
    BASE="${3:-https://api.openai.com/v1}"
    MODEL="${4:-gpt-4o-mini}"
    jq --arg k "$KEY" --arg b "$BASE" --arg m "$MODEL" \
       '.env_variables += {KSP_LLM_BACKEND:"openai", OPENAI_API_KEY:$k, OPENAI_BASE_URL:$b, KSP_LLM_MODEL:$m}' \
       "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
    { echo "KSP_LLM_BACKEND=openai"; echo "OPENAI_API_KEY=$KEY"; echo "OPENAI_BASE_URL=$BASE"; echo "KSP_LLM_MODEL=$MODEL"; } > "$DEPLOY_ENV"
    echo "✓ OpenAI-compatible backend configured. Redeploying…"
    ;;
hf)
    require_jq
    TOK="${2:?hf token required}"
    MODEL="${3:-meta-llama/Llama-3.3-70B-Instruct}"
    jq --arg k "$TOK" --arg m "$MODEL" \
       '.env_variables += {KSP_LLM_BACKEND:"hf", HF_TOKEN:$k, KSP_LLM_MODEL:$m}' \
       "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
    { echo "KSP_LLM_BACKEND=hf"; echo "HF_TOKEN=$TOK"; echo "KSP_LLM_MODEL=$MODEL"; } > "$DEPLOY_ENV"
    echo "✓ HuggingFace backend configured. Redeploying…"
    ;;
ollama)
    require_jq
    HOST="${2:?ollama host required (e.g. http://your-host:11434)}"
    MODEL="${3:-llama3.2:1b}"
    jq --arg h "$HOST" --arg m "$MODEL" \
       '.env_variables += {KSP_LLM_BACKEND:"ollama", OLLAMA_HOST:$h, KSP_LLM_MODEL:$m}' \
       "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
    { echo "KSP_LLM_BACKEND=ollama"; echo "OLLAMA_HOST=$HOST"; echo "KSP_LLM_MODEL=$MODEL"; } > "$DEPLOY_ENV"
    echo "✓ Ollama backend configured. Redeploying…"
    ;;
*)
    echo "❌ unknown provider: $1"
    sed -n '2,15p' "$0"; exit 2 ;;
esac

command -v catalyst >/dev/null || { echo "❌ catalyst CLI missing. add ~/.npm-global/bin to PATH."; exit 2; }
./build-catalyst.sh
catalyst deploy --only appsail 2>&1 | tail -8

URL="https://ksp-cip-server-50044254710.development.catalystappsail.in"
echo
echo "▶ verifying online mode on $URL/assistant/health"
sleep 8
curl -s "$URL/assistant/health"
echo
echo "✓ Done. Open $URL and go to the Assistant tab."
