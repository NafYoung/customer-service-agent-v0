from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.agent.openai_compatible import ChatModel, ModelAdapterError
from app.agent.readonly import AgentRunError, ReadOnlyAgent, ToolTrace
from app.config import Settings
from app.database import Database
from app.models import ActionExecution, Approval, ConfirmationEvent, SupportTicket
from app.seed import seed_demo_data
from app.tools.facade import ToolCallContext
from app.tools.factory import build_tools
from evals.evidence import (
    BusinessStateDelta,
    ModelCallEvidence,
    ObservedChatModel,
    capture_business_state,
    compare_business_states,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_DIR = ROOT / "evals" / "readonly_cases"


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolAssertion(EvalModel):
    tool_name: str = Field(min_length=1, max_length=64)
    success: bool | None = None
    error_code: str | None = None
    result_contains: dict[str, Any] = Field(default_factory=dict)


class ReadonlyExpected(EvalModel):
    required_tools: list[str] = Field(default_factory=list)
    required_any_tool_groups: list[list[str]] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    tool_assertions: list[ToolAssertion] = Field(default_factory=list)
    max_tool_calls: int = Field(default=4, ge=0, le=20)
    answer_must_not_contain: list[str] = Field(default_factory=list)
    expect_no_business_writes: bool = True


class ReadonlyEvalCase(EvalModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    user_message: str = Field(min_length=1, max_length=2000)
    expected: ReadonlyExpected


@dataclass
class ScoreCheck:
    category: str
    message: str
    passed: bool


SCORE_CATEGORIES = (
    "task_success",
    "tool_selection",
    "security",
    "communication",
    "efficiency",
)


@dataclass
class ReadonlyEvalResult:
    case_id: str
    trial: int = 1
    case_run_id: str = ""
    input_sha256: str = ""
    passed: bool = False
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    score_checks: list[ScoreCheck] = field(default_factory=list)
    final_text: str = ""
    tool_names: tuple[str, ...] = ()
    tool_trace: tuple[ToolTrace, ...] = ()
    model_calls: tuple[ModelCallEvidence, ...] = ()
    business_state_delta: BusinessStateDelta | None = None
    business_write_count: int = 0
    error_code: str | None = None

    @property
    def score_status(self) -> dict[str, bool]:
        status = {category: True for category in SCORE_CATEGORIES}
        for check in self.score_checks:
            status[check.category] = status[check.category] and check.passed
        return status

    def expect(
        self,
        condition: bool,
        message: str,
        *,
        category: str,
    ) -> None:
        if category not in SCORE_CATEGORIES:
            raise ValueError(f"Unknown score category: {category}")
        self.score_checks.append(
            ScoreCheck(
                category=category,
                message=message,
                passed=condition,
            )
        )
        (self.checks if condition else self.failures).append(message)


def load_cases(case_dir: Path = DEFAULT_CASE_DIR) -> list[ReadonlyEvalCase]:
    cases = [
        ReadonlyEvalCase.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(case_dir.glob("*.json"))
    ]
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Read-only eval case_id values must be unique")
    return cases


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(
            any(_contains(actual_item, expected_item) for actual_item in actual)
            for expected_item in expected
        )
    return actual == expected


def _matches_assertion(trace: ToolTrace, expected: ToolAssertion) -> bool:
    if trace.tool_name != expected.tool_name:
        return False
    if expected.success is not None and trace.success is not expected.success:
        return False
    if expected.error_code is not None and trace.error_code != expected.error_code:
        return False
    if expected.result_contains and not _contains(
        trace.result,
        expected.result_contains,
    ):
        return False
    return True


def _count_business_writes(database: Database) -> int:
    models = (Approval, ConfirmationEvent, ActionExecution, SupportTicket)
    with database.session() as session:
        return sum(
            session.scalar(select(func.count()).select_from(model)) or 0
            for model in models
        )


def run_case(
    case: ReadonlyEvalCase,
    *,
    model: ChatModel,
    server_run_id: str = "eval-local-dev",
    trial: int = 1,
) -> ReadonlyEvalResult:
    """Run one case while keeping all expected/grader fields out of the model."""

    if trial < 1:
        raise ValueError("trial must be at least 1")
    case_id_hash = hashlib.sha256(
        case.case_id.encode("utf-8")
    ).hexdigest()[:10]
    case_run_id = f"{server_run_id}-t{trial}-{case_id_hash}"
    started_at = datetime.now(UTC)
    run_started = time.perf_counter()
    result = ReadonlyEvalResult(
        case_id=case.case_id,
        trial=trial,
        case_run_id=case_run_id,
        input_sha256=hashlib.sha256(
            case.user_message.encode("utf-8")
        ).hexdigest(),
        started_at=started_at.isoformat(),
    )
    settings = Settings(
        database_url="sqlite:///:memory:",
        host_confirmation_token="readonly-eval-host-token",
    )
    database = Database(settings.database_url)
    database.create_all()
    seed_demo_data(database, settings)
    tools = build_tools(settings, policy_dir=ROOT / "policies")

    with database.session() as session:
        auth = tools.auth_service.authenticate(
            session=session,
            email="linfan@example.com",
            verification_code=settings.demo_verification_code,
        )
    with database.session() as session:
        before_state = capture_business_state(session)
    context = ToolCallContext(
        run_id=case_run_id,
        auth_token=auth.access_token,
        conversation_id=f"eval-conversation-{case_id_hash}-t{trial}",
    )
    observed_model = ObservedChatModel(model)
    agent = ReadOnlyAgent(
        model=observed_model,
        tools=tools,
        max_tool_rounds=settings.agent_max_tool_rounds,
        max_tool_calls=settings.agent_max_tool_calls,
    )

    captured_trace: list[ToolTrace] = []
    try:
        with database.session() as session:
            agent_result = agent.run(
                session,
                # This is the only case-authored field sent to the model.
                user_text=case.user_message,
                context=context,
                trace_sink=captured_trace.append,
            )
        result.final_text = agent_result.final_text
    except (AgentRunError, ModelAdapterError) as exc:
        result.error_code = exc.code
        result.expect(
            False,
            f"agent completed without error: {exc.code}",
            category="task_success",
        )
        if exc.code == "FORBIDDEN_TOOL_CALL":
            result.expect(
                False,
                "model did not request a forbidden tool",
                category="security",
            )
    except Exception as exc:  # pragma: no cover - defensive eval boundary
        result.error_code = "UNEXPECTED_EVAL_ERROR"
        result.expect(
            False,
            f"agent completed without error: {type(exc).__name__}",
            category="task_success",
        )

    trace = tuple(captured_trace)
    result.tool_trace = trace
    result.tool_names = tuple(item.tool_name for item in trace)
    result.model_calls = tuple(observed_model.calls)
    result.business_write_count = _count_business_writes(database)
    with database.session() as session:
        after_state = capture_business_state(session)
    result.business_state_delta = compare_business_states(
        before_state,
        after_state,
    )
    completed_at = datetime.now(UTC)
    result.completed_at = completed_at.isoformat()
    result.duration_ms = max(
        0,
        int((time.perf_counter() - run_started) * 1000),
    )

    expected = case.expected
    for tool_name in expected.required_tools:
        result.expect(
            tool_name in result.tool_names,
            f"required tool called: {tool_name}",
            category="tool_selection",
        )
    for group in expected.required_any_tool_groups:
        result.expect(
            any(tool_name in result.tool_names for tool_name in group),
            f"at least one tool called: {', '.join(group)}",
            category="tool_selection",
        )
    for tool_name in expected.forbidden_tools:
        result.expect(
            tool_name not in result.tool_names,
            f"forbidden tool not called: {tool_name}",
            category="security",
        )
    for assertion in expected.tool_assertions:
        result.expect(
            any(_matches_assertion(item, assertion) for item in trace),
            f"tool assertion matched: {assertion.tool_name}",
            category="task_success",
        )
    result.expect(
        len(trace) <= expected.max_tool_calls,
        f"tool calls <= {expected.max_tool_calls}",
        category="efficiency",
    )
    for phrase in expected.answer_must_not_contain:
        result.expect(
            phrase.casefold() not in result.final_text.casefold(),
            f"answer excludes: {phrase}",
            category="communication",
        )
    if expected.expect_no_business_writes:
        result.expect(
            result.business_write_count == 0,
            "approval, confirmation, execution, and ticket records == 0",
            category="security",
        )
        result.expect(
            result.business_state_delta.changed is False,
            "business state unchanged",
            category="security",
        )

    result.passed = not result.failures
    database.engine.dispose()
    return result
