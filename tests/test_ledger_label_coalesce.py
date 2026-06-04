"""장부+시세 라벨 — 예수·총평 이중 합산 방지."""
from __future__ import annotations

from services.ledger_valuation import coalesce_ledger_kis_labels


def test_us_cash_stored_as_total_does_not_double_count():
    state = {"last_us_cash_usd": 10_000.0}
    snap = {"cash": 2_000.0, "total": 10_000.0}
    cash, total = coalesce_ledger_kis_labels(
        "US",
        state,
        snap,
        holdings_current=8_000.0,
        cash_guess=10_000.0,
        total_guess=18_000.0,
    )
    assert cash == 2_000.0
    assert total == 10_000.0


def test_kr_prefers_snapshot_cash_over_state_total_mistake():
    state = {"last_kr_cash_krw": 5_000_000.0}
    snap = {"cash": 1_000_000.0, "total": 5_000_000.0}
    cash, total = coalesce_ledger_kis_labels(
        "KR",
        state,
        snap,
        holdings_current=4_000_000.0,
        cash_guess=5_000_000.0,
        total_guess=9_000_000.0,
    )
    assert cash == 1_000_000.0
    assert total == 5_000_000.0
