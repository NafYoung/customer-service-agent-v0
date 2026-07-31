from __future__ import annotations

import random
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Protocol, Sequence

import httpx

from app.agent.deepseek_budget import (
    BudgetError,
    BudgetExceededError,
    BudgetPriceWindowError,
    BudgetUsageError,
    logical_call_sha256,
)

Message = dict[str, Any]
ToolContract = dict[str, Any]
ModelErrorStage = Literal["reserve_attempt", "provider_attempt"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class AssistantTurn:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    usage: dict[str, int] | None
    response_id: str | None = None
    model: str | None = None
    provider_request_id: str | None = None
    provider_attempts: int = 1
    logical_call_sha256: str | None = None


class ChatModel(Protocol):
    def complete(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolContract],
    ) -> AssistantTurn: ...


class AttemptBudgetGuard(Protocol):
    def bind_request_timeout(self, *, timeout_seconds: float) -> None: ...

    def reserve_attempt(
        self,
        *,
        logical_call_id: str,
        attempt_number: int,
    ) -> Any: ...

    def settle_attempt(
        self,
        *,
        reservation: Any,
        usage: Mapping[str, Any],
        provider_request_id: str | None,
        response_content_sha256: str | None = None,
    ) -> Any: ...

    def bind_response_content_sha256(
        self,
        *,
        logical_call_sha256: str,
        response_content_sha256: str,
    ) -> None: ...

    def ensure_response_in_price_window(
        self,
        *,
        reservation: Any,
        usage: Mapping[str, Any] | None,
        provider_request_id: str | None,
    ) -> None: ...

    def mark_uncertain(
        self,
        *,
        reservation: Any,
        error_code: str,
    ) -> None: ...

    def close(self) -> None: ...


class ModelAdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        attempts: int | None = None,
        logical_call_sha256: str | None = None,
        error_stage: ModelErrorStage | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.attempts = attempts
        self.logical_call_sha256 = logical_call_sha256
        self.error_stage = error_stage


class ModelAPIError(ModelAdapterError):
    pass


class ModelProtocolError(ModelAdapterError):
    pass


