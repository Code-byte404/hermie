"""Trajectories: what happened in a task, node by node, for the self-improvement loop to learn from.

One JSON line per task in data_dir/trajectories.jsonl. The record is data-free by construction: the input's
SHA-256 (as in audit.jsonl), the workspace's SHA-256, route, backend, timings, routing signals (probabilities and
choices), counters and the framework's own reason strings. Never task text, tool output, diffs, answers or review
problem text: node bodies pass counts and flags through TaskState.trace_note(), and the tests assert that a phone
number in the task, the answer and a review problem never reaches the file.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .audit import sha256

if TYPE_CHECKING:
    from .core import TaskResult
    from .session import TaskState


def task_record(st: "TaskState", r: "TaskResult", latency_s: float, interrupted: bool) -> dict:
    flow = st.flow
    routing = flow.routing
    return {
        "input_sha256": sha256(st.text),
        "workspace_sha256": sha256(str(st.s.workspace)),
        "route": r.route,
        "backend": r.backend,
        "force": flow.force.value,
        "interrupted": interrupted,
        "latency_s": round(latency_s, 3),
        "sensitive": st.sensitive_input,
        "tainted": st.tainted,
        "business": st.business,
        "outbound_count": st.outbound_count,
        "delegations": st.delegations,
        "review_failures": st.review_failures,
        "review_fixed": st.review_fixed,
        "escalated": flow.escalated,
        "fallback": flow.fallback,
        "reasons": list(r.reasons),
        "signals": routing.signals_dict() if routing is not None else {},
        "nodes": list(st.trace),
    }
