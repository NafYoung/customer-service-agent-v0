from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import MethodType

import httpx
import pytest

from app.agent.factory import build_deepseek_client
from evals import run_readonly_agent_evals as runner
from tests.paid_gate.helpers import (
    _assert_zero_budget_attempts,
    _CountingModel,
    _formal_budget_guard,
    _formal_runtime_inputs,
    _issue_formal_execution_capability,
    _run_formal_runtime_attack,
    _settings,
)


def test_formal_execution_capability_rejects_actor_model_replacement_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-bound-actor",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    replacement_model = _CountingModel()
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=replacement_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert replacement_model.calls == 0
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()

def test_formal_model_public_runtime_config_is_canonical_and_credential_free(
    tmp_path: Path,
) -> None:
    synthetic_key = "test-only-secret-canary"
    settings = replace(
        _settings(),
        deepseek_api_key=synthetic_key,
    )
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-public-config",
        settings=settings,
    )
    model = runner.build_deepseek_client(
        settings,
        budget_guard=guard,
    )
    try:
        public_config = model.public_runtime_config()
        assert public_config == runner.deepseek_public_runtime_config(settings)
        assert synthetic_key not in repr(public_config)
        assert all("key" not in field.casefold() for field in public_config)
        _assert_zero_budget_attempts(guard)
    finally:
        model.close()

def test_formal_execution_capability_rejects_instance_method_override_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-instance-method-override",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    forged_calls = 0

    def forged_complete(*, messages, tools):
        del messages, tools
        nonlocal forged_calls
        forged_calls += 1
        raise AssertionError("instance override reached a model-call boundary")

    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        bound_model.__dict__["complete"] = forged_complete
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert forged_calls == 0
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.__dict__.pop("complete", None)
        bound_model.close()

def test_formal_execution_capability_rejects_class_method_override_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-class-method-override",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    capability = _issue_formal_execution_capability(
        runtime=runtime,
        model=bound_model,
        semantic_judge_model=bound_model,
        budget_guard=guard,
        budget_report_provider=report_provider,
    )
    forged_calls = 0

    def forged_complete(self, *, messages, tools):
        del self, messages, tools
        nonlocal forged_calls
        forged_calls += 1
        raise AssertionError("class override reached a model-call boundary")

    monkeypatch.setattr(
        type(bound_model),
        "complete",
        forged_complete,
    )
    try:
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert forged_calls == 0
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()

def test_formal_execution_capability_rejects_guard_method_override_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-guard-method-override",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    capability = _issue_formal_execution_capability(
        runtime=runtime,
        model=bound_model,
        semantic_judge_model=bound_model,
        budget_guard=guard,
        budget_report_provider=report_provider,
    )
    forged_calls = 0

    def forged_reserve_attempt(*, logical_call_id, attempt_number):
        del logical_call_id, attempt_number
        nonlocal forged_calls
        forged_calls += 1
        raise AssertionError("guard override reached a paid-call boundary")

    guard.__dict__["reserve_attempt"] = forged_reserve_attempt
    try:
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert forged_calls == 0
        _assert_zero_budget_attempts(guard)
    finally:
        guard.__dict__.pop("reserve_attempt", None)
        bound_model.close()

def test_formal_execution_capability_rejects_judge_model_replacement_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-bound-judge",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    replacement_judge = _CountingModel()
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=replacement_judge,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert replacement_judge.calls == 0
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()

