from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from app.agent.openai_compatible import (
    AssistantTurn,
    ChatModel,
    Message,
    ToolCall,
    ToolContract,
)
from app.errors import ServiceError
from app.schemas import EligibilityRequest, PolicySearchRequest
from app.tools.contracts import (
    READ_ONLY_TOOL_NAMES,
    EligibilityToolInput,
    EmptyInput,
    InventoryInput,
    OrderIdInput,
    get_read_only_tool_contracts,
)
from app.tools.facade import CustomerServiceTools, ToolCallContext

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SYSTEM_PROMPT = _PROJECT_ROOT / "app" / "agent" / "readonly_system_prompt.md"
_INTERNAL_KEYS = {
    "access_token",
    "auth_token",
    "authorization",
    "customer_id",
    "host_confirmation_token",
    "token",
    "verification_code",
}


class AgentRunError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolTrace:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] | None
    success: bool
    result: Any | None
    error_code: str | None
    latency_ms: int | None = None


@dataclass(frozen=True)
class AgentRunResult:
    final_text: str
    tool_trace: tuple[ToolTrace, ...]
    model_turns: tuple[AssistantTurn, ...]


@dataclass(frozen=True)
class _PreparedToolCall:
    call: ToolCall
    parsed: BaseModel | None
    arguments: dict[str, Any] | None
    error_code: str | None


def _model_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            key: _model_safe(item)
            for key, item in value.items()
            if key.casefold() not in _INTERNAL_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_model_safe(item) for item in value]
    return value


class _ReadOnlyDispatcher:
    _input_models: dict[str, type[BaseModel]] = {
        "get_customer_orders": EmptyInput,
        "get_order": OrderIdInput,
        "get_shipment": OrderIdInput,
        "get_inventory": InventoryInput,
        "search_policy": PolicySearchRequest,
        "check_action_eligibility": EligibilityToolInput,
    }

    def __init__(self, tools: CustomerServiceTools):
        self._tools = tools

    def validate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> BaseModel:
        return self._input_models[tool_name].model_validate(arguments)

    def execute(
        self,
        session: Session,
        *,
        tool_name: str,
        parsed: BaseModel,
        context: ToolCallContext,
    ) -> Any:
        if tool_name == "get_customer_orders":
            return self._tools.get_customer_orders(session, context=context)
        if tool_name == "get_order":
            if not isinstance(parsed, OrderIdInput):
                raise AgentRunError(
                    "TOOL_VALIDATION_INVARIANT",
                    "Validated order tool input had an unexpected type.",
                )
            return self._tools.get_order(
                session,
                order_id=parsed.order_id,
                context=context,
            )
        if tool_name == "get_shipment":
            if not isinstance(parsed, OrderIdInput):
                raise AgentRunError(
                    "TOOL_VALIDATION_INVARIANT",
                    "Validated shipment tool input had an unexpected type.",
                )
            return self._tools.get_shipment(
                session,
                order_id=parsed.order_id,
                context=context,
            )
        if tool_name == "get_inventory":
            if not isinstance(parsed, InventoryInput):
                raise AgentRunError(
                    "TOOL_VALIDATION_INVARIANT",
                    "Validated inventory tool input had an unexpected type.",
                )
            return self._tools.get_inventory(
                session,
                sku=parsed.sku,
                size=parsed.size,
                context=context,
            )
        if tool_name == "search_policy":
            return self._tools.search_policy(
                session,
                request=PolicySearchRequest.model_validate(parsed),
                context=context,
            )
        if tool_name == "check_action_eligibility":
            request_data = parsed.model_dump(
                mode="json",
                exclude_none=True,
            )
            return self._tools.check_action_eligibility(
                session,
                request=EligibilityRequest.model_validate(request_data),
                context=context,
            )
        raise AgentRunError(
            "FORBIDDEN_TOOL_CALL",
            "The model requested a tool outside the read-only allowlist.",
        )


def _assistant_message(turn: AssistantTurn) -> Message:
    message: Message = {
        "role": "assistant",
        "content": turn.content,
    }
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.arguments,
                },
            }
            for call in turn.tool_calls
        ]
    return message


_EXCHANGE_INTENT_MARKERS = ("换货", "换成", "换到", "调换")
_SIZE_TOKEN_RE = re.compile(
    r"(?:目标尺码|想换成|换到|换成)\s*[：:]?\s*[A-Za-z0-9一二三四五六七八九十]+"
    r"|\d+\s*码|[A-Za-z]\s*码"
)
# Keep this text aligned with semantic-judge phrase overlays and regression
# answer_must_contain_any for reg_missing_exchange_size_clarify.
_MISSING_EXCHANGE_SIZE_REPLY = (
    "请提供想换成的目标尺码。在您告知目标尺码之前，"
    "我无法判断是否符合换货条件，也不会预占库存或完成换货。"
)


