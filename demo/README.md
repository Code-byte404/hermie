# Hermie demo

A small signup service with one bug and a customer file full of fake personal data.
Everything here is made up: the phones are in the 555-01xx range, the cards are Stripe
test numbers and the keys in `.env` are not real.

Task to type into your coding agent:

    The phone validation in app.py rejects valid numbers like the ones in customers.csv; fix it and run the tests

Layout: two terminal panes.

- Left: the agent (Claude Code, Codex, Gemini CLI) with its base URL pointed at Hermie.
- Right: `hermie tail`, showing what was replaced before each request left the machine.

Run `./record.sh` to set this up in tmux. Needs `pip install flask pytest` for the tests.