@pytest.mark.parametrize("entity", ["prompt", "policy", "tool"])
def test_formal_execution_capability_rejects_harness_entity_replacement_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entity: str,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-bound-{entity}",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    if entity == "prompt":
        replacement_harness = replace(
            runtime.frozen_harness,
            agent_system_prompt=(
                runtime.frozen_harness.agent_system_prompt + "\nforged prompt"
            ),
        )
    elif entity == "policy":
        replacement_policies = dict(runtime.frozen_harness.policy_documents)
        policy_name = next(iter(replacement_policies))
        replacement_policies[policy_name] += "\nforged policy"
        replacement_harness = replace(
            runtime.frozen_harness,
            policy_documents=replacement_policies,
        )
    else:
        replacement_tools = deepcopy(runtime.frozen_harness.tool_contracts)
        replacement_tools[0]["description"] += " forged tool"
        replacement_harness = replace(
            runtime.frozen_harness,
            tool_contracts=tuple(replacement_tools),
        )
    assert replacement_harness.fingerprints == runtime.frozen_harness.fingerprints
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=replacement_harness,
                capability=capability,
            )
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()

def test_formal_execution_capability_refreezes_harness_before_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-refreeze",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    capability = _issue_formal_execution_capability(
        runtime=runtime,
        model=bound_model,
        semantic_judge_model=bound_model,
        budget_guard=guard,
        budget_report_provider=report_provider,
    )
    changed_canonical_harness = replace(
        runtime.frozen_harness,
        agent_system_prompt=(
            runtime.frozen_harness.agent_system_prompt + "\nchanged canonical input"
        ),
    )
    monkeypatch.setattr(
        runner,
        "freeze_readonly_harness",
        lambda settings: changed_canonical_harness,
    )
    try:
        with pytest.raises(ValueError, match="runtime identity"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()

@pytest.mark.parametrize(
    "runtime_object",
    ["budget_guard", "report_provider", "capability", "model_config"],
)
def test_formal_execution_capability_rejects_runtime_object_replacement_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_object: str,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-bound-{runtime_object}",
        settings=runtime.settings,
    )
    replacement_guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-replacement-{runtime_object}",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    capability = None
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        attacked_provider = report_provider
        attacked_capability = capability
        if runtime_object == "budget_guard":
            bound_model._budget_guard = replacement_guard
        elif runtime_object == "report_provider":
            attacked_provider = replacement_guard.snapshot
        elif runtime_object == "model_config":
            bound_model._model = "forged-model"
        else:
            attacked_capability = replace(
                capability,
                budget_guard=replacement_guard,
            )
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=attacked_provider,
                frozen_harness=runtime.frozen_harness,
                capability=attacked_capability,
            )
        _assert_zero_budget_attempts(guard)
        _assert_zero_budget_attempts(replacement_guard)
    finally:
        bound_model._model = runtime.settings.deepseek_model
        bound_model._budget_guard = guard
        bound_model.close()
        replacement_guard.close()

def test_formal_execution_capability_rejects_post_issue_httpx_client_swap_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-client-swap",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    mock_hits = 0

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal mock_hits
        mock_hits += 1
        return httpx.Response(200, json={"choices": []})

    sealed_client = bound_model._client
    swapped_client = httpx.Client(
        transport=httpx.MockTransport(_mock_handler),
    )
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        bound_model._client = swapped_client
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert mock_hits == 0
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model._client = sealed_client
        swapped_client.close()
        bound_model.close()

def test_formal_execution_capability_rejects_sibling_http_transport_swap_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-sibling-transport",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    sealed_transport = bound_model._client._transport
    sibling_transport = httpx.HTTPTransport()
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        bound_model._client._transport = sibling_transport
        assert bound_model.live_transport_mode() == "default"
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model._client._transport = sealed_transport
        sibling_transport.close()
        bound_model.close()

@pytest.mark.parametrize("method_name", ["send", "post", "request"])
def test_formal_execution_capability_rejects_client_method_shadow_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-client-{method_name}-shadow",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    mock_hits = 0

    def _shadowed(self: httpx.Client, *args: object, **kwargs: object) -> object:
        del self, args, kwargs
        nonlocal mock_hits
        mock_hits += 1
        return httpx.Response(200, json={"choices": []})

    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        setattr(
            bound_model._client,
            method_name,
            MethodType(_shadowed, bound_model._client),
        )
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert mock_hits == 0
        _assert_zero_budget_attempts(guard)
    finally:
        bound_model.close()

