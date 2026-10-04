from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import StrEnum
from typing import TypeVar

from sqlalchemy.orm import Session

from app.errors import ServiceError
from app.schemas import ConfirmActionRequest, ExecuteActionResponse
from app.services.actions import ActionService

_T = TypeVar("_T")

OpenSession = Callable[[], AbstractContextManager[Session]]
ResolveCustomerId = Callable[[Session], str]


class ExecuteFailurePolicy(StrEnum):
    MARK_FAILED = "mark_failed"
    LEAVE_RETRYABLE = "leave_retryable"


def confirm_and_execute(
    *,
    open_session: OpenSession,
    action_service: ActionService,
    resolve_customer_id: ResolveCustomerId,
    conversation_id: str,
    approval_id: str,
    request: ConfirmActionRequest,
    on_execute_4xx: ExecuteFailurePolicy = ExecuteFailurePolicy.MARK_FAILED,
) -> ExecuteActionResponse:
    if on_execute_4xx not in (
        ExecuteFailurePolicy.MARK_FAILED,
        ExecuteFailurePolicy.LEAVE_RETRYABLE,
    ):
        raise ValueError(f"unknown execute failure policy: {on_execute_4xx!r}")

    try:
        confirmation = _call(
            open_session,
            resolve_customer_id,
            lambda session, customer_id: action_service.record_confirmation(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
                approval_id=approval_id,
                request=request,
            ),
        )
    except ServiceError as exc:
        if _marks_client_failure(on_execute_4xx) and exc.code == "APPROVAL_EXPIRED":
            _mark_expired(
                open_session,
                action_service,
                resolve_customer_id,
                conversation_id=conversation_id,
                approval_id=approval_id,
            )
        raise

    try:
        return _call(
            open_session,
            resolve_customer_id,
            lambda session, customer_id: action_service.execute_confirmed_action(
                session,
                customer_id=customer_id,
                conversation_id=conversation_id,
                approval_id=approval_id,
                confirmation_event_id=confirmation.confirmation_event_id,
            ),
        )
    except ServiceError as exc:
        if _marks_client_failure(on_execute_4xx) and exc.status_code < 500:
            if exc.code == "APPROVAL_EXPIRED":
                _mark_expired(
                    open_session,
                    action_service,
                    resolve_customer_id,
                    conversation_id=conversation_id,
                    approval_id=approval_id,
                )
            else:
                _mark_failed(
                    open_session,
                    action_service,
                    resolve_customer_id,
                    conversation_id=conversation_id,
                    approval_id=approval_id,
                    failure_code=exc.code,
                )
        raise


def _marks_client_failure(on_execute_4xx: ExecuteFailurePolicy) -> bool:
    return on_execute_4xx == ExecuteFailurePolicy.MARK_FAILED


def _call(
    open_session: OpenSession,
    resolve_customer_id: ResolveCustomerId,
    use: Callable[[Session, str], _T],
) -> _T:
    with open_session() as session:
        customer_id = resolve_customer_id(session)
        return use(session, customer_id)


def _mark_expired(
    open_session: OpenSession,
    action_service: ActionService,
    resolve_customer_id: ResolveCustomerId,
    *,
    conversation_id: str,
    approval_id: str,
) -> None:
    _call(
        open_session,
        resolve_customer_id,
        lambda session, customer_id: action_service.mark_expired(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
        ),
    )


def _mark_failed(
    open_session: OpenSession,
    action_service: ActionService,
    resolve_customer_id: ResolveCustomerId,
    *,
    conversation_id: str,
    approval_id: str,
    failure_code: str,
) -> None:
    _call(
        open_session,
        resolve_customer_id,
        lambda session, customer_id: action_service.mark_failed(
            session,
            customer_id=customer_id,
            conversation_id=conversation_id,
            approval_id=approval_id,
            failure_code=failure_code,
        ),
    )
