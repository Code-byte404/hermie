"""Plan mode design phase: questions, a structured plan, approval, PLAN.md, and the privacy of answers."""
import asyncio

import pytest

from hermie.events import ClarifyAnswer, ClarifyRequest, EventBus, PlanDecision, PlanReviewRequest, QuestionView


async def test_bus_without_providers_never_blocks():
    bus = EventBus()
    assert await bus.request_clarification(ClarifyRequest(1, [QuestionView("q", ["a", "b"])])) is None
    d = await bus.request_plan_review(PlanReviewRequest({}, "", 1, {}))
    assert d.action == "approve"


async def test_bus_wait_time_is_counted_as_user_wait():
    bus = EventBus()

    async def slow(req):
        await asyncio.sleep(0.05)
        return [ClarifyAnswer(option=0)]
    bus.clarifier = slow
    assert (await bus.request_clarification(ClarifyRequest(1, [QuestionView("q", ["a", "b"])])))[0].option == 0
    assert bus.user_wait_s() >= 0.04
