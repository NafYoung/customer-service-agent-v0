from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.openai_compatible import ChatModel, ToolCall
from app.agent.readonly import (
    AgentRunError,
    AgentRunResult,
    ReadOnlyAgent,
    ToolTrace,
    _ReadOnlyDispatcher,
)
from app.enums import IssueType, ItemCondition
from app.schemas import PrepareActionResponse
from app.tools.contracts import (
    PREPARATION_TOOL_NAMES,
    PREPARE_TOOL_NAMES,
    PrepareCancelInput,
    PrepareExchangeInput,
    PrepareReturnInput,
    get_preparation_tool_contracts,
)
from app.tools.facade import CustomerServiceTools, ToolCallContext

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SYSTEM_PROMPT = (
    _PROJECT_ROOT / "app" / "agent" / "preparation_system_prompt.md"
)


@dataclass(frozen=True)
class PreparationAgentRunResult(AgentRunResult):
    prepared_action: PrepareActionResponse | None = None


class _PreparationDispatcher(_ReadOnlyDispatcher):
    _input_models = {
        **_ReadOnlyDispatcher._input_models,
        "prepare_cancel_order": PrepareCancelInput,
        "prepare_return": PrepareReturnInput,
        "prepare_exchange": PrepareExchangeInput,
    }

    def execute(
        self,
        session: Session,
        *,
        tool_name: str,
        parsed: BaseModel,
        context: ToolCallContext,
    ) -> Any:
        if tool_name == "prepare_cancel_order":
            if not isinstance(parsed, PrepareCancelInput):
                raise AgentRunError(
                    "TOOL_VALIDATION_INVARIANT",
                    "Validated cancellation input had an unexpected type.",
                )
            return self._tools.prepare_cancel_order(
                session,
                order_id=parsed.order_id,
                user_note=parsed.user_note,
                context=context,
            )
        if tool_name == "prepare_return":
            if not isinstance(parsed, PrepareReturnInput):
                raise AgentRunError(
                    "TOOL_VALIDATION_INVARIANT",
                    "Validated return input had an unexpected type.",
                )
            return self._tools.prepare_return(
                session,
                order_id=parsed.order_id,
                order_item_id=parsed.order_item_id,
                declared_condition=ItemCondition(parsed.declared_condition),
                issue_type=IssueType(parsed.issue_type),
                user_note=parsed.user_note,
                context=context,
            )
        if tool_name == "prepare_exchange":
            if not isinstance(parsed, PrepareExchangeInput):
                raise AgentRunError(
                    "TOOL_VALIDATION_INVARIANT",
                    "Validated exchange input had an unexpected type.",
                )
            return self._tools.prepare_exchange(
                session,
                order_id=parsed.order_id,
                order_item_id=parsed.order_item_id,
                target_size=parsed.target_size,
                declared_condition=ItemCondition(parsed.declared_condition),
                issue_type=IssueType(parsed.issue_type),
                user_note=parsed.user_note,
                context=context,
            )
        return super().execute(
            session,
            tool_name=tool_name,
            parsed=parsed,
            context=context,
        )


class PreparationAgent(ReadOnlyAgent):
    """Bounded Agent that may query, check eligibility, and prepare once."""

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: CustomerServiceTools,
        max_tool_rounds: int = 4,
        max_tool_calls: int = 12,
        system_prompt: str | None = None,
    ):
        super().__init__(
            model=model,
            tools=tools,
            max_tool_rounds=max_tool_rounds,
            max_tool_calls=max_tool_calls,
            system_prompt=(
                system_prompt
                if system_prompt is not None
                else _DEFAULT_SYSTEM_PROMPT.read_text(encoding="utf-8")
            ),
        )
        self._contracts = get_preparation_tool_contracts()
        self._dispatcher = _PreparationDispatcher(tools)
        self._allowed_tool_names = PREPARATION_TOOL_NAMES

    def _validate_tool_batch(
        self,
        calls: tuple[ToolCall, ...],
        trace: tuple[ToolTrace, ...],
    ) -> None:
        already_prepared = any(
            item.success and item.tool_name in PREPARE_TOOL_NAMES
            for item in trace
        )
        if already_prepared:
            raise AgentRunError(
                "TOOL_CALL_AFTER_PREPARE",
                "The model requested another tool after preparing an action.",
            )
        prepare_calls = [
            call for call in calls if call.name in PREPARE_TOOL_NAMES
        ]
        if prepare_calls and len(calls) != 1:
            raise AgentRunError(
                "PREPARE_BATCH_MUST_BE_SINGLE_CALL",
                "A prepare tool must be the only call in its batch.",
            )

    def _context_for_tool_call(
        self,
        context: ToolCallContext,
        call: ToolCall,
    ) -> ToolCallContext:
        return replace(
            context,
            atomic_run=True,
            origin_tool_call_id=(
                call.id if call.name in PREPARE_TOOL_NAMES else None
            ),
        )

    def run(
        self,
        session: Session,
        *,
        user_text: str,
        context: ToolCallContext,
        trace_sink: Callable[[ToolTrace], None] | None = None,
    ) -> PreparationAgentRunResult:
        with session.begin_nested():
            result = super().run(
                session,
                user_text=user_text,
                context=context,
                trace_sink=trace_sink,
            )
        successful_prepares = [
            item
            for item in result.tool_trace
            if item.success and item.tool_name in PREPARE_TOOL_NAMES
        ]
        prepared_action = None
        if successful_prepares:
            prepared_action = PrepareActionResponse.model_validate(
                successful_prepares[0].result
            )
        return PreparationAgentRunResult(
            final_text=result.final_text,
            tool_trace=result.tool_trace,
            model_turns=result.model_turns,
            prepared_action=prepared_action,
        )
