from __future__ import annotations

import json
import stat
from dataclasses import asdict

import pytest
from sqlalchemy import select

from app.agent.openai_compatible import AssistantTurn, ModelAPIError, ToolCall
from app.config import Settings
from app.database import Database
from app.models import Inventory, ToolEvent
from app.seed import seed_demo_data
from evals.evidence import (
    ArtifactIntegrityError,
    ObservedChatModel,
    capture_business_state,
    compare_business_states,
    stable_sha256,
    verify_eval_bundle,
    write_eval_bundle,
)


class SuccessfulModel:
    def complete(self, *, messages, tools):
        return AssistantTurn(
            content="ok",
            tool_calls=(),
            finish_reason="stop",
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 3,
                "total_tokens": 14,
            },
            response_id="response-123",
            model="observed-model",
            provider_request_id="provider-request-success-1",
            provider_attempts=2,
        )


class FailingModel:
    def complete(self, *, messages, tools):
        raise ModelAPIError(
            "MODEL_HTTP_ERROR",
            "provider detail containing PRIVATE-PROVIDER-CANARY",
            status_code=503,
            request_id="provider-request-456",
            attempts=2,
        )


def test_stable_sha256_is_canonical_and_detects_changes():
    first = {"b": [2, 3], "a": {"value": 1}}
    reordered = {"a": {"value": 1}, "b": [2, 3]}
    changed = {"a": {"value": 2}, "b": [2, 3]}

    assert stable_sha256(first) == stable_sha256(reordered)
    assert stable_sha256(first) != stable_sha256(changed)
    assert len(stable_sha256(first)) == 64


def test_observed_model_records_usage_latency_and_safe_error_metadata():
    observed = ObservedChatModel(SuccessfulModel())

    turn = observed.complete(
        messages=[{"role": "user", "content": "PRIVATE-INPUT-CANARY"}],
        tools=[],
    )

    assert turn.content == "ok"
    assert len(observed.calls) == 1
    call = observed.calls[0]
    assert call.sequence == 1
    assert call.status == "success"
    assert call.message_count == 1
    assert call.tool_contract_count == 0
    assert call.latency_ms >= 0
    assert call.finish_reason == "stop"
    assert call.response_id == "response-123"
    assert call.observed_model == "observed-model"
    assert call.provider_request_id == "provider-request-success-1"
    assert call.provider_attempts == 2
    assert call.usage["total_tokens"] == 14
    assert "PRIVATE-INPUT-CANARY" not in json.dumps(
        asdict(call),
        ensure_ascii=False,
    )

    failing = ObservedChatModel(FailingModel())
    with pytest.raises(ModelAPIError):
        failing.complete(
            messages=[{"role": "user", "content": "do not persist me"}],
            tools=[],
        )

    failed_call = failing.calls[0]
    assert failed_call.status == "error"
    assert failed_call.error_code == "MODEL_HTTP_ERROR"
    assert failed_call.http_status == 503
    assert failed_call.provider_request_id == "provider-request-456"
    assert failed_call.provider_attempts == 2
    assert "PRIVATE-PROVIDER-CANARY" not in json.dumps(
        asdict(failed_call),
        ensure_ascii=False,
    )


def test_observed_model_preserves_structured_tool_requests_without_messages():
    class ToolCallingModel:
        def complete(self, *, messages, tools):
            return AssistantTurn(
                content=None,
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="get_order",
                        arguments='{"order_id":"ORD-1001"}',
                    ),
                ),
                finish_reason="tool_calls",
                usage={"total_tokens": 9},
                response_id="response-tool-1",
                model="observed-model",
            )

    observed = ObservedChatModel(ToolCallingModel())
    observed.complete(
        messages=[{"role": "user", "content": "PRIVATE-INPUT-CANARY"}],
        tools=[{"name": "get_order", "description": "read", "input_schema": {}}],
    )

    call = observed.calls[0]
    assert len(call.tool_calls) == 1
    assert call.tool_calls[0].tool_call_id == "call-1"
    assert call.tool_calls[0].tool_name == "get_order"
    assert call.tool_calls[0].arguments == '{"order_id":"ORD-1001"}'
    assert "PRIVATE-INPUT-CANARY" not in json.dumps(
        asdict(call),
        ensure_ascii=False,
    )