class OpenAICompatibleChatClient:
    """Minimal non-streaming Chat Completions adapter.

    The client deliberately implements only the common request surface used by
    this project. Provider credentials remain private instance state and are
    never copied into messages, tool contracts, return values, or exceptions.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_retries: int = 0,
        retry_backoff_seconds: float = 0.25,
        extra_body: Mapping[str, Any] | None = None,
        budget_guard: AttemptBudgetGuard | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        if not api_key:
            raise ValueError("A provider API key is required")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if not model:
            raise ValueError("A model name is required")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds cannot be negative")
        if max_tokens is not None and max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        reserved_extra_fields = {
            "messages",
            "model",
            "response_format",
            "stream",
            "temperature",
            "tool_choice",
            "tools",
        }
        overridden_fields = reserved_extra_fields.intersection(
            extra_body or {}
        )
        if overridden_fields:
            raise ValueError(
                "extra_body cannot override reserved protocol fields: "
                + ", ".join(sorted(overridden_fields))
            )

        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._extra_body = dict(extra_body or {})
        self._budget_guard = budget_guard
        # Construction-time hint only; formal binding derives mode from the live
        # httpx client transport (see live_transport_mode).
        self._transport_mode = "custom" if transport is not None else "default"
        if self._budget_guard is not None:
            self._budget_guard.bind_request_timeout(
                timeout_seconds=timeout_seconds,
            )
        self._closed = False
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._client.close()
        if self._budget_guard is not None:
            self._budget_guard.close()
        self._closed = True

    def __enter__(self) -> OpenAICompatibleChatClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def transport_mode_for_client(client: object) -> str:
        """Derive transport mode from the live httpx client, not a writable flag."""

        if type(client) is not httpx.Client:
            return "custom"
        transport = getattr(client, "_transport", None)
        if type(transport) is httpx.HTTPTransport:
            return "default"
        return "custom"

    def live_transport_mode(self) -> str:
        """Return transport mode sealed to the current ``_client`` object."""

        return self.transport_mode_for_client(getattr(self, "_client", None))

    def bind_response_content_sha256(
        self,
        *,
        logical_call_sha256: str,
        response_content_sha256: str,
    ) -> None:
        """Seal a settled paid attempt to one judge response digest."""

        if self._budget_guard is None:
            return
        self._budget_guard.bind_response_content_sha256(
            logical_call_sha256=logical_call_sha256,
            response_content_sha256=response_content_sha256,
        )

    def public_runtime_config(self) -> dict[str, object]:
        """Return credential-free configuration suitable for runtime binding."""

        return {
            "adapter": "openai_compatible_chat_completions",
            "base_url": self._base_url,
            "model": self._model,
            "timeout_seconds": self._timeout_seconds,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "max_retries": self._max_retries,
            "retry_backoff_seconds": self._retry_backoff_seconds,
            "extra_body": deepcopy(self._extra_body),
            "transport_mode": self.live_transport_mode(),
            "budget_guard_attached": self._budget_guard is not None,
        }

    def uses_budget_guard(self, budget_guard: object) -> bool:
        """Check guard identity without exposing the guard or any credential."""

        return self._budget_guard is budget_guard

    @staticmethod
    def _openai_tools(
        contracts: Sequence[ToolContract],
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": contract["name"],
                    "description": contract["description"],
                    "parameters": contract["input_schema"],
                },
            }
            for contract in contracts
        ]

    def complete(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolContract],
    ) -> AssistantTurn:
        return self._complete(
            messages=messages,
            tools=tools,
            response_format=None,
        )

    def complete_json(
        self,
        *,
        messages: Sequence[Message],
    ) -> AssistantTurn:
        """Request one tool-free JSON object for an isolated evaluator."""

        return self._complete(
            messages=messages,
            tools=(),
            response_format={"type": "json_object"},
        )

    def _complete(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolContract],
        response_format: Mapping[str, str] | None,
    ) -> AssistantTurn:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if tools:
            payload["tools"] = self._openai_tools(tools)
            payload["tool_choice"] = "auto"
        payload.update(self._extra_body)

        logical_call_id = f"model-call-{uuid.uuid4().hex}"
        logical_call_hash = logical_call_sha256(logical_call_id)
        for attempt_index in range(self._max_retries + 1):
            attempt_count = attempt_index + 1
            reservation: Any | None = None
            if self._budget_guard is not None:
                try:
                    reservation = self._budget_guard.reserve_attempt(
                        logical_call_id=logical_call_id,
                        attempt_number=attempt_count,
                    )
                except BudgetPriceWindowError as exc:
                    raise ModelAPIError(
                        "MODEL_PRICE_EXPIRED",
                        "Paid model request blocked by the local pricing window.",
                        attempts=attempt_index,
                        logical_call_sha256=logical_call_hash,
                        error_stage="reserve_attempt",
                    ) from exc
                except BudgetExceededError as exc:
                    raise ModelAPIError(
                        "MODEL_BUDGET_EXHAUSTED",
                        "Paid model request blocked by the local CNY budget limit.",
                        attempts=attempt_index,
                        logical_call_sha256=logical_call_hash,
                        error_stage="reserve_attempt",
                    ) from exc
                except BudgetError as exc:
                    raise ModelAPIError(
                        "MODEL_BUDGET_ERROR",
                        "Paid model request blocked by a local budget ledger error.",
                        attempts=attempt_index,
                        logical_call_sha256=logical_call_hash,
                        error_stage="reserve_attempt",
                    ) from exc

            try:
                response = self._client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.RequestError as exc:
                self._mark_budget_uncertain(
                    reservation,
                    error_code="MODEL_TRANSPORT_ERROR",
                    attempts=attempt_count,
                    logical_call_hash=logical_call_hash,
                )
                # Under a budget guard, transport failures fail closed after the
                # first reservation. Retrying would permanently lock another
                # full worst-case reservation while known_cost stays empty.
                if (
                    self._budget_guard is None
                    and attempt_index < self._max_retries
                ):
                    self._retry_delay(attempt_index)
                    continue
                raise ModelAPIError(
                    "MODEL_TRANSPORT_ERROR",
                    "Model provider request failed before a response was received.",
                    attempts=attempt_count,
                    logical_call_sha256=logical_call_hash,
                    error_stage="provider_attempt",
                ) from exc

            request_id = (
                response.headers.get("x-request-id")
                or response.headers.get("request-id")
            )
            if response.status_code >= 400:
                self._ensure_response_price_window(
                    reservation,
                    usage=None,
                    provider_request_id=request_id,
                    attempts=attempt_count,
                    logical_call_hash=logical_call_hash,
                )
                self._mark_budget_uncertain(
                    reservation,
                    error_code="MODEL_HTTP_ERROR",
                    attempts=attempt_count,
                    logical_call_hash=logical_call_hash,
                )
                retryable_status = (
                    response.status_code == 429
                    or response.status_code >= 500
                )
                if retryable_status and attempt_index < self._max_retries:
                    self._retry_delay(attempt_index)
                    continue
                raise ModelAPIError(
                    "MODEL_HTTP_ERROR",
                    (
                        "Model provider returned HTTP status "
                        f"{response.status_code}."
                    ),
                    status_code=response.status_code,
                    request_id=request_id,
                    attempts=attempt_count,
                    logical_call_sha256=logical_call_hash,
                    error_stage="provider_attempt",
                )

            try:
                turn = self._parse_success_response(
                    response,
                    request_id=request_id,
                    attempt_count=attempt_count,
                )
                turn = replace(
                    turn,
                    logical_call_sha256=logical_call_hash,
                )
            except ModelAdapterError as exc:
                exc.logical_call_sha256 = logical_call_hash
                exc.error_stage = "provider_attempt"
                self._ensure_response_price_window(
                    reservation,
                    usage=None,
                    provider_request_id=request_id,
                    attempts=attempt_count,
                    logical_call_hash=logical_call_hash,
                )
                self._mark_budget_uncertain(
                    reservation,
                    error_code=exc.code,
                    attempts=attempt_count,
                    logical_call_hash=logical_call_hash,
                )
                raise

            if self._budget_guard is not None:
                self._ensure_response_price_window(
                    reservation,
                    usage=turn.usage,
                    provider_request_id=request_id,
                    attempts=attempt_count,
                    logical_call_hash=logical_call_hash,
                )
                if turn.usage is None:
                    self._mark_budget_uncertain(
                        reservation,
                        error_code="MISSING_PROVIDER_USAGE",
                        attempts=attempt_count,
                        logical_call_hash=logical_call_hash,
                    )
                    raise ModelAPIError(
                        "MODEL_BUDGET_USAGE_ERROR",
                        "Provider response omitted billable usage evidence.",
                        request_id=request_id,
                        attempts=attempt_count,
                        logical_call_sha256=logical_call_hash,
                        error_stage="provider_attempt",
                    )
                try:
                    self._budget_guard.settle_attempt(
                        reservation=reservation,
                        usage=turn.usage,
                        provider_request_id=request_id,
                    )
                except BudgetUsageError as exc:
                    raise ModelAPIError(
                        "MODEL_BUDGET_USAGE_ERROR",
                        "Provider returned inconsistent billable usage evidence.",
                        request_id=request_id,
                        attempts=attempt_count,
                        logical_call_sha256=logical_call_hash,
                        error_stage="provider_attempt",
                    ) from exc
                except BudgetError as exc:
                    self._mark_budget_uncertain(
                        reservation,
                        error_code="MODEL_BUDGET_ERROR",
                        attempts=attempt_count,
                        logical_call_hash=logical_call_hash,
                    )
                    raise ModelAPIError(
                        "MODEL_BUDGET_ERROR",
                        "Paid model response could not be settled safely.",
                        request_id=request_id,
                        attempts=attempt_count,
                        logical_call_sha256=logical_call_hash,
                        error_stage="provider_attempt",
                    ) from exc
            return turn

        raise ModelAPIError(  # pragma: no cover - bounded loop invariant
            "MODEL_TRANSPORT_ERROR",
            "Model provider request loop ended unexpectedly.",
            attempts=self._max_retries + 1,
            logical_call_sha256=logical_call_hash,
            error_stage="provider_attempt",
        )

    def _ensure_response_price_window(
        self,
        reservation: Any | None,
        *,
        usage: Mapping[str, Any] | None,
        provider_request_id: str | None,
        attempts: int,
        logical_call_hash: str,
    ) -> None:
        if self._budget_guard is None or reservation is None:
            return
        try:
            self._budget_guard.ensure_response_in_price_window(
                reservation=reservation,
                usage=usage,
                provider_request_id=provider_request_id,
            )
        except BudgetPriceWindowError as exc:
            raise ModelAPIError(
                "MODEL_PRICE_EXPIRED",
                "Paid model response crossed the local pricing window.",
                request_id=provider_request_id,
                attempts=attempts,
                logical_call_sha256=logical_call_hash,
                error_stage="provider_attempt",
            ) from exc
        except BudgetError as exc:
            raise ModelAPIError(
                "MODEL_BUDGET_ERROR",
                "Paid model response could not be recorded safely.",
                request_id=provider_request_id,
                attempts=attempts,
                logical_call_sha256=logical_call_hash,
                error_stage="provider_attempt",
            ) from exc

    def _retry_delay(self, attempt_index: int) -> None:
        delay = self._retry_backoff_seconds * (2**attempt_index)
        if delay:
            time.sleep(delay + random.uniform(0, delay * 0.25))

    def _mark_budget_uncertain(
        self,
        reservation: Any | None,
        *,
        error_code: str,
        attempts: int,
        logical_call_hash: str,
    ) -> None:
        if self._budget_guard is None or reservation is None:
            return
        try:
            self._budget_guard.mark_uncertain(
                reservation=reservation,
                error_code=error_code,
            )
        except BudgetError as exc:
            raise ModelAPIError(
                "MODEL_BUDGET_ERROR",
                "Paid model attempt could not be recorded safely.",
                attempts=attempts,
                logical_call_sha256=logical_call_hash,
                error_stage="provider_attempt",
            ) from exc

    @staticmethod
    def _parse_success_response(
        response: httpx.Response,
        *,
        request_id: str | None,
        attempt_count: int,
    ) -> AssistantTurn:
        try:
            data = response.json()
            choices = data["choices"]
            choice = choices[0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelProtocolError(
                "MODEL_PROTOCOL_ERROR",
                "Model provider returned a malformed completion response.",
                request_id=request_id,
                attempts=attempt_count,
            ) from exc

        if not isinstance(message, dict):
            raise ModelProtocolError(
                "MODEL_PROTOCOL_ERROR",
                "Model provider returned an invalid assistant message.",
                request_id=request_id,
                attempts=attempt_count,
            )

        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ModelProtocolError(
                "MODEL_PROTOCOL_ERROR",
                "Model provider returned invalid assistant content.",
                request_id=request_id,
                attempts=attempt_count,
            )

        raw_tool_calls = message.get("tool_calls") or []
        if not isinstance(raw_tool_calls, list):
            raise ModelProtocolError(
                "MODEL_PROTOCOL_ERROR",
                "Model provider returned invalid tool calls.",
                request_id=request_id,
                attempts=attempt_count,
            )

        parsed_calls: list[ToolCall] = []
        try:
            for raw_call in raw_tool_calls:
                if raw_call.get("type") != "function":
                    raise TypeError("unsupported tool call type")
                function = raw_call["function"]
                call_id = raw_call["id"]
                name = function["name"]
                arguments = function["arguments"]
                if not all(
                    isinstance(value, str) and value
                    for value in (call_id, name, arguments)
                ):
                    raise TypeError("invalid tool call field")
                parsed_calls.append(
                    ToolCall(
                        id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ModelProtocolError(
                "MODEL_PROTOCOL_ERROR",
                "Model provider returned a malformed tool call.",
                request_id=request_id,
                attempts=attempt_count,
            ) from exc

        usage = data.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise ModelProtocolError(
                    "MODEL_PROTOCOL_ERROR",
                    "Model provider returned invalid usage data.",
                    request_id=request_id,
                    attempts=attempt_count,
                )
            usage = {
                key: value
                for key, value in usage.items()
                if isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
            }

        finish_reason = choice.get("finish_reason")
        if finish_reason == "insufficient_system_resource":
            raise ModelAPIError(
                "MODEL_INSUFFICIENT_RESOURCE",
                "Model provider could not complete the request.",
                request_id=request_id,
                attempts=attempt_count,
            )
        if finish_reason in {"length", "content_filter"}:
            raise ModelAPIError(
                "MODEL_INCOMPLETE_RESPONSE",
                "Model provider returned an incomplete response.",
                request_id=request_id,
                attempts=attempt_count,
            )
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ModelProtocolError(
                "MODEL_PROTOCOL_ERROR",
                "Model provider returned an invalid finish reason.",
                request_id=request_id,
                attempts=attempt_count,
            )
        if finish_reason not in {"stop", "tool_calls"}:
            raise ModelProtocolError(
                "MODEL_PROTOCOL_ERROR",
                "Model provider returned an unknown finish reason.",
                request_id=request_id,
                attempts=attempt_count,
            )
        if finish_reason == "tool_calls" and not parsed_calls:
            raise ModelProtocolError(
                "MODEL_PROTOCOL_ERROR",
                "Model provider ended with tool_calls but returned no calls.",
                request_id=request_id,
                attempts=attempt_count,
            )
        if finish_reason == "stop" and parsed_calls:
            raise ModelProtocolError(
                "MODEL_PROTOCOL_ERROR",
                "Model provider returned tool calls with an inconsistent finish reason.",
                request_id=request_id,
                attempts=attempt_count,
            )

        return AssistantTurn(
            content=content,
            tool_calls=tuple(parsed_calls),
            finish_reason=finish_reason,
            usage=usage,
            response_id=data.get("id") if isinstance(data.get("id"), str) else None,
            model=data.get("model") if isinstance(data.get("model"), str) else None,
            provider_request_id=request_id,
            provider_attempts=attempt_count,
        )
