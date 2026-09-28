"""End-to-end example: run a few typical scenarios against real Ollama (and DeepSeek, if .env has a key).

    python examples/run_scenarios.py                 # run all scenarios
    python examples/run_scenarios.py 1 4             # only scenarios 1 and 4
    python examples/run_scenarios.py --list          # list scenarios

Every scenario runs in auto mode inside the demo workspace (~/HermieWork/demo, created automatically);
afterwards it prints the route, execution backend, outbound count and output files. Outbound content can be
checked in ~/.hermie/outbound.jsonl.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from demo_workspace import create

from hermie.config import RunMode, Settings
from hermie.core import Hermie
from hermie.events import ChatMessage, CommandFinished, CommandStarted, Notice, OutboundSent, RouteDecided

SCENARIOS = [
    ("Repetitive work -> local only",
     "Translate each English title in data/titles.txt into Chinese, line by line, and write them to out/titles_zh.txt, one per line"),
    ("Data processing with private data -> local only",
     "Count the customers and average assets per city in data/clients.csv and write the result to out/by_city.csv"),
    ("Code fix -> local (may escalate after self-check)",
     "The average function in project/calc.py has a bug; fix it, then run python -m pytest -q in the project directory to confirm the tests pass"),
    ("Overall planning with no private data -> cloud direct",
     "Draft a three-month release plan for an open-source command-line tool, listing goals and milestones per phase"),
    ("Private data + needs planning -> plan mode (placeholder redaction)",
     "Customer Zhang Wei (mobile 13812345678, age 58, assets about 3.2 million) wants retirement financial planning. Plan a financial plan and write it to out/plan_zhangwei.md"),
    ("Contextually sensitive + planning -> plan mode (local abstract rewrite)",
     "Based on notes/meeting.md, plan a communication plan for all staff and write it to out/comms.md"),
]


T0 = [time.time()]


def printer(ev) -> None:
    ts = f"{time.time() - T0[0]:6.1f}s"
    if isinstance(ev, RouteDecided):
        print(f"  {ts} [route] {ev.route}: {'; '.join(ev.reasons)}")
    elif isinstance(ev, CommandStarted):
        print(f"  {ts} [exec] {ev.tool} {ev.summary[:100]}" + (f" (risk {ev.risk})" if ev.risk else ""))
    elif isinstance(ev, CommandFinished) and ev.exit_code != 0:
        print(f"         exit={ev.exit_code}")
    elif isinstance(ev, OutboundSent):
        print(f"  {ts} [outbound -> {ev.model}] {ev.content[:120]!r}")
    elif isinstance(ev, Notice):
        print(f"  {ts} [{ev.level}] {ev.text}")
    elif isinstance(ev, ChatMessage) and not ev.streaming and ev.role == "planner":
        print(f"  {ts} [planner] {ev.text[:200]!r}")


async def run(indices: list[int]) -> None:
    s = Settings()
    s.workspace = create(Path("~/HermieWork/demo").expanduser())
    s.mode = RunMode.AUTO  # unattended demo: skip approvals, but sandbox, gate, snapshots and audit stay on
    agent = Hermie(s)
    agent.bus.subscribe(printer)
    print(f"Workspace: {s.workspace}\nExecutor/judge model: {s.worker_model} / {s.judge_model}\n"
          f"DeepSeek: {'configured' if s.deepseek_api_key else 'not configured (cloud routes fall back to local)'}\n")
    print("Loading privacy detection models...")
    agent.warm_up()
    for i in indices:
        title, task = SCENARIOS[i - 1]
        print(f"\n=== Scenario {i}: {title} ===\nTask: {task}")
        t0 = T0[0] = time.time()
        try:
            r = await agent.run(task)
        except Exception as e:
            print(f"  x Failed: {type(e).__name__}: {e}")
            continue
        print(f"  -> route {r.route} - backend {r.backend} - outbound {r.outbound_count} - tainted {r.tainted} - "
              f"{time.time() - t0:.0f}s")
        print("  -> Output: " + r.output[:400].replace("\n", "\n             "))
    print(f"\nOutbound record: {s.outbound_log_path}\nAudit log: {s.audit_log_path}\nOutput dir: {s.workspace / 'out'}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--list" in args:
        for i, (title, task) in enumerate(SCENARIOS, 1):
            print(f"{i}. {title}\n   {task}")
        sys.exit(0)
    picked = [int(a) for a in args if a.isdigit()] or list(range(1, len(SCENARIOS) + 1))
    asyncio.run(run(picked))
