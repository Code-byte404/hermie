"""Entry routing layer: all checks run locally in parallel, then a pure policy function picks the mode."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from .complexity import RouteLLMScorer
from .config import Settings
from .judge import Judge
from .policy import (COMPLEXITY_LEVELS, NEEDS_WORKSPACE_QUESTION, TASK_TYPES, Decision, Force, Route, Signals,
                     decide)
from .privacy import PrivacyGate, PrivacyVerdict

log = logging.getLogger(__name__)

# Bias toward doing: if even one sample out of several votes that something must be produced, go to plan mode
# (planner + local executor) instead of letting the cloud return text only
NEEDS_WORKSPACE_THRESHOLD = 0.3


@dataclass
class Routing:
    decision: Decision
    verdict: PrivacyVerdict
    signals: Optional[Signals]

    def signals_dict(self) -> dict:
        v = self.verdict
        d: dict = {"privacy": {"sensitive": v.sensitive, "reason": v.reason, "contextual_prob": v.contextual_prob,
                               "entities": sorted({f.entity for f in v.findings})}}
        if self.signals:
            sig = self.signals
            d.update({"task_type": {"choice": sig.task.choice, "confidence": sig.task.confidence},
                      "complexity": {"score": sig.complexity.score, "confidence": sig.complexity.confidence},
                      "routellm_win_rate": sig.win_rate, "needs_workspace": sig.needs_workspace,
                      "needs_workspace_prob": sig.needs_workspace_prob,
                      "task_probs": sig.task.probabilities, "complexity_probs": sig.complexity.probabilities})
        return d


class EntryRouter:
    def __init__(self, s: Settings, judge: Judge, gate: PrivacyGate, scorer: Optional[RouteLLMScorer]):
        self.s, self.judge, self.gate, self.scorer = s, judge, gate, scorer

    async def route(self, task: str, text: str, force: Force = Force.NONE) -> Routing:
        t = asyncio.to_thread
        jobs = [t(self.gate.check, text),
                t(self.judge.choice, text, "What kind of task is this request?", TASK_TYPES),
                t(self.judge.score, text, "How difficult is it to complete this request?", COMPLEXITY_LEVELS),
                t(self.judge.noul, text, NEEDS_WORKSPACE_QUESTION)]
        if self.scorer is not None:
            jobs.append(t(self.scorer.strong_win_rate, task))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        verdict = results[0]
        if isinstance(verdict, BaseException):  # fail closed
            verdict = PrivacyVerdict(True, reason=f"check_error: {verdict}")
        task_ans, cx_ans, needs_ws = results[1:4]
        win_rate = results[4] if len(results) > 4 and not isinstance(results[4], BaseException) else None
        if any(isinstance(x, BaseException) for x in (task_ans, cx_ans)):
            log.error("Judge model failed; handling locally: %s",
                      [x for x in (task_ans, cx_ans) if isinstance(x, BaseException)])
            return Routing(Decision(Route.LOCAL, ["judge model failed: falling back to local"]), verdict, None)
        if isinstance(needs_ws, BaseException):
            needs_ws = 1.0  # when unsure, assume local operations are needed: plan mode rather than handing the whole task to the cloud
        sig = Signals(verdict.sensitive, task_ans, cx_ans, win_rate, needs_ws > NEEDS_WORKSPACE_THRESHOLD, needs_ws)
        return Routing(decide(sig, self.s, force), verdict, sig)
