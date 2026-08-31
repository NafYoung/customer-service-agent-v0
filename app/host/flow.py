from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from pydantic import Field
from sqlalchemy.orm import Session

from app.agent.openai_compatible import ChatModel, ModelAdapterError
from app.agent.preparation import PreparationAgent
from app.agent.readonly import AgentRunError, ToolTrace
from app.config import Settings
from app.enums import (
    ApprovalStatus,
    ConversationMode,
    EligibilityReason,
    HandoffReason,
    TicketPriority,
)
from app.errors import ConflictError, ServiceError
from app.schemas import (
    APIModel,
    ConfirmationRecorded,
    ExecuteActionResponse,
    ManualHandoffRead,
    ManualHandoffRequest,
)
from app.services.actions import HostApprovalPresentation
from app.tools.facade import CustomerServiceTools, ToolCallContext


class HostMessageRequest(APIModel):
    text: str = Field(min_length=1, max_length=4000)


class HostMessageResponse(APIModel):
    mode: ConversationMode
    reply: str
    server_run_id: str
    pending_approval_id: str | None = None
    handoff: ManualHandoffRead | None = None
    safety_notice: str


class HostApprovalCard(APIModel):
    approval_id: str
    status: ApprovalStatus
    preview: dict[str, object]
    presented_at: datetime


_AGENT_SAFETY_NOTICE = (
    "本轮尚未执行任何交易（即没有执行）；如有待确认操作，仍需先展示并确认。"
)
_MANUAL_SAFETY_NOTICE = "本会话已转由人工处理，本轮没有执行任何交易。"
_MANUAL_REPLY = "该会话已转交人工客服继续处理。"
_HUMAN_REVIEW_HANDOFFS = {
    "WRONG_ITEM": (HandoffReason.WRONG_ITEM, "错发商品"),
    "DEFECTIVE": (HandoffReason.DEFECTIVE_ITEM, "商品质量问题"),
    "DAMAGED_IN_TRANSIT": (HandoffReason.DAMAGED_IN_TRANSIT, "运输破损"),
}


