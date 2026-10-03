"""Entry routing layer: all checks run locally in parallel, then a pure policy function picks the mode."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from .complexity import RouteLLMScorer
from .config import Settings
from .judge import YES_NO, FormAnswers, Judge, as_score, form_via_primitives, score_options
from .policy import (BUSINESS_DATA_QUESTION, COMPLEXITY_LEVELS, NEEDS_WORKSPACE_QUESTION, TASK_TYPES, Decision,
                     Force, Route, Signals, decide)
from .privacy import PrivacyGate, PrivacyVerdict

log = logging.getLogger(__name__)

# Bias toward doing: if even one sample out of several votes that something must be produced, go to plan mode
# (planner + local executor) instead of letting the cloud return text only
NEEDS_WORKSPACE_THRESHOLD = 0.3  # default of Settings.needs_workspace_threshold; kept for evals
BUSINESS_DATA_THRESHOLD = 0.3  # any yes-vote: local is the safe side

TASK_QUESTION = "What kind of task is this request?"
COMPLEXITY_QUESTION = "How difficult is it to complete this request?"

# The routing questions, asked in one judge request per sample (JUDGE_BATCH). The privacy gate's contextual question
# deliberately stays a request of its own: folded into this form the judge missed named-person cases it catches alone
# (evals: contextual recall 0.97 -> 0.91), while the routing answers got better (36/40 -> 40/40) and faster.
ROUTING_QUESTIONS = {
    "task": (TASK_QUESTION, TASK_TYPES),
    "complexity": (COMPLEXITY_QUESTION, score_options(COMPLEXITY_LEVELS)),
    "needs_workspace": (NEEDS_WORKSPACE_QUESTION, YES_NO),
}


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
                      "business": sig.business, "business_prob": sig.business_prob,
                      "task_probs": sig.task.probabilities, "complexity_probs": sig.complexity.probabilities})
        return d


class EntryRouter:
    def __init__(self, s: Settings, judge: Judge, gate: PrivacyGate, scorer: Optional[RouteLLMScorer]):
        self.s, self.judge, self.gate, self.scorer = s, judge, gate, scorer
        self.ask_business = False   # set by Hermie when a connector is ready

    async def route(self, task: str, text: str, force: Force = Force.NONE, business: bool = False) -> Routing:
        if self.s.judge_batch:
            return await self._route_batched(task, text, force, business)
        return await self._route_per_question(task, text, force, business)

    def _form(self, text: str, questions: dict = ROUTING_QUESTIONS) -> FormAnswers:
        form = getattr(self.judge, "form", None)
        if callable(form):
            return form(text, questions)
        return form_via_primitives(self.judge, text, questions)

    async def _route_batched(self, task: str, text: str, force: Force, business: bool = False) -> Routing:
        """Privacy check (rules + contextual question), one judge form with the routing questions, RouteLLM: in parallel."""
        t = asyncio.to_thread
        questions = dict(ROUTING_QUESTIONS)
        if self.ask_business:
            questions["business_data"] = (BUSINESS_DATA_QUESTION, YES_NO)
        jobs = [t(self.gate.check, text), t(self._form, text, questions)]
        if self.scorer is not None:
            jobs.append(t(self.scorer.strong_win_rate, task))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        verdict, answers = results[0], results[1]
        win_rate = results[2] if len(results) > 2 and not isinstance(results[2], BaseException) else None
        if isinstance(verdict, BaseException):  # fail closed
            verdict = PrivacyVerdict(True, reason=f"check_error: {verdict}")
        if isinstance(answers, BaseException):
            log.error("Judge model failed; handling locally: %r", answers)
            return Routing(Decision(Route.LOCAL, ["judge model failed: falling back to local"]), verdict, None)
        needs_ws = answers["needs_workspace"].probabilities["yes"]
        biz = answers["business_data"].probabilities["yes"] if self.ask_business else None
        sig = Signals(verdict.sensitive, answers["task"], as_score(answers["complexity"]), win_rate,
                      needs_ws > self.s.needs_workspace_threshold, needs_ws,
                      business=business or (biz is not None and biz > BUSINESS_DATA_THRESHOLD), business_prob=biz)
        return Routing(decide(sig, self.s, force), verdict, sig)

    async def _route_per_question(self, task: str, text: str, force: Force, business: bool = False) -> Routing:
        """One judge request per question (JUDGE_BATCH=false)."""
        t = asyncio.to_thread
        jobs = [t(self.gate.check, text),
                t(self.judge.choice, text, TASK_QUESTION, TASK_TYPES),
                t(self.judge.score, text, COMPLEXITY_QUESTION, COMPLEXITY_LEVELS),
                t(self.judge.noul, text, NEEDS_WORKSPACE_QUESTION),
                t(self.judge.noul, text, BUSINESS_DATA_QUESTION) if self.ask_business else asyncio.sleep(0, None)]
        if self.scorer is not None:
            jobs.append(t(self.scorer.strong_win_rate, task))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        verdict = results[0]
        if isinstance(verdict, BaseException):  # fail closed
            verdict = PrivacyVerdict(True, reason=f"check_error: {verdict}")
        task_ans, cx_ans, needs_ws, biz = results[1:5]
        win_rate = results[5] if len(results) > 5 and not isinstance(results[5], BaseException) else None
        if any(isinstance(x, BaseException) for x in (task_ans, cx_ans)):
            log.error("Judge model failed; handling locally: %s",
                      [x for x in (task_ans, cx_ans) if isinstance(x, BaseException)])
            return Routing(Decision(Route.LOCAL, ["judge model failed: falling back to local"]), verdict, None)
        if isinstance(needs_ws, BaseException):
            needs_ws = 1.0  # when unsure, assume local operations are needed: plan mode rather than handing the whole task to the cloud
        if isinstance(biz, BaseException):
            biz = 1.0  # a failed business question counts as yes: local is the safe side
        sig = Signals(verdict.sensitive, task_ans, cx_ans, win_rate, needs_ws > self.s.needs_workspace_threshold,
                      needs_ws, business=business or (biz is not None and biz > BUSINESS_DATA_THRESHOLD),
                      business_prob=biz)
        return Routing(decide(sig, self.s, force), verdict, sig)