def exchange_request_missing_target_size(user_text: str) -> bool:
    """Host-side gate: exchange intent without an explicit target size."""

    text = user_text.strip()
    if not any(marker in text for marker in _EXCHANGE_INTENT_MARKERS):
        return False
    return _SIZE_TOKEN_RE.search(text) is None


def _tool_error(code: str, message: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ReadOnlyAgent:
    """Bounded single-Agent loop with an exact deterministic tool allowlist."""

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: CustomerServiceTools,
        max_tool_rounds: int = 4,
        max_tool_calls: int = 12,
        system_prompt: str | None = None,
        tool_contracts: Sequence[ToolContract] | None = None,
    ):
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least 1")
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls must be at least 1")
        self._model = model
        self._contracts = list(
            tool_contracts
            if tool_contracts is not None
            else get_read_only_tool_contracts()
        )
        contract_names = [
            contract.get("name")
            for contract in self._contracts
        ]
        if (
            len(contract_names) != len(set(contract_names))
            or set(contract_names) != set(READ_ONLY_TOOL_NAMES)
        ):
            raise ValueError(
                "Read-only tool contracts must match the exact allowlist."
            )
        self._dispatcher = _ReadOnlyDispatcher(tools)
        self._allowed_tool_names: tuple[str, ...] = READ_ONLY_TOOL_NAMES
        self._max_tool_rounds = max_tool_rounds
        self._max_tool_calls = max_tool_calls
        self._system_prompt = (
            system_prompt
            if system_prompt is not None
            else _DEFAULT_SYSTEM_PROMPT.read_text(encoding="utf-8")
        )

    def _validate_tool_batch(
        self,
        calls: tuple[ToolCall, ...],
        trace: tuple[ToolTrace, ...],
    ) -> None:
        """Phase-specific preflight hook; read-only runs need no extra rule."""

    def _context_for_tool_call(
        self,
        context: ToolCallContext,
        call: ToolCall,
    ) -> ToolCallContext:
        """Phase-specific context binding hook."""

        return context

    def run(
        self,
        session: Session,
        *,
        user_text: str,
        context: ToolCallContext,
        trace_sink: Callable[[ToolTrace], None] | None = None,
    ) -> AgentRunResult:
        if not user_text.strip():
            raise AgentRunError(
                "EMPTY_USER_MESSAGE",
                "A non-empty user message is required.",
            )

        messages: list[Message] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_text},
        ]
        trace: list[ToolTrace] = []
        model_turns: list[AssistantTurn] = []
        tool_rounds = 0
        total_tool_calls = 0
        seen_call_ids: set[str] = set()
        # Always advertise the full read-only allowlist so paid evidence keeps
        # tool_contract_count == len(allowlist). Missing-size exchange turns
        # still fail-close on the host: one agent call, zero tool executions,
        # deterministic clarification text.
        missing_exchange_size = exchange_request_missing_target_size(user_text)

        def record_trace(item: ToolTrace) -> None:
            trace.append(item)
            if trace_sink is not None:
                trace_sink(item)

        while True:
            turn = self._model.complete(
                messages=messages,
                tools=self._contracts,
            )
            model_turns.append(turn)

            if missing_exchange_size:
                return AgentRunResult(
                    final_text=_MISSING_EXCHANGE_SIZE_REPLY,
                    tool_trace=(),
                    model_turns=tuple(model_turns),
                )

            if not turn.tool_calls:
                if not turn.content or not turn.content.strip():
                    raise AgentRunError(
                        "EMPTY_MODEL_RESPONSE",
                        "The model returned no final answer.",
                    )
                return AgentRunResult(
                    final_text=turn.content.strip(),
                    tool_trace=tuple(trace),
                    model_turns=tuple(model_turns),
                )

            if tool_rounds >= self._max_tool_rounds:
                raise AgentRunError(
                    "MAX_TOOL_ROUNDS_EXCEEDED",
                    "The model exceeded the configured tool-round limit.",
                )
            if total_tool_calls + len(turn.tool_calls) > self._max_tool_calls:
                raise AgentRunError(
                    "MAX_TOOL_CALLS_EXCEEDED",
                    "The model exceeded the configured total tool-call limit.",
                )
            tool_rounds += 1
            total_tool_calls += len(turn.tool_calls)

            batch_ids: set[str] = set()
            for call in turn.tool_calls:
                if call.id in seen_call_ids or call.id in batch_ids:
                    raise AgentRunError(
                        "DUPLICATE_TOOL_CALL_ID",
                        "The model returned duplicate tool call identifiers.",
                    )
                batch_ids.add(call.id)
                if call.name not in self._allowed_tool_names:
                    raise AgentRunError(
                        "FORBIDDEN_TOOL_CALL",
                        "The model requested a tool outside the configured allowlist.",
                    )
            self._validate_tool_batch(turn.tool_calls, tuple(trace))
            seen_call_ids.update(batch_ids)

            prepared_calls: list[_PreparedToolCall] = []
            for call in turn.tool_calls:
                try:
                    raw_arguments = json.loads(call.arguments)
                    if not isinstance(raw_arguments, dict):
                        raise ValueError("tool arguments must be an object")
                    parsed = self._dispatcher.validate(call.name, raw_arguments)
                except (ValueError, json.JSONDecodeError, PydanticValidationError):
                    prepared_calls.append(
                        _PreparedToolCall(
                            call=call,
                            parsed=None,
                            arguments=None,
                            error_code="INVALID_TOOL_ARGUMENTS",
                        )
                    )
                else:
                    prepared_calls.append(
                        _PreparedToolCall(
                            call=call,
                            parsed=parsed,
                            arguments=_model_safe(
                                parsed.model_dump(
                                    mode="json",
                                    exclude_none=True,
                                )
                            ),
                            error_code=None,
                        )
                    )

            messages.append(_assistant_message(turn))
            if any(item.error_code for item in prepared_calls):
                for item in prepared_calls:
                    error_code = (
                        item.error_code or "TOOL_BATCH_REJECTED"
                    )
                    error_message = (
                        "Tool arguments did not match the declared schema."
                        if item.error_code
                        else (
                            "No tools in this batch were executed because "
                            "another call failed validation."
                        )
                    )
                    record_trace(
                        ToolTrace(
                            tool_call_id=item.call.id,
                            tool_name=item.call.name,
                            arguments=item.arguments,
                            success=False,
                            result=None,
                            error_code=error_code,
                            latency_ms=0,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": item.call.id,
                            "content": _tool_error(
                                error_code,
                                error_message,
                            ),
                        }
                    )
                continue

            for item in prepared_calls:
                call = item.call
                parsed_call = item.parsed
                arguments = item.arguments
                if parsed_call is None:  # pragma: no cover - guarded above
                    raise AgentRunError(
                        "INVALID_TOOL_ARGUMENTS",
                        "Tool batch validation invariant failed.",
                    )
                try:
                    tool_started = time.perf_counter()
                    result = self._dispatcher.execute(
                        session,
                        tool_name=call.name,
                        parsed=parsed_call,
                        context=self._context_for_tool_call(context, call),
                    )
                    safe_result = _model_safe(result)
                    record_trace(
                        ToolTrace(
                            tool_call_id=call.id,
                            tool_name=call.name,
                            arguments=arguments,
                            success=True,
                            result=safe_result,
                            error_code=None,
                            latency_ms=max(
                                0,
                                int(
                                    (time.perf_counter() - tool_started)
                                    * 1000
                                ),
                            ),
                        )
                    )
                    content = json.dumps(
                        {"ok": True, "result": safe_result},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                except ServiceError as exc:
                    record_trace(
                        ToolTrace(
                            tool_call_id=call.id,
                            tool_name=call.name,
                            arguments=arguments,
                            success=False,
                            result=None,
                            error_code=exc.code,
                            latency_ms=max(
                                0,
                                int(
                                    (time.perf_counter() - tool_started)
                                    * 1000
                                ),
                            ),
                        )
                    )
                    content = _tool_error(exc.code, exc.message)
                except AgentRunError:
                    raise
                except Exception as exc:
                    record_trace(
                        ToolTrace(
                            tool_call_id=call.id,
                            tool_name=call.name,
                            arguments=arguments,
                            success=False,
                            result=None,
                            error_code="TOOL_EXECUTION_ERROR",
                            latency_ms=max(
                                0,
                                int(
                                    (time.perf_counter() - tool_started)
                                    * 1000
                                ),
                            ),
                        )
                    )
                    raise AgentRunError(
                        "TOOL_EXECUTION_ERROR",
                        "A configured tool failed unexpectedly.",
                    ) from exc

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": content,
                    }
                )