class HostPilotFlow:
    """Keep trusted host state outside the model prompt and response trace."""

    def __init__(
        self,
        *,
        tools: CustomerServiceTools,
        settings: Settings,
        preparation_model_factory: Callable[[], ChatModel] | None,
    ):
        self._tools = tools
        self._settings = settings
        self._preparation_model_factory = preparation_model_factory

    @staticmethod
    def _server_run_id() -> str:
        return f"RUN-{uuid.uuid4().hex.upper()}"

    @staticmethod
    def _manual_response(
        *,
        server_run_id: str,
        handoff: ManualHandoffRead,
    ) -> HostMessageResponse:
        return HostMessageResponse(
            mode=ConversationMode.MANUAL,
            reply=_MANUAL_REPLY,
            server_run_id=server_run_id,
            pending_approval_id=None,
            handoff=handoff,
            safety_notice=_MANUAL_SAFETY_NOTICE,
        )

    @staticmethod
    def _human_review_handoff(
        trace: tuple[ToolTrace, ...],
    ) -> ManualHandoffRequest | None:
        for item in trace:
            if (
                not item.success
                or item.tool_name != "check_action_eligibility"
                or not isinstance(item.result, dict)
                or item.result.get("reason_code")
                != EligibilityReason.HUMAN_REVIEW_REQUIRED.value
                or not isinstance(item.arguments, dict)
            ):
                continue

            order_id = item.arguments.get("order_id")
            if not isinstance(order_id, str) or not order_id:
                continue

            issue_type = item.arguments.get("issue_type")
            transfer_reason, label = _HUMAN_REVIEW_HANDOFFS.get(
                str(issue_type),
                (HandoffReason.AUTOMATION_UNSAFE, "需人工核验的问题"),
            )
            return ManualHandoffRequest(
                order_id=order_id,
                transfer_reason=transfer_reason,
                summary=f"系统资格核验显示订单 {order_id} 存在{label}，需要人工继续处理。",
                priority=TicketPriority.HIGH,
            )
        return None

    def _current_manual_handoff(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
    ) -> ManualHandoffRead | None:
        return self._tools.ticket_service.get_manual_handoff(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
        )

    def _manual_handoff_after_agent_ownership_check(
        self,
        session: Session,
        *,
        customer_id: str,
        conversation_id: str,
    ) -> ManualHandoffRead | None:
        try:
            self._tools.ticket_service.assert_agent_owned(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
            )
        except ConflictError as exc:
            if exc.code != "CONVERSATION_MANUAL":
                raise
            handoff = self._current_manual_handoff(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
            )
            if handoff is not None:
                return handoff
            raise
        return None

    def handle_message(
        self,
        session: Session,
        *,
        auth_token: str,
        conversation_id: str,
        text: str,
    ) -> HostMessageResponse:
        """Run a bounded preparation turn unless the conversation is manual."""

        customer_id = self._tools.auth_service.resolve_customer_id(session, auth_token)
        server_run_id = self._server_run_id()
        existing_handoff = self._current_manual_handoff(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
        )
        if existing_handoff is not None:
            return self._manual_response(
                server_run_id=server_run_id,
                handoff=existing_handoff,
            )
        existing_handoff = self._manual_handoff_after_agent_ownership_check(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
        )
        if existing_handoff is not None:
            return self._manual_response(
                server_run_id=server_run_id,
                handoff=existing_handoff,
            )

        if self._preparation_model_factory is None:
            raise ServiceError(
                "AGENT_RUNTIME_UNAVAILABLE",
                "自动处理运行时尚未配置。",
                status_code=503,
            )

        context = ToolCallContext(
            auth_token=auth_token,
            conversation_id=conversation_id,
            server_run_id=server_run_id,
        )
        existing_handoff = None
        handoff_request = None
        try:
            with session.begin_nested() as host_run:
                agent = PreparationAgent(
                    model=self._preparation_model_factory(),
                    tools=self._tools,
                    max_tool_rounds=self._settings.agent_max_tool_rounds,
                    max_tool_calls=self._settings.agent_max_tool_calls,
                )
                result = agent.run(
                    session,
                    user_text=text,
                    context=context,
                )

                # A trusted handoff or deterministic human-review result wins
                # over every model-side read or proposal from this run. Roll
                # back the whole Agent savepoint before returning MANUAL so a
                # mixed trace cannot leave an Approval behind.
                existing_handoff = self._current_manual_handoff(
                    session,
                    customer_id=customer_id,
                    conversation_id=conversation_id,
                )
                if existing_handoff is None:
                    existing_handoff = (
                        self._manual_handoff_after_agent_ownership_check(
                            session,
                            customer_id=customer_id,
                            conversation_id=conversation_id,
                        )
                    )
                handoff_request = self._human_review_handoff(result.tool_trace)
                if existing_handoff is not None or handoff_request is not None:
                    host_run.rollback()
        except AgentRunError as exc:
            raise ServiceError(
                "AGENT_RUN_REJECTED",
                "本轮自动处理未完成，请稍后重试或申请人工协助。",
                status_code=422,
            ) from exc
        except ModelAdapterError as exc:
            raise ServiceError(
                "AGENT_RUNTIME_FAILED",
                "自动处理服务暂时不可用，请稍后重试或申请人工协助。",
                status_code=503,
            ) from exc

        if existing_handoff is not None:
            return self._manual_response(
                server_run_id=server_run_id,
                handoff=existing_handoff,
            )

        if handoff_request is not None:
            try:
                handoff = self._tools.ticket_service.create_manual_handoff(
                    session,
                    customer_id=customer_id,
                    conversation_id=conversation_id,
                    origin_server_run_id=server_run_id,
                    request=handoff_request,
                )
            except ConflictError as exc:
                if exc.code != "HANDOFF_ALREADY_MANUAL":
                    raise
                existing_handoff = self._current_manual_handoff(
                    session,
                    customer_id=customer_id,
                    conversation_id=conversation_id,
                )
                if existing_handoff is None:
                    raise
                handoff = existing_handoff
            return self._manual_response(
                server_run_id=server_run_id,
                handoff=handoff,
            )

        return HostMessageResponse(
            mode=ConversationMode.AGENT,
            reply=result.final_text,
            server_run_id=server_run_id,
            pending_approval_id=(
                result.prepared_action.approval_id
                if result.prepared_action is not None
                else None
            ),
            handoff=None,
            safety_notice=_AGENT_SAFETY_NOTICE,
        )

    def create_explicit_handoff(
        self,
        session: Session,
        *,
        auth_token: str,
        conversation_id: str,
        request: ManualHandoffRequest,
    ) -> ManualHandoffRead:
        customer_id = self._tools.auth_service.resolve_customer_id(session, auth_token)
        return self._tools.ticket_service.create_manual_handoff(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            origin_server_run_id=self._server_run_id(),
            request=request,
        )

    def present_approval(
        self,
        session: Session,
        *,
        auth_token: str,
        conversation_id: str,
        approval_id: str,
    ) -> HostApprovalCard:
        customer_id = self._tools.auth_service.resolve_customer_id(session, auth_token)
        presentation: HostApprovalPresentation = (
            self._tools.action_service.present_host_approval(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
                approval_id=approval_id,
            )
        )
        return HostApprovalCard(
            approval_id=presentation.approval_id,
            status=presentation.status,
            preview=presentation.preview,
            presented_at=presentation.presented_at,
        )

    def record_confirmation(
        self,
        session: Session,
        *,
        auth_token: str,
        conversation_id: str,
        approval_id: str,
    ) -> ConfirmationRecorded:
        customer_id = self._tools.auth_service.resolve_customer_id(session, auth_token)
        return self._tools.action_service.record_host_confirmation(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
        )

    def execute_confirmation(
        self,
        session: Session,
        *,
        auth_token: str,
        conversation_id: str,
        approval_id: str,
        confirmation_event_id: str,
    ) -> ExecuteActionResponse:
        customer_id = self._tools.auth_service.resolve_customer_id(session, auth_token)
        return self._tools.action_service.execute_confirmed_action(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
            confirmation_event_id=confirmation_event_id,
        )

    def mark_confirmation_failed(
        self,
        session: Session,
        *,
        auth_token: str,
        conversation_id: str,
        approval_id: str,
        failure_code: str,
    ) -> None:
        customer_id = self._tools.auth_service.resolve_customer_id(session, auth_token)
        if failure_code == "APPROVAL_EXPIRED":
            self._tools.action_service.mark_expired(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
                approval_id=approval_id,
            )
            return
        self._tools.action_service.mark_failed(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
            failure_code=failure_code,
        )
