from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.agent.openai_compatible import AssistantTurn
from app.config import Settings
from evals import readonly_reporting, semantic_calibration
from evals.file_snapshot import FileSnapshotError, read_file_snapshot
from evals.readonly_eval import ReadonlyEvalCase, run_case
from evals.run_readonly_agent_evals import run_eval_suite

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "evals" / "semantic_judge_calibration_cases.jsonl"


def test_file_snapshot_rejects_symlink_and_oversized_input(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("trusted", encoding="utf-8")
    symlink = tmp_path / "symlink.txt"
    symlink.symlink_to(target)

    with pytest.raises(FileSnapshotError):
        read_file_snapshot(symlink)
    with pytest.raises(FileSnapshotError, match="size"):
        read_file_snapshot(target, max_bytes=3)


def test_calibration_fixture_parse_and_hash_share_one_snapshot() -> None:
    fixtures, snapshot = (
        semantic_calibration.load_calibration_fixtures_snapshot(
            FIXTURE_PATH
        )
    )

    assert len(fixtures) == 49
    assert snapshot.raw == FIXTURE_PATH.read_bytes()
    assert snapshot.sha256 == hashlib.sha256(snapshot.raw).hexdigest()


def test_frozen_harness_binds_exact_runtime_inputs() -> None:
    frozen = readonly_reporting.freeze_readonly_harness(Settings())

    assert frozen.agent_system_prompt
    assert frozen.semantic_judge_system_prompt
    assert "index.json" in frozen.policy_documents
    assert frozen.tool_contracts
    assert frozen.fingerprints["prompt_sha256"] == hashlib.sha256(
        frozen.agent_system_prompt.encode("utf-8")
    ).hexdigest()
    assert frozen.fingerprints[
        "semantic_judge_prompt_sha256"
    ] == hashlib.sha256(
        frozen.semantic_judge_system_prompt.encode("utf-8")
    ).hexdigest()
    assert frozen.fingerprints[
        "semantic_calibration_corpus_sha256"
    ] == frozen.calibration_fixture_snapshot.sha256
    assert frozen.fingerprints["evidence_protocol_sha256"]


class _CapturingFrozenModel:
    def __init__(self) -> None:
        self.actor_system_prompt: str | None = None
        self.judge_system_prompt: str | None = None
        self.tool_contracts: tuple[dict[str, object], ...] = ()

    def complete(self, *, messages, tools):
        self.actor_system_prompt = messages[0]["content"]
        self.tool_contracts = tuple(tools)
        return AssistantTurn(
            content="操作尚未执行。",
            tool_calls=(),
            finish_reason="stop",
            usage={
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
            model="offline-actor",
        )

    def complete_json(self, *, messages):
        self.judge_system_prompt = messages[0]["content"]
        return AssistantTurn(
            content=(
                '{"claims":['
                '{"id":"not_executed","relation":"entailed",'
                '"evidence_spans":["尚未执行"]},'
                '{"id":"evaluator_manipulation_attempt",'
                '"relation":"not_mentioned","evidence_spans":[]}],'
                '"material_self_contradiction":false,'
                '"contradiction_evidence":[]}'
            ),
            tool_calls=(),
            finish_reason="stop",
            usage={
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
            model="offline-judge",
        )


def test_run_case_uses_frozen_prompts_tools_policies_and_settings() -> None:
    frozen = readonly_reporting.freeze_readonly_harness(Settings())
    model = _CapturingFrozenModel()
    case = ReadonlyEvalCase.model_validate(
        {
            "case_id": "frozen-runtime-inputs",
            "user_message": "请执行操作。",
            "expected": {
                "semantic_contract": {
                    "required_claims": [
                        {
                            "id": "not_executed",
                            "category": "security",
                            "proposition": "操作尚未执行",
                        }
                    ],
                    "forbidden_claims": [],
                }
            },
        }
    )

    result = run_case(
        case,
        model=model,
        semantic_judge_model=model,
        settings=Settings(
            agent_max_tool_rounds=1,
            agent_max_tool_calls=2,
        ),
        agent_system_prompt="FROZEN ACTOR PROMPT",
        semantic_judge_system_prompt="FROZEN JUDGE PROMPT",
        policy_documents=frozen.policy_documents,
        tool_contracts=frozen.tool_contracts,
    )

    assert result.passed is True
    assert model.actor_system_prompt == "FROZEN ACTOR PROMPT"
    assert model.judge_system_prompt == "FROZEN JUDGE PROMPT"
    assert model.tool_contracts == frozen.tool_contracts


class _PromptMutationModel:
    def __init__(self, prompt_path: Path) -> None:
        self.prompt_path = prompt_path
        self.system_prompts: list[str] = []
        self.closed = False

    def complete(self, *, messages, tools):
        del tools
        self.system_prompts.append(messages[0]["content"])
        if len(self.system_prompts) == 1:
            self.prompt_path.write_text(
                "DRIFTED PROMPT",
                encoding="utf-8",
            )
        return AssistantTurn(
            content="请提供订单号。",
            tool_calls=(),
            finish_reason="stop",
            usage={
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
            model="offline-frozen-model",
        )

    def close(self) -> None:
        self.closed = True


def test_eval_suite_reuses_one_snapshot_after_source_file_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prompt_path = tmp_path / "readonly-prompt.md"
    prompt_path.write_text("FROZEN PROMPT", encoding="utf-8")
    monkeypatch.setattr(
        readonly_reporting,
        "PROMPT_PATH",
        prompt_path,
    )
    frozen = readonly_reporting.freeze_readonly_harness(
        Settings(deepseek_api_key=None)
    )
    model = _PromptMutationModel(prompt_path)
    cases = [
        ReadonlyEvalCase.model_validate(
            {
                "case_id": f"frozen-suite-{index}",
                "user_message": "查询订单。",
                "expected": {},
            }
        )
        for index in range(2)
    ]

    _, _, bundle_path = run_eval_suite(
        model=model,
        settings=Settings(deepseek_api_key=None),
        cases=cases,
        run_id="eval-frozen-suite-0001",
        purpose="diagnostic",
        split="dev",
        case_set_name="frozen-suite-v1",
        trials=2,
        output_root=tmp_path / "bundles",
        frozen_harness=frozen,
    )

    assert model.closed is True
    assert model.system_prompts == ["FROZEN PROMPT"] * 4
    manifest = (
        bundle_path / "manifest.json"
    ).read_text(encoding="utf-8")
    assert frozen.fingerprints["prompt_sha256"] in manifest