def test_formal_execution_capability_rejects_mounts_mock_injection_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-mounts-inject",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    mock_hits = 0

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal mock_hits
        mock_hits += 1
        return httpx.Response(200, json={"choices": []})

    mounts = bound_model._client._mounts
    # httpx>=0.28 default clients often start with empty mounts; seed a
    # pattern key so the post-seal MockTransport remount remains a real attack.
    pattern_donor = httpx.Client(mounts={"all://": httpx.HTTPTransport()})
    try:
        mount_key = next(iter(pattern_donor._mounts))
    finally:
        pattern_donor.close()
    had_existing_mount = mount_key in mounts
    original_mount_transport = mounts.get(mount_key)
    injected = httpx.MockTransport(_mock_handler)
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        mounts[mount_key] = injected
        assert type(bound_model._client._transport) is httpx.HTTPTransport
        assert bound_model.live_transport_mode() == "default"
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        assert mock_hits == 0
        _assert_zero_budget_attempts(guard)
    finally:
        if had_existing_mount:
            mounts[mount_key] = original_mount_transport
        else:
            mounts.pop(mount_key, None)
        bound_model.close()

def test_formal_execution_capability_rejects_transport_mode_lie_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id="eval-20260729-formal-transport-lie",
        settings=runtime.settings,
    )
    mock_hits = 0

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal mock_hits
        mock_hits += 1
        return httpx.Response(200, json={"choices": []})

    lied_model = build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
        transport=httpx.MockTransport(_mock_handler),
    )
    report_provider = guard.snapshot
    try:
        lied_model._transport_mode = "default"
        assert lied_model.live_transport_mode() == "custom"
        with pytest.raises(ValueError, match="formal execution capability"):
            _issue_formal_execution_capability(
                runtime=runtime,
                model=lied_model,
                semantic_judge_model=lied_model,
                budget_guard=guard,
                budget_report_provider=report_provider,
            )
        assert mock_hits == 0
        _assert_zero_budget_attempts(guard)
    finally:
        lied_model.close()

@pytest.mark.parametrize("rebinding", ["ledger", "price_snapshot"])
def test_formal_execution_capability_rejects_budget_graph_rebinding_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rebinding: str,
) -> None:
    runtime = _formal_runtime_inputs(tmp_path, monkeypatch)
    guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-budget-graph-{rebinding}",
        settings=runtime.settings,
    )
    replacement_guard = _formal_budget_guard(
        tmp_path,
        run_id=f"eval-20260729-formal-budget-graph-replacement-{rebinding}",
        settings=runtime.settings,
    )
    bound_model = runner.build_deepseek_client(
        runtime.settings,
        budget_guard=guard,
    )
    report_provider = guard.snapshot
    original_ledger = guard._ledger
    original_price = guard._price_snapshot
    try:
        capability = _issue_formal_execution_capability(
            runtime=runtime,
            model=bound_model,
            semantic_judge_model=bound_model,
            budget_guard=guard,
            budget_report_provider=report_provider,
        )
        if rebinding == "ledger":
            guard._ledger = replacement_guard._ledger
        else:
            guard._price_snapshot = replacement_guard._price_snapshot
        with pytest.raises(ValueError, match="formal execution capability"):
            _run_formal_runtime_attack(
                runtime=runtime,
                model=bound_model,
                semantic_judge_model=bound_model,
                budget_report_provider=report_provider,
                frozen_harness=runtime.frozen_harness,
                capability=capability,
            )
        guard._ledger = original_ledger
        guard._price_snapshot = original_price
        _assert_zero_budget_attempts(guard)
        _assert_zero_budget_attempts(replacement_guard)
    finally:
        guard._ledger = original_ledger
        guard._price_snapshot = original_price
        bound_model.close()
        replacement_guard.close()
