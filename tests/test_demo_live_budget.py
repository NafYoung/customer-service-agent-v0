"""Live demo per-conversation soft budget gate and model routing reservation."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from app.agent.openai_compatible import AssistantTurn
from app.agent.preparation import PreparationAgentRunResult
from app.config import Settings
from app.demo import DEMO_AGENT_MODE_PREPARATION_LIVE
from app.demo.live_runner import run_preparation_live
from app.demo.session import DemoSessionManager
from app.models import SupportTicket

ORIGIN = "http://testserver"
HOST_TOKEN = "demo-live-budget-host-token"


def _live_settings(**overrides: object) -> Settings:
    return Settings(
        app_mode="local",
        demo_agent_mode=DEMO_AGENT_MODE_PREPARATION_LIVE,
        deepseek_api_key="sk-local-test-key",
        demo_allowed_origin=ORIGIN,
        demo_cookie_secure=False,
        host_confirmation_token=HOST_TOKEN,
        **overrides,
    )


class _FakeModel:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _turn(content: str = "ok") -> AssistantTurn:
    return AssistantTurn(
        content=content,
        tool_calls=(),
        finish_reason="stop",
        usage=None,
    )


class _EmptyAgent:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *args, **kwargs):
        self.calls += 1
        return PreparationAgentRunResult(
            final_text="已处理，当前没有待确认操作。",
            tool_trace=(),
            model_turns=(_turn(), _turn()),
            prepared_action=None,
        )


def _tickets(demo) -> list[SupportTicket]:
    with demo.database.session() as db:
        return list(db.scalars(select(SupportTicket)).all())


def test_conversation_attempt_soft_gate_stops_calls_and_creates_ticket():
    settings = _live_settings(demo_max_live_attempts_per_session=1)
    manager = DemoSessionManager(settings)
    _, demo = manager.create()
    agent = _EmptyAgent()
    try:
        with (
            patch(
                "app.demo.live_runner._live_budget_guard",
                return_value=None,
            ),
            patch(
                "app.demo.live_runner.build_deepseek_client",
                return_value=_FakeModel(),
            ),
            patch(
                "app.demo.live_runner.build_preparation_agent",
                return_value=agent,
            ),
        ):
            reply, has_pending, delta = run_preparation_live(
                demo,
                message="查一下我的订单",
            )
            assert reply and not has_pending and delta == 2
            assert agent.calls == 1

            reply2, has_pending2, delta2 = run_preparation_live(
                demo,
                message="查一下我的订单",
            )
            assert not has_pending2 and delta2 == 0
            assert agent.calls == 1  # soft gate stops before the agent run
            assert "TKT-" in reply2
            tickets = _tickets(demo)
            assert len(tickets) == 1
            assert tickets[0].category == "CONVERSATION_BUDGET"
            assert demo.handoff_reasons == {"conversation_budget"}
    finally:
        manager.dispose_all()


def test_model_routing_reserves_action_and_query_models():
    settings = _live_settings(
        demo_live_query_model="deepseek-v4-flash",
        demo_live_action_model="deepseek-v4-pro",
    )
    manager = DemoSessionManager(settings)
    _, demo = manager.create()
    captured: dict[str, object] = {}

    def _client(settings, **kwargs):
        captured["model"] = kwargs.get("model")
        return _FakeModel()

    try:
        with (
            patch(
                "app.demo.live_runner._live_budget_guard",
                return_value=None,
            ),
            patch(
                "app.demo.live_runner.build_deepseek_client",
                side_effect=_client,
            ),
            patch(
                "app.demo.live_runner.build_preparation_agent",
                return_value=_EmptyAgent(),
            ),
        ):
            run_preparation_live(demo, message="取消订单 ORD-1001")
            assert captured["model"] == "deepseek-v4-pro"
            run_preparation_live(demo, message="查一下我的订单")
            assert captured["model"] == "deepseek-v4-flash"
    finally:
        manager.dispose_all()


def test_routing_defaults_to_canonical_model():
    settings = _live_settings()
    manager = DemoSessionManager(settings)
    _, demo = manager.create()
    captured: dict[str, object] = {}

    def _client(settings, **kwargs):
        captured["model"] = kwargs.get("model")
        return _FakeModel()

    try:
        with (
            patch(
                "app.demo.live_runner._live_budget_guard",
                return_value=None,
            ),
            patch(
                "app.demo.live_runner.build_deepseek_client",
                side_effect=_client,
            ),
            patch(
                "app.demo.live_runner.build_preparation_agent",
                return_value=_EmptyAgent(),
            ),
        ):
            run_preparation_live(demo, message="取消订单 ORD-1001")
            assert captured["model"] == "deepseek-v4-flash"
    finally:
        manager.dispose_all()
