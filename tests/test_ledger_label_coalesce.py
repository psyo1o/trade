"""장부+시세 라벨 — 예수·총평 이중 합산 방지."""
from __future__ import annotations

from services.ledger_valuation import (
    coalesce_ledger_kis_labels,
    display_cash_from_state,
    write_kis_display_snapshot_part,
)


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


def test_write_snapshot_part_preserves_roi_on_cash_only_update():
    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 1, "total": 2, "roi": 3.5},
        }
    }
    write_kis_display_snapshot_part(state, "KR", cash=817_417, total=1_579_644)
    assert state["last_kis_display_snapshot"]["kr"]["roi"] == 3.5
    assert state["last_kis_display_snapshot"]["kr"]["cash"] == 817_417


def test_display_cash_reads_snapshot_when_valid():
    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 817_417, "total": 1_579_417},
        },
        "last_kr_cash_krw": 999.0,
    }
    assert display_cash_from_state(state, "KR") == 817_417.0


def test_kr_prefers_last_kr_cash_when_snapshot_is_stale_total_blob():
    """장중 persist(81만) + 옛 스냅(예수=총평 158만) — 미장 시간대에도 81만 표시."""
    state = {
        "last_kr_cash_krw": 817_417.0,
        "last_kis_display_snapshot": {
            "kr": {"cash": 1_579_644.0, "total": 1_579_644.0},
        },
    }
    snap = {"cash": 1_579_644.0, "total": 1_579_644.0}
    cash, total = coalesce_ledger_kis_labels(
        "KR",
        state,
        snap,
        holdings_current=762_000.0,
        cash_guess=817_417.0,
        total_guess=1_579_417.0,
    )
    assert cash == 817_417.0
    assert total == int(round(817_417.0 + 762_000.0))


def test_write_snapshot_skips_us_downgrade_after_force_refresh():
    """강제 새로고침 직후 잔고 API가 옛 예수($37)를 주어도 스냅샷을 덮어쓰지 않는다."""
    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 895.08, "total": 1434.53},
        },
        "last_us_cash_usd": 895.08,
    }
    write_kis_display_snapshot_part(
        state,
        "US",
        cash=37.91,
        total=576.69,
    )
    us = state["last_kis_display_snapshot"]["us"]
    assert float(us["cash"]) == 895.08
    assert float(us["total"]) == 1434.53


def test_coalesce_after_full_liquidation_uses_snapshot_total():
    """전량 매도 후 보유 0 — 예수는 스냅샷 총평(매도 대금 반영)으로."""
    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 800_000, "total": 1_500_000},
        },
        "last_kr_cash_krw": 800_000.0,
    }
    cash, total = coalesce_ledger_kis_labels(
        "KR",
        state,
        state["last_kis_display_snapshot"]["kr"],
        holdings_current=0.0,
        cash_guess=800_000.0,
        total_guess=800_000.0,
    )
    assert cash == 1_500_000.0
    assert total == 1_500_000.0


def test_coalesce_after_buy_stale_cash_inflates_total():
    """매수 후 옛 예수($1240)+신규 보유 — coalesce가 예수 역산."""
    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 1_240.58, "total": 2_289.48},
        },
    }
    snap = state["last_kis_display_snapshot"]["us"]
    cash, total = coalesce_ledger_kis_labels(
        "US",
        state,
        snap,
        holdings_current=2_209.0,
        cash_guess=1_240.58,
        total_guess=3_449.0,
    )
    assert cash < 100.0
    assert abs(total - (cash + 2_209.0)) < 1.0


def test_coalesce_partial_sell_settlement_lag():
    """부분 매도 후 예수만 API 지연 — 스냅샷 총평 유지·예수 보정."""
    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 957.40, "total": 2466.12},
        },
    }
    snap = state["last_kis_display_snapshot"]["us"]
    cash, total = coalesce_ledger_kis_labels(
        "US",
        state,
        snap,
        holdings_current=1176.14,
        cash_guess=957.40,
        total_guess=2133.54,
    )
    assert total > 2400.0
    assert cash > 1250.0
    assert abs(total - (cash + 1176.14)) < 1.0


def test_sanitize_persist_blocks_stale_cash_after_buy():
    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 1_240.58, "total": 2_289.48},
        },
    }
    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    cash, total = _sanitize_kis_cash_total_persist("US", state, 1_240.58, 2_209.0)
    assert cash < 100.0
    assert abs(total - 2_289.48) < 5.0


def test_sanitize_persist_blocks_cash_rebound_after_buy_correction():
    """1차 이중합산 역산 후 API 예수($950) 재유입 → 총평 부풀림 차단."""
    import time

    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    state = {
        "last_kis_display_snapshot": {
            "us": {
                "cash": 19.97,
                "total": 3_498.55,
                "_buy_cash_guard": {
                    "cash": 19.97,
                    "total": 3_498.55,
                    "ts": time.time(),
                },
            },
        },
    }
    cash, total = _sanitize_kis_cash_total_persist("US", state, 950.52, 3_478.0)
    assert cash < 50.0
    assert total < 3_600.0
    assert abs(total - (cash + 3_478.0)) < 1.0


def test_sanitize_persist_repairs_inflated_snap_via_recent_buy():
    """이미 오염된 스냅샷($950+$TLT) — 최근 매입대금으로 예수 복구."""
    import time

    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 950.52, "total": 4_428.83},
        },
        "positions": {
            "TLT": {
                "qty": 11,
                "buy_p": 84.56,
                "buy_time": time.time() - 60,
            },
            "AAPL": {
                "qty": 3,
                "buy_p": 315.36,
                "buy_time": time.time() - 3 * 86400,
            },
        },
    }
    cash, total = _sanitize_kis_cash_total_persist("US", state, 950.52, 3_478.0)
    assert abs(cash - (950.52 - 11 * 84.56)) < 1.0
    assert cash < 50.0
    assert total < 3_600.0


def test_persist_live_cash_is_noop():
    from execution.balance_read import _persist_live_cash

    assert _persist_live_cash("US", {"rt_cd": "0", "output2": {}}) is None


def test_kis_display_total_prefers_ledger_only_aux():
    from services.ledger_valuation import kis_display_total

    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 1_000_000, "total": 2_000_000},
            "us": {"cash": 100.0, "total": 576.0},
        },
        "_phase5_aux_sync": {
            "ledger_only": True,
            "kr_krw": 2_500_000.0,
            "usd_total": 1434.53,
        },
    }
    assert kis_display_total(state, "KR") == 2_500_000.0
    assert kis_display_total(state, "US") == 1434.53
    state["_phase5_aux_sync"]["ledger_only"] = False
    assert kis_display_total(state, "US") == 576.0
