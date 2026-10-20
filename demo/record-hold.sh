#!/bin/bash
# Stages the hold demo: Claude Code through `hermie claude` with the local judge on. The prompt carries
# non-public company plans; the rules replace the company name, the judge holds the rest, and Hermie's menu inside
# Claude Code asks before anything leaves. Choosing "Send this one" lets the turn go on.
#
# Usage: bash demo/record-hold.sh           (hermie, claude and tmux on PATH; claude logged in; JUDGE reachable)
#   JUDGE defaults to a fine-tuned Laya checkpoint; JUDGE=ollama:MODEL works too. It opens Claude Code in tmux
#   session `hermie-hold` with a throwaway data dir and prints the prompt to paste. To record:
#     asciinema rec --cols 120 --rows 44 --command "tmux attach -t hermie-hold" demo-hold.cast
#     agg --font-size 14 --theme monokai demo-hold.cast assets/demo-hold.gif
#   Clean up: quit Claude Code (/exit; the proxy stops with the last window), then tmux kill-session -t hermie-hold
set -u
PORT="${PORT:-8791}"
JUDGE="${JUDGE:-laya:$HOME/.hermie/models/laya-judge-v2}"
DATA="${DATA:-$(mktemp -d /tmp/hermie-hold-demo.XXXX)}"
mkdir -p "$DATA"
PROJ="$(mktemp -d /tmp/hermie-hold-proj.XXXX)"
PROMPT="Write three short talking points for Monday's team meeting. Confidential: Northwind Holdings will cut 80 jobs in March, before the earnings call; staff do not know yet."

cd "$PROJ" && git init -q && echo "# demo" > README.md

tmux kill-session -t hermie-hold 2>/dev/null
tmux new-session -d -s hermie-hold -x 120 -y 44 -c "$PROJ" -e "PATH=$PATH" -e "HERMIE_JUDGE=$JUDGE" -e "HERMIE_PORT=$PORT"
tmux set-option -t hermie-hold status off     # the recording attaches to this session; no tmux bar in it
tmux send-keys -t hermie-hold "clear; unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT; for v in \$(env | grep -o '^CLAUDE_CODE_[A-Z_]*'); do unset \$v; done; hermie claude --data-dir '$DATA' --setting-sources project,local --strict-mcp-config" C-m

echo "data dir: $DATA   project: $PROJ"
echo "Claude Code is starting in tmux session hermie-hold (accept the folder trust prompt once). Paste this prompt:"
echo
echo "  $PROMPT"
echo
echo "Hermie's menu appears above the prompt; press 2 (Send this one). Afterwards: hermie tail --once --data-dir $DATA"
