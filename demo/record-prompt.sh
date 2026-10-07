#!/bin/bash
# Records the simplest demo: a Claude Code prompt that contains a customer's name, phone, email and a key.
# Claude answers as usual; `hermie show` then prints what Anthropic actually received.
# Usage: DATA=/tmp/hermie-demo-data bash demo/record-prompt.sh   (hermie and claude must be on PATH)
#   asciinema rec --idle-time-limit 2 --cols 100 --rows 28 --command "bash demo/record-prompt.sh" demo.cast
#   agg --font-size 14 --theme monokai demo.cast assets/demo.gif
set -u
DATA="${DATA:-/tmp/hermie-demo-data}"
WS="$(mktemp -d)"; cd "$WS"
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
for v in $(env | grep -o '^CLAUDE_CODE_[A-Z_]*'); do unset "$v"; done
type_cmd() { printf '\033[1;32m$\033[0m '; for ((i=0;i<${#1};i++)); do printf '%s' "${1:$i:1}"; sleep 0.018; done; printf '\n'; sleep 0.5; }
say() { printf '\n\033[1;36m# %s\033[0m\n' "$1"; sleep 1.2; }

type_cmd "hermie serve &"
hermie serve --data-dir "$DATA" > "$WS/serve.log" 2>&1 &
sleep 2.5
head -3 "$WS/serve.log"
type_cmd "export ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic"
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic
sleep 0.5

say "A normal Claude Code prompt, with a customer's details in it:"
PROMPT='Our customer Maria Gonzalez (555-010-0199, maria.gonzalez@example.com) says her Stripe key sk-test-hermie-demo-not-a-real-key-0000 stopped working. What should I check first? Two sentences.'
type_cmd "claude -p \"$PROMPT\""
claude -p "$PROMPT" --permission-mode bypassPermissions --output-format text 2>/dev/null | fold -s -w 96
sleep 2.5

say "What actually left the machine (the user message in the stored request body):"
ID=$(python3 -c "import json,sys; L=[json.loads(l) for l in open('$DATA/receipt.jsonl')]; L=[l for l in L if l.get('client')=='claude-code']; print(L[0]['id'])")
type_cmd "hermie show $ID | grep -o '\"Our customer[^\"]*'"
hermie show "$ID" --data-dir "$DATA" | grep -o '"Our customer[^"]*' | sed 's/^"//' | fold -s -w 96 | sed -E 's/<([A-Z_]+_[0-9]+)>/\x1b[1;33m<\1>\x1b[0m/g'
sleep 3.5

say "The receipt line for that request (no values, only what was replaced):"
type_cmd "hermie tail --once | grep SECRET"
hermie tail --once --data-dir "$DATA" | grep SECRET
sleep 5
kill %1 2>/dev/null
