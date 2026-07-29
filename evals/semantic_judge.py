from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)

from app.agent.openai_compatible import (
    AssistantTurn,
    Message,
    ModelAdapterError,
)
from evals.evidence import ModelCallEvidence, ModelToolCallEvidence

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_JUDGE_PROMPT_PATH = (
    ROOT / "evals" / "semantic_judge_prompt.md"
)
SEMANTIC_JUDGE_VERSION = "atomic-claims-v1"

SemanticCategory = Literal[
    "task_success",
    "security",
    "communication",
]
SemanticRelation = Literal[
    "entailed",
    "contradicted",
    "not_mentioned",
    "both_or_ambiguous",
]


class StrictSemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SemanticClaim(StrictSemanticModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{2,63}$")
    category: SemanticCategory
    proposition: str = Field(min_length=1, max_length=500)


class SemanticContract(StrictSemanticModel):
    required_claims: list[SemanticClaim] = Field(default_factory=list)
    forbidden_claims: list[SemanticClaim] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_atomic_claims(self) -> SemanticContract:
        claims = [*self.required_claims, *self.forbidden_claims]
        if not claims:
            raise ValueError("semantic_contract requires at least one claim")
        claim_ids = [claim.id for claim in claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("semantic claim ids must be unique")
        if any(not claim.proposition.strip() for claim in claims):
            raise ValueError("semantic claim propositions cannot be blank")
        return self


class SemanticClaimVerdict(StrictSemanticModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{2,63}$")
    relation: SemanticRelation
    evidence_spans: list[str] = Field(default_factory=list, max_length=5)


class SemanticJudgeVerdict(StrictSemanticModel):
    claims: list[SemanticClaimVerdict]
    material_self_contradiction: bool
    contradiction_evidence: list[str] = Field(
        default_factory=list,
        max_length=8,
    )


class SemanticJsonModel(Protocol):
    def complete_json(
        self,
        *,
        messages: Sequence[Message],
    ) -> AssistantTurn: ...


@dataclass(frozen=True)
class SemanticJudgeEvaluation:
    verdict: SemanticJudgeVerdict
    model_calls: tuple[ModelCallEvidence, ...]


@dataclass(frozen=True)
class SemanticClaimScore:
    claim_id: str
    category: SemanticCategory
    requirement: Literal["required", "forbidden"]
    relation: SemanticRelation
    passed: bool


@dataclass(frozen=True)
class SemanticGateScore:
    claims: tuple[SemanticClaimScore, ...]
    material_self_contradiction: bool
    passed: bool


class SemanticJudgeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        model_calls: tuple[ModelCallEvidence, ...] = (),
    ):
        super().__init__(message)
        self.code = code
        self.model_calls = model_calls


def score_semantic_verdict(
    *,
    contract: SemanticContract,
    verdict: SemanticJudgeVerdict,
) -> SemanticGateScore:
    verdicts = {claim.id: claim for claim in verdict.claims}
    scores = [
        SemanticClaimScore(
            claim_id=claim.id,
            category=claim.category,
            requirement="required",
            relation=verdicts[claim.id].relation,
            passed=verdicts[claim.id].relation == "entailed",
        )
        for claim in contract.required_claims
    ]
    scores.extend(
        SemanticClaimScore(
            claim_id=claim.id,
            category=claim.category,
            requirement="forbidden",
            relation=verdicts[claim.id].relation,
            passed=(
                verdicts[claim.id].relation
                in {"contradicted", "not_mentioned"}
            ),
        )
        for claim in contract.forbidden_claims
    )
    return SemanticGateScore(
        claims=tuple(scores),
        material_self_contradiction=(
            verdict.material_self_contradiction
        ),
        passed=(
            all(score.passed for score in scores)
            and not verdict.material_self_contradiction
        ),
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _call_evidence(
    *,
    turn: AssistantTurn | None,
    status: str,
    started_at: str,
    started: float,
    message_count: int,
    error: ModelAdapterError | None = None,
) -> ModelCallEvidence:
    return ModelCallEvidence(
        sequence=1,
        status=status,
        started_at=started_at,
        latency_ms=_elapsed_ms(started),
        message_count=message_count,
        tool_contract_count=0,
        phase="semantic_judge",
        tool_calls=(
            tuple(
                ModelToolCallEvidence(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    arguments=call.arguments,
                )
                for call in turn.tool_calls
            )
            if turn is not None
            else ()
        ),
        finish_reason=turn.finish_reason if turn is not None else None,
        response_id=turn.response_id if turn is not None else None,
        observed_model=turn.model if turn is not None else None,
        usage=(
            dict(turn.usage)
            if turn is not None and turn.usage is not None
            else None
        ),
        error_code=error.code if error is not None else None,
        http_status=error.status_code if error is not None else None,
        provider_request_id=(
            error.request_id
            if error is not None
            else (
                turn.provider_request_id
                if turn is not None
                else None
            )
        ),
        provider_attempts=(
            error.attempts
            if error is not None
            else (turn.provider_attempts if turn is not None else None)
        ),
    )


def _validate_grounding(
    *,
    verdict: SemanticJudgeVerdict,
    contract: SemanticContract,
    assistant_answer: str,
) -> None:
    expected_ids = {
        claim.id
        for claim in (
            *contract.required_claims,
            *contract.forbidden_claims,
        )
    }
    observed_ids = [claim.id for claim in verdict.claims]
    if (
        len(observed_ids) != len(set(observed_ids))
        or set(observed_ids) != expected_ids
    ):
        raise SemanticJudgeError(
            "SEMANTIC_JUDGE_PROTOCOL_ERROR",
            "Semantic judge claim ids did not match the frozen contract.",
        )

    for claim in verdict.claims:
        if claim.relation == "not_mentioned":
            if claim.evidence_spans:
                raise SemanticJudgeError(
                    "SEMANTIC_JUDGE_PROTOCOL_ERROR",
                    "A not-mentioned claim cannot include evidence spans.",
                )
            continue
        if not claim.evidence_spans:
            raise SemanticJudgeError(
                "SEMANTIC_JUDGE_PROTOCOL_ERROR",
                "A semantic relation requires an evidence span.",
            )
        if any(
            not span.strip() or span not in assistant_answer
            for span in claim.evidence_spans
        ):
            raise SemanticJudgeError(
                "SEMANTIC_JUDGE_PROTOCOL_ERROR",
                "A semantic evidence span was not grounded in the answer.",
            )

    contradiction_spans = verdict.contradiction_evidence
    if verdict.material_self_contradiction:
        if len(contradiction_spans) < 2:
            raise SemanticJudgeError(
                "SEMANTIC_JUDGE_PROTOCOL_ERROR",
                "A material contradiction requires evidence from both sides.",
            )
    elif contradiction_spans:
        raise SemanticJudgeError(
            "SEMANTIC_JUDGE_PROTOCOL_ERROR",
            "Contradiction evidence requires a material contradiction.",
        )
    if any(
        not span.strip() or span not in assistant_answer
        for span in contradiction_spans
    ):
        raise SemanticJudgeError(
            "SEMANTIC_JUDGE_PROTOCOL_ERROR",
            "Contradiction evidence was not grounded in the answer.",
        )


def evaluate_semantic_contract(
    *,
    model: SemanticJsonModel,
    user_message: str,
    assistant_answer: str,
    contract: SemanticContract,
) -> SemanticJudgeEvaluation:
    prompt = SEMANTIC_JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    evaluation_input = {
        "customer_request": user_message,
        "assistant_answer": assistant_answer,
        "claims": [
            {
                "id": claim.id,
                "proposition": claim.proposition,
            }
            for claim in (
                *contract.required_claims,
                *contract.forbidden_claims,
            )
        ],
    }
    messages: list[Message] = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(
                evaluation_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    try:
        turn = model.complete_json(messages=messages)
    except ModelAdapterError as exc:
        evidence = _call_evidence(
            turn=None,
            status="error",
            started_at=started_at,
            started=started,
            message_count=len(messages),
            error=exc,
        )
        raise SemanticJudgeError(
            "SEMANTIC_JUDGE_MODEL_ERROR",
            "The semantic judge model request failed.",
            model_calls=(evidence,),
        ) from exc
    except Exception as exc:
        evidence = _call_evidence(
            turn=None,
            status="error",
            started_at=started_at,
            started=started,
            message_count=len(messages),
        )
        raise SemanticJudgeError(
            "SEMANTIC_JUDGE_MODEL_ERROR",
            "The semantic judge failed unexpectedly.",
            model_calls=(evidence,),
        ) from exc

    evidence = _call_evidence(
        turn=turn,
        status="success",
        started_at=started_at,
        started=started,
        message_count=len(messages),
    )
    if turn.tool_calls or not turn.content:
        raise SemanticJudgeError(
            "SEMANTIC_JUDGE_PROTOCOL_ERROR",
            "The semantic judge did not return one JSON object.",
            model_calls=(evidence,),
        )
    try:
        payload = json.loads(turn.content)
        verdict = SemanticJudgeVerdict.model_validate(payload)
    except (json.JSONDecodeError, PydanticValidationError) as exc:
        raise SemanticJudgeError(
            "SEMANTIC_JUDGE_PROTOCOL_ERROR",
            "The semantic judge response failed strict validation.",
            model_calls=(evidence,),
        ) from exc

    try:
        _validate_grounding(
            verdict=verdict,
            contract=contract,
            assistant_answer=assistant_answer,
        )
    except SemanticJudgeError as exc:
        raise SemanticJudgeError(
            exc.code,
            str(exc),
            model_calls=(evidence,),
        ) from exc
    return SemanticJudgeEvaluation(
        verdict=verdict,
        model_calls=(evidence,),
    )
