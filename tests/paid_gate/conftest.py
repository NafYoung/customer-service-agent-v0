from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stub_paid_ledger_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _noop(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "evals.paid_ledger_binding.require_persistent_budget_matches_trusted_ledger",
        _noop,
    )
