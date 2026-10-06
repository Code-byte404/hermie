#!/usr/bin/env bash
# Two tmux panes: the agent on the left, `hermie tail` on the right.
# To record a GIF: asciinema rec demo.cast -c ./record.sh, then agg demo.cast ../assets/demo.gif
set -euo pipefail
cd "$(dirname "$0")"
PORT=${HERMIE_PORT:-8787}
tmux new-session -d -s hermie-demo -x 160 -y 40
tmux send-keys -t hermie-demo "hermie serve --mode enforce --port $PORT" C-m
tmux split-window -h -t hermie-demo
tmux send-keys -t hermie-demo "sleep 2 && hermie tail" C-m
tmux select-pane -t hermie-demo:0.0
tmux split-window -v -t hermie-demo:0.0
tmux send-keys -t hermie-demo "ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT/anthropic claude" C-m
tmux attach -t hermie-demo
