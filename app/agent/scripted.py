"""Deterministic ChatModel used by tests and public_demo preparation_scripted."""

from __future__ import annotations

from copy import deepcopy
from typing import Sequence

from app.agent.openai_compatible import (
    AssistantTurn,
    Message,
    ToolCall,
    ToolContract,
)


class ScriptedModel:
    """Pop pre-authored assistant turns; record each complete() call."""

    def __init__(self, *turns: AssistantTurn):
        self.turns = list(turns)
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolContract],
    ) -> AssistantTurn:
        self.calls.append(
            {
                "messages": deepcopy(list(messages)),
                "tools": deepcopy(list(tools)),
            }
        )
        if not self.turns:
            raise AssertionError("scripted model ran out of turns")
        return self.turns.pop(0)


def tool_turn(
    name: str,
    arguments: str,
    *,
    call_id: str = "call-prepare-1",
) -> AssistantTurn:
    return AssistantTurn(
        content=None,
        tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),),
        finish_reason="tool_calls",
        usage=None,
    )


def final_turn(content: str) -> AssistantTurn:
    return AssistantTurn(
        content=content,
        tool_calls=(),
        finish_reason="stop",
        usage=None,
    )
