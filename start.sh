#!/bin/zsh
# Start Hermie: activate the conda env, make sure Ollama is running, check .env, then open the full-screen UI.
#
#   ./start.sh            # default mode (high-risk commands prompt for approval)
#   ./start.sh --auto     # auto mode (skip approvals, boundaries unchanged)
#   any other arguments are passed through to hermie, e.g. ./start.sh --workspace ~/proj
set -euo pipefail

# No cd: the current directory is the workspace (like Claude Code). The project's own files are located via PROJECT_DIR
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_NAME=hermie
OLLAMA_URL=${OLLAMA_URL:-http://localhost:11434}

# 1. conda env
if ! command -v conda >/dev/null 2>&1; then
    for c in /opt/miniconda3 /opt/anaconda3 "$HOME/miniconda3" "$HOME/anaconda3"; do
        [[ -f "$c/etc/profile.d/conda.sh" ]] && source "$c/etc/profile.d/conda.sh" && break
    done
fi
command -v conda >/dev/null 2>&1 || { echo "x conda not found"; exit 1; }
eval "$(conda shell.zsh hook)"
conda activate "$ENV_NAME" 2>/dev/null || {
    echo "x conda env $ENV_NAME does not exist. Run first:"
    echo "    conda env create -f environment.yml && conda activate $ENV_NAME && python -m spacy download zh_core_web_sm"
    exit 1
}

# 2. .env
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "! No .env, copying from .env.example. Fill in DEEPSEEK_API_KEY and run again."
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    exit 1
fi

# 3. Ollama
if ! curl -sf -m 3 "$OLLAMA_URL/api/tags" >/dev/null; then
    echo "- Ollama is not running, starting it..."
    if [[ -d "/Applications/Ollama.app" ]]; then
        open -a Ollama
    else
        nohup ollama serve >/tmp/ollama.log 2>&1 &
    fi
    for _ in {1..30}; do
        curl -sf -m 2 "$OLLAMA_URL/api/tags" >/dev/null && break
        sleep 1
    done
    curl -sf -m 2 "$OLLAMA_URL/api/tags" >/dev/null || { echo "x Ollama failed to start"; exit 1; }
fi

# 4. Are the local models pulled?
MODEL=$(grep -E '^WORKER_MODEL=' "$PROJECT_DIR/.env" | cut -d= -f2- | tr -d ' "' || true)
MODEL=${MODEL:-qwen3.8:27b-mlx}
JUDGE=$(grep -E '^JUDGE_MODEL=' "$PROJECT_DIR/.env" | cut -d= -f2- | tr -d ' "' || true)
JUDGE=${JUDGE:-$MODEL}
for M in "$MODEL" "$JUDGE"; do
    if ! curl -sf -m 3 "$OLLAMA_URL/api/tags" | grep -q "\"name\":\"$M\""; then
        echo "! Ollama does not have model $M, pulling..."
        ollama pull "$M"
    fi
done

echo "- env $ENV_NAME - Ollama ready - executor $MODEL - judge $JUDGE - workspace $PWD"
exec hermie "$@"
