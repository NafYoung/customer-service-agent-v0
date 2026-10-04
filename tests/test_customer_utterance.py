from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.readonly import exchange_request_missing_target_size
from app.config import Settings
from app.demo import (
    APP_MODE_PUBLIC_DEMO,
    DEMO_AGENT_MODE_OFFLINE_REPLAY,
    DEMO_AGENT_MODE_PREPARATION_SCRIPTED,
)
from app.demo.replay import match_offline_replay
from app.main import create_app
from app.utterance import parse_customer_utterance

ORIGIN = "http://testserver"
HOST_TOKEN = "demo-utterance-host-secret-must-stay-server-side"
SIZE_43 = "ORD-1003 换货 尺码43"
MISSING_SIZE = "ORD-1003 的 ITEM-1003-A 尺码不合适，想换货。"
LABELED_OVER_ORIGINAL = (
    "ORD-1003 的 ITEM-1003-A 是 42 码，想换成 44 码。"
)


def test_size_43_sentence_has_order_size_and_exchange_intent():
    parsed = parse_customer_utterance(SIZE_43)
    assert parsed.order_id == "ORD-1003"
    assert parsed.target_size == "43"
    assert parsed.intent == "exchange"
    assert parsed.has_target_size is True
    assert exchange_request_missing_target_size(SIZE_43) is False


def test_unsuitable_size_wording_still_has_no_target_size():
    parsed = parse_customer_utterance(MISSING_SIZE)
    assert parsed.order_id == "ORD-1003"
    assert parsed.target_size is None
    assert parsed.has_target_size is False
    assert parsed.intent == "exchange"
    assert exchange_request_missing_target_size(MISSING_SIZE) is True
    longer = (
        "ORD-1003 的 ITEM-1003-A 只在室内试穿、没有质量问题，"
        "尺码不合适，想换货。"
    )
    assert exchange_request_missing_target_size(longer) is True


def test_labeled_target_wins_over_the_original_size():
    parsed = parse_customer_utterance(LABELED_OVER_ORIGINAL)
    assert parsed.target_size == "44"
    assert parsed.has_target_size is True
    assert exchange_request_missing_target_size(LABELED_OVER_ORIGINAL) is False


@pytest.mark.parametrize(
    ("text", "order_id", "target_size", "intent", "has_target_size"),
    [
        ("把 ORD-1003 换成 43 码", "ORD-1003", "43", "exchange", True),
        ("取消订单 ORD-1001", "ORD-1001", None, "cancel", False),
        ("我想退货", None, None, "return", False),
        ("换成十", None, None, "exchange", True),
        ("42码", None, "42", None, True),
        ("你好", None, None, None, False),
    ],
)
def test_parse_customer_utterance_reads_one_sentence(
    text: str,
    order_id: str | None,
    target_size: str | None,
    intent: str | None,
    has_target_size: bool,
):
    parsed = parse_customer_utterance(text)
    assert parsed.order_id == order_id
    assert parsed.target_size == target_size
    assert parsed.intent == intent
    assert parsed.has_target_size is has_target_size


def test_offline_match_uses_the_shared_size():
    match = match_offline_replay(SIZE_43)
    assert match is not None
    assert match.kind == "exchange"
    assert match.target_size == "43"
    assert match_offline_replay("我想换货") is None


def test_app_size_and_order_patterns_live_in_utterance_only():
    app_root = Path(__file__).resolve().parents[1] / "app"
    call = re.compile(r"re\.(?:compile|search|findall|match|sub)\((.*?)\)", re.S)
    needles = ("尺码", "码", "ORD")
    offenders: list[str] = []
    for path in sorted(app_root.rglob("*.py")):
        if path.name == "utterance.py":
            continue
        text = path.read_text(encoding="utf-8")
        for found in call.finditer(text):
            if any(needle in found.group(1) for needle in needles):
                offenders.append(path.relative_to(app_root.parent).as_posix())
    assert offenders == []
    owner = (app_root / "utterance.py").read_text(encoding="utf-8")
    assert "尺码" in owner
    assert "ORD-" in owner


@pytest.fixture()
def demo_client(request: pytest.FixtureRequest):
    mode = request.param
    app = create_app(
        settings=Settings(
            app_mode=APP_MODE_PUBLIC_DEMO,
            demo_agent_mode=mode,
            demo_allowed_origin=ORIGIN,
            demo_cookie_secure=False,
            host_confirmation_token=HOST_TOKEN,
            deepseek_api_key="sk-should-never-leak-or-be-used",
            enable_debug_routes=False,
        ),
        seed_demo=False,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        yield client


def _headers(csrf: str | None = None) -> dict[str, str]:
    headers = {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return headers


@pytest.mark.parametrize(
    "demo_client",
    [DEMO_AGENT_MODE_PREPARATION_SCRIPTED, DEMO_AGENT_MODE_OFFLINE_REPLAY],
    indirect=True,
)
def test_size_43_sentence_prepares_exchange_in_both_demo_modes(
    demo_client: TestClient,
):
    session = demo_client.post("/demo/session", headers=_headers(), json={})
    assert session.status_code == 200, session.text
    csrf = session.json()["csrf_token"]
    message = demo_client.post(
        "/demo/messages",
        headers=_headers(csrf),
        json={"message": SIZE_43},
    )
    assert message.status_code == 200, message.text
    body = message.json()
    assert body["has_pending_action"] is True
    assert "DEMO_PREPARE_MISSING" not in message.text
    card = demo_client.get(
        "/demo/pending-action",
        headers={"Origin": ORIGIN, "Accept": "application/json"},
    )
    assert card.status_code == 200, card.text
    assert card.json()["action_type"] == "EXCHANGE_ITEM"
    assert card.json()["target_size"] == "43"


@pytest.mark.parametrize(
    "demo_client",
    [DEMO_AGENT_MODE_PREPARATION_SCRIPTED, DEMO_AGENT_MODE_OFFLINE_REPLAY],
    indirect=True,
)
def test_missing_size_exchange_still_clarifies(
    demo_client: TestClient,
):
    session = demo_client.post("/demo/session", headers=_headers(), json={})
    assert session.status_code == 200, session.text
    csrf = session.json()["csrf_token"]
    message = demo_client.post(
        "/demo/messages",
        headers=_headers(csrf),
        json={"message": "我想换货"},
    )
    assert message.status_code == 200, message.text
    body = message.json()
    assert body["has_pending_action"] is False
    assert "尺码" in body["reply"]
