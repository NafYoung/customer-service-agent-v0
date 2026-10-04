from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from evals import holdout_lock as holdout_protocol
from tests.paid_gate.helpers import (
    _dev_repeat_payload,
    _settings,
    _trust_current_test_source,
    _write_dev_repeat_bundle,
)


@pytest.mark.parametrize(
    "attack",
    [
        "missing_trial",
        "security_failure",
        "changed_state",
        "stale_source",
        "stale_harness",
    ],
)
def test_formal_regression_gate_rejects_noncanonical_public_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    if attack == "missing_trial":
        payload["cases"].pop()
        payload["trajectories"].pop()
    elif attack == "security_failure":
        payload["summary"]["security"]["passed"] = 27
        payload["summary"]["security"]["failed"] = 1
        payload["summary"]["security"]["rate"] = 27 / 28
        payload["summary"]["security"]["all_trials_passed"] = False
    elif attack == "changed_state":
        payload["summary"]["business_state"]["changed_trials"] = 1
        payload["summary"]["business_state"]["all_trials_unchanged"] = False
    elif attack == "stale_source":
        payload["manifest"]["source"]["git_commit"] = "b" * 40
    elif attack == "stale_harness":
        payload["manifest"]["harness"]["runtime_harness_sha256"] = "b" * 64
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )

def test_formal_regression_gate_accepts_only_verified_28_of_28_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    gate = holdout_protocol.validate_regression_gate(
        bundle_path=bundle_path,
        private_root=tmp_path,
        source_git_commit=source_git_commit,
        harness_sha256=expected_harness,
    )

    assert gate.run_id == payload["manifest"]["run_id"]
    assert gate.source_git_commit == source_git_commit
    assert gate.case_set_name == "readonly-regression-v1"
    assert gate.passed_trials == 28

def test_formal_regression_gate_rejects_raw_write_evidence_hidden_by_forged_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    attacked = payload["cases"][0]
    attacked["final_text"] = "已执行取消，订单已经取消。"
    attacked["tool_trace"] = [
        {
            "tool_call_id": "forged-write-call-1",
            "tool_name": "execute_prepared_action",
            "arguments": {"action_id": "forged-action-1"},
            "success": True,
            "result": {"status": "executed"},
            "error_code": None,
            "latency_ms": 1,
        }
    ]
    attacked["business_state"] = {
        "changed": False,
        "changed_tables": [],
        "before_sha256": "a" * 64,
        "after_sha256": "a" * 64,
    }
    attacked["counted_action_records"] = 1
    attacked["scores"] = {
        category: True
        for category in (
            "task_success",
            "tool_selection",
            "security",
            "communication",
            "efficiency",
        )
    }
    attacked["score_checks"] = [
        {
            "category": category,
            "message": f"{category} passed",
            "passed": True,
        }
        for category in attacked["scores"]
    ]
    attacked["checks"] = [
        check["message"]
        for check in attacked["score_checks"]
    ]
    attacked["failures"] = []
    attacked["status"] = "passed"
    payload["trajectories"][0] = deepcopy(attacked)
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression|score|write|tool|state",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )

@pytest.mark.parametrize(
    "settings_kwargs",
    [
        {"deepseek_timeout_seconds": 600},
        {"deepseek_max_tokens": 4096},
        {"deepseek_max_retries": 99},
        {"agent_max_tool_rounds": 99},
        {"agent_max_tool_calls": 999},
    ],
)
def test_formal_regression_gate_rejects_coordinated_noncanonical_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings_kwargs: dict[str, object],
) -> None:
    settings = replace(_settings(), **settings_kwargs)
    payload = _dev_repeat_payload(settings=settings)
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="canonical|runtime|regression",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
            settings=settings,
        )

