#!/bin/bash
# Stages the keys demo: Claude Code (interactive, inside tmux) with Hermie's hooks and status line, a fresh
# project, and a prompt that pastes a fake key and asks for a .env file. The recording shows Claude writing the
# file, Hermie's two lines under the tool call and at the end of the turn, and the status line at the bottom.
#
# Usage: bash demo/record-env.sh            (hermie, claude and tmux on PATH; claude logged in)
#   It starts `hermie serve` on $PORT with a throwaway data dir, opens Claude Code in tmux session `hermie-env`
#   and prints the prompt to paste. To record:
#     asciinema rec --cols 120 --rows 36 --command "tmux attach -t hermie-env" demo-env.cast
#     agg --font-size 14 --theme monokai demo-env.cast assets/demo-env.gif
#   Clean up with: tmux kill-session -t hermie-env; kill $(cat "$DATA/serve.pid")
set -u
PORT="${PORT:-8790}"
DATA="${DATA:-$(mktemp -d /tmp/hermie-env-demo.XXXX)}"
mkdir -p "$DATA"
PROJ="$(mktemp -d /tmp/hermie-env-proj.XXXX)"
KEY="sk-test-hermie-demo-not-a-real-key-0123456789abcdef"
PROMPT="Create a .env file in this project with exactly one line: OPENAI_API_KEY=$KEY . Do not explain, just write the file."

cd "$PROJ" && git init -q && echo "# demo" > README.md
hermie install-hooks --data-dir "$DATA" > /dev/null
hermie serve --port "$PORT" --data-dir "$DATA" > "$DATA/serve.log" 2>&1 &
echo $! > "$DATA/serve.pid"
sleep 4

tmux kill-session -t hermie-env 2>/dev/null
tmux new-session -d -s hermie-env -x 120 -y 36 -c "$PROJ"
tmux set-option -t hermie-env status off     # the recording attaches to this session; no tmux bar in it
tmux send-keys -t hermie-env "clear; unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT; for v in \$(env | grep -o '^CLAUDE_CODE_[A-Z_]*'); do unset \$v; done; export ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT/anthropic; claude --permission-mode acceptEdits" C-m

echo "data dir: $DATA   project: $PROJ"
echo "Claude Code is starting in tmux session hermie-env (accept the folder trust prompt once). Paste this prompt:"
echo
echo "  $PROMPT"
echo
echo "Afterwards: hermie show ID --data-dir $DATA  (the stored requests hold OPENAI_API_KEY=<SECRET_1>);  cat $PROJ/.env"