def test_business_state_snapshot_ignores_runtime_records_and_detects_inventory_change():
    settings = Settings(database_url="sqlite:///:memory:")
    database = Database(settings.database_url)
    database.create_all()
    seed_demo_data(database, settings)

    with database.session() as session:
        before = capture_business_state(session)

        assert "auth_sessions" not in before.tables
        assert "tool_events" not in before.tables

        session.add(
            ToolEvent(
                id="TEV-EVIDENCE-ONLY",
                run_id="eval-run",
                customer_id=None,
                tool_name="evidence_test",
                arguments={},
                result={"ok": True},
                success=True,
                error_code=None,
                latency_ms=1,
            )
        )
        session.flush()
        after_runtime_record = capture_business_state(session)

        assert before.sha256 == after_runtime_record.sha256

        inventory = session.scalar(select(Inventory).limit(1))
        assert inventory is not None
        inventory.available_qty += 1
        session.flush()
        after_inventory_change = capture_business_state(session)

    delta = compare_business_states(before, after_inventory_change)
    assert delta.changed is True
    assert delta.changed_tables == ("inventory",)
    assert delta.before_sha256 == before.sha256
    assert delta.after_sha256 == after_inventory_change.sha256
    database.engine.dispose()


def test_eval_bundle_is_private_integrity_checked_and_never_overwritten(tmp_path):
    secret = "PRIVATE-DEEPSEEK-KEY-CANARY"
    output_root = tmp_path / "eval-runs"
    run_id = "eval-20260729-abcdef12"
    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "purpose": "dev_repeat",
        "status": "completed",
        "model": {
            "provider": "deepseek",
            "requested_model": "deepseek-v4-flash",
            "base_url_host": "api.deepseek.com",
            "deepseek_api_key": secret,
        },
    }
    cases = [
        {
            "case_id": "case-1",
            "trial": 1,
            "passed": False,
            "final_text": f"accidental echo {secret}",
            "authorization": f"Bearer {secret}",
            "scores": {
                "task_success": True,
                "security": False,
                "efficiency": True,
            },
        }
    ]
    summary = {
        "total_trials": 1,
        "strict_passed": 0,
        "security_passed": 0,
        "provider_error": f"must redact {secret}",
    }

    bundle_path = write_eval_bundle(
        output_root=output_root,
        run_id=run_id,
        manifest=manifest,
        case_records=cases,
        summary=summary,
        secret_values=(secret,),
    )

    assert bundle_path == output_root / run_id
    assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o700
    assert {path.name for path in bundle_path.iterdir()} == {
        "manifest.json",
        "cases.jsonl",
        "summary.json",
        "integrity.json",
        "trajectories",
    }
    for path in bundle_path.iterdir():
        if path.is_dir():
            assert path.name == "trajectories"
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
            continue
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert secret not in path.read_text(encoding="utf-8")
    trajectory_path = bundle_path / "trajectories" / "case-1" / "1.json"
    assert trajectory_path.is_file()
    assert stat.S_IMODE(trajectory_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(trajectory_path.stat().st_mode) == 0o600
    assert secret not in trajectory_path.read_text(encoding="utf-8")

    verified = verify_eval_bundle(bundle_path)
    assert verified["manifest"]["run_id"] == run_id
    assert "deepseek_api_key" not in verified["manifest"]["model"]
    assert verified["cases"][0]["final_text"] == "accidental echo [REDACTED]"
    assert verified["summary"]["provider_error"] == "must redact [REDACTED]"
    assert verified["trajectories"][0]["case_id"] == "case-1"

    with pytest.raises(FileExistsError):
        write_eval_bundle(
            output_root=output_root,
            run_id=run_id,
            manifest=manifest,
            case_records=cases,
            summary=summary,
            secret_values=(secret,),
        )

    cases_path = bundle_path / "cases.jsonl"
    cases_path.write_text(
        cases_path.read_text(encoding="utf-8") + "{}\n",
        encoding="utf-8",
    )
    with pytest.raises(ArtifactIntegrityError):
        verify_eval_bundle(bundle_path)


@pytest.mark.parametrize(
    "run_id",
    [
        "../escape",
        "nested/run",
        "/absolute",
        "has spaces",
        "x",
    ],
)
def test_eval_bundle_rejects_unsafe_run_ids(tmp_path, run_id):
    with pytest.raises(ValueError):
        write_eval_bundle(
            output_root=tmp_path,
            run_id=run_id,
            manifest={"schema_version": "1.0", "run_id": run_id},
            case_records=[],
            summary={},
        )