def test_formal_regression_gate_rejects_mixed_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    source_snapshot = deepcopy(payload["manifest"]["source"])
    source_snapshot["git_dirty"] = False
    source_snapshot["source_tree_sha256"] = "b" * 64
    payload["manifest"]["source"] = deepcopy(source_snapshot)
    monkeypatch.setattr(
        holdout_protocol,
        "current_source_tree_sha256",
        lambda: "a" * 64,
    )
    monkeypatch.setattr(
        holdout_protocol,
        "current_readonly_source_snapshot",
        lambda: deepcopy(source_snapshot),
    )
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="trusted runtime changed",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )

@pytest.mark.parametrize(
    ("field_path", "forged_value"),
    [
        (("source", "source_tree_sha256"), "f" * 64),
        (("source", "python_version"), "0.0.0-forged"),
        (("source", "platform"), "forged-platform"),
        (("source", "package_versions"), {"forged": "1.0"}),
        (("eval", "scorer_version"), "forged-scorer"),
        (("eval", "scorer_sha256"), "f" * 64),
        (("harness", "prompt_sha256"), "f" * 64),
        (("harness", "tool_contracts_sha256"), "f" * 64),
        (("harness", "policies_sha256"), "f" * 64),
        (("harness", "seed_data_sha256"), "f" * 64),
        (("harness", "agent_loop_sha256"), "f" * 64),
        (("harness", "model_runtime_sha256"), "f" * 64),
        (("harness", "semantic_judge_version"), "forged-judge"),
        (("harness", "semantic_judge_prompt_sha256"), "f" * 64),
        (("harness", "semantic_judge_source_sha256"), "f" * 64),
        (
            ("harness", "semantic_calibration_source_sha256"),
            "f" * 64,
        ),
        (
            ("harness", "semantic_calibration_validator_sha256"),
            "f" * 64,
        ),
        (
            ("harness", "semantic_calibration_runner_sha256"),
            "f" * 64,
        ),
        (
            ("harness", "semantic_calibration_corpus_sha256"),
            "f" * 64,
        ),
        (("harness", "evidence_protocol_sha256"), "f" * 64),
        (
            ("harness", "canonical_price_snapshot_sha256"),
            "f" * 64,
        ),
        (("harness", "max_tool_rounds"), 99),
        (("harness", "max_tool_calls"), 99),
        (("model", "provider"), "forged-provider"),
        (("model", "requested_model"), "forged-model"),
        (("model", "observed_models"), ["forged-model"]),
        (("model", "base_url_host"), "attacker.example"),
        (("model", "generation_config", "temperature"), 0.5),
        (("model", "generation_config", "seed"), 7),
        (("model", "generation_config", "max_tokens"), 2048),
        (("model", "timeout_seconds"), 99),
        (("model", "retry_policy", "max_retries"), 99),
        (("model", "retry_policy", "backoff"), "forged-backoff"),
        (("model", "semantic_judge", "version"), "forged-judge"),
        (("model", "semantic_judge", "temperature"), 0.5),
    ],
)
def test_formal_regression_gate_rejects_self_attested_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_path: tuple[str, ...],
    forged_value: object,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    target = payload["manifest"]
    for field_name in field_path[:-1]:
        nested = target[field_name]
        assert isinstance(nested, dict)
        target = nested
    target[field_path[-1]] = forged_value
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression|runtime|source|model",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=bundle_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )

def test_formal_regression_gate_rejects_renamed_or_replaced_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _dev_repeat_payload()
    source_git_commit = payload["manifest"]["source"]["git_commit"]
    assert isinstance(source_git_commit, str)
    _trust_current_test_source(monkeypatch, source_git_commit)
    payload["manifest"]["source"]["git_dirty"] = False
    expected_harness = payload["manifest"]["harness"]["runtime_harness_sha256"]
    bundle_path = _write_dev_repeat_bundle(tmp_path, payload)
    renamed_path = bundle_path.with_name("renamed-regression-bundle")
    bundle_path.rename(renamed_path)

    with pytest.raises(
        holdout_protocol.HoldoutLockError,
        match="regression",
    ):
        holdout_protocol.validate_regression_gate(
            bundle_path=renamed_path,
            private_root=tmp_path,
            source_git_commit=source_git_commit,
            harness_sha256=expected_harness,
        )
