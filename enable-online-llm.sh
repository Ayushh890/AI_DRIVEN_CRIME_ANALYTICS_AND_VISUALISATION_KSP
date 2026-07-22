#!/usr/bin/env bash
# Turn on the online LLM backend for the KSP CIP Assistant and redeploy.
#
# Secrets never touch app-config.json (which is committed to git). Instead this
# script writes provider config to .env.deploy (gitignored), and
# build-catalyst.sh merges those key/value pairs into app-config.json's
# env_variables JUST for the duration of `catalyst deploy` before reverting
# the file.
#
# Usage:
#   ./enable-online-llm.sh groq  gsk_YOUR_KEY                          # Groq (default, free tier)
#   ./enable-online-llm.sh openai sk_...  https://api.openai.com/v1  gpt-4o-mini
#   ./enable-online-llm.sh hf     hf_YOUR_TOKEN     meta-llama/Llama-3.3-70B-Instruct
#   ./enable-online-llm.sh ollama http://your-host:11434  llama3.2:1b
#   ./enable-online-llm.sh --show           # print current .env.deploy
#   ./enable-online-llm.sh --clear          # revert to offline (deletes .env.deploy, redeploys)

set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

DEPLOY_ENV=.env.deploy

case "${1:-}" in
"") sed -n '2,15p' "$0"; exit 0 ;;
--show)
    if [[ -f "$DEPLOY_ENV" ]]; then
        # Mask secrets when echoing to stdout.
        awk -F= '{
          if ($1 ~ /(KEY|TOKEN)/) print $1 "=" substr($2,1,4) "…" substr($2, length($2)-2)
          else print $0
        }' "$DEPLOY_ENV"
    else
        echo "(no online-LLM config — running in offline mode)"
    fi
    exit 0 ;;
--clear)
    rm -f "$DEPLOY_ENV"
    echo "✓ .env.deploy removed. Redeploying in OFFLINE mode…"
    ;;
groq)
    KEY="${2:?groq api key required — get one at https://console.groq.com/keys}"
    MODEL="${3:-llama-3.3-70b-versatile}"
    umask 077   # 0600 permissions so key isn't world-readable
    { echo "KSP_LLM_BACKEND=groq"
      echo "GROQ_API_KEY=$KEY"
      echo "KSP_LLM_MODEL=$MODEL"; } > "$DEPLOY_ENV"
    echo "✓ Groq configured (stored in $DEPLOY_ENV, gitignored). Redeploying…" ;;
openai)
    KEY="${2:?openai api key required}"
    BASE="${3:-https://api.openai.com/v1}"
    MODEL="${4:-gpt-4o-mini}"
    umask 077
    { echo "KSP_LLM_BACKEND=openai"
      echo "OPENAI_API_KEY=$KEY"
      echo "OPENAI_BASE_URL=$BASE"
      echo "KSP_LLM_MODEL=$MODEL"; } > "$DEPLOY_ENV"
    echo "✓ OpenAI-compatible configured. Redeploying…" ;;
hf)
    TOK="${2:?huggingface token required}"
    MODEL="${3:-meta-llama/Llama-3.3-70B-Instruct}"
    umask 077
    { echo "KSP_LLM_BACKEND=hf"
      echo "HF_TOKEN=$TOK"
      echo "KSP_LLM_MODEL=$MODEL"; } > "$DEPLOY_ENV"
    echo "✓ HuggingFace configured. Redeploying…" ;;
ollama)
    HOST="${2:?ollama host required (e.g. http://your-host:11434)}"
    MODEL="${3:-llama3.2:1b}"
    umask 077
    { echo "KSP_LLM_BACKEND=ollama"
      echo "OLLAMA_HOST=$HOST"
      echo "KSP_LLM_MODEL=$MODEL"; } > "$DEPLOY_ENV"
    echo "✓ Ollama configured. Redeploying…" ;;
*)
    echo "❌ unknown provider: $1"; sed -n '2,15p' "$0"; exit 2 ;;
esac

command -v catalyst >/dev/null || { echo "❌ catalyst CLI missing. add ~/.npm-global/bin to PATH."; exit 2; }
./build-catalyst.sh --deploy

URL="https://ksp-cip-server-50044254710.development.catalystappsail.in"
echo
echo "▶ verifying $URL/assistant/health"
sleep 6
curl -s "$URL/assistant/health"; echo
echo "✓ Done. Open $URL → Assistant tab."
