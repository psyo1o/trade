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


def test_write_snapshot_skips_kr_cash_zero_after_buy():
    """매수 직후 예수 0·총평=보유만 찍혀도 직전 스냅샷을 유지."""
    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 657_147, "total": 657_147},
        }
    }
    write_kis_display_snapshot_part(state, "KR", cash=0, total=209_880)
    kr = state["last_kis_display_snapshot"]["kr"]
    assert int(kr["cash"]) == 657_147
    assert int(kr["total"]) == 657_147


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


def test_kis_display_total_rejects_aux_crash_vs_snapshot():
    from services.ledger_valuation import kis_display_total

    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 658_357, "total": 658_357},
        },
        "_phase5_aux_sync": {
            "ledger_only": True,
            "kr_krw": 209_880.0,
        },
    }
    assert kis_display_total(state, "KR") == 658_357.0


def test_display_cash_all_cash_no_holdings_is_not_stale():
    from services.ledger_valuation import display_cash_from_state

    state = {
        "positions": {},
        "last_kis_display_snapshot": {
            "kr": {"cash": 658_357, "total": 658_357},
        },
        "last_kr_cash_krw": 1_063_317.0,
    }
    assert display_cash_from_state(state, "KR") == 658_357.0


def test_sanitize_persist_repairs_kr_cash_vanished_after_buy():
    """국장 매수 직후 예수 0·총평=보유평가만 — 직전 예수에서 매입대금을 뺀다."""
    import time

    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 657_147, "total": 657_147},
        },
        "positions": {
            "085620": {
                "qty": 11,
                "buy_p": 19070.0,
                "buy_time": time.time() - 60,
            },
        },
    }
    cash, total = _sanitize_kis_cash_total_persist("KR", state, 0.0, 209_880.0)
    assert abs(cash - (657_147 - 11 * 19070)) < 1.0
    assert abs(total - (cash + 209_880.0)) < 1.0
    assert total > 600_000


def test_sanitize_persist_repairs_kr_first_buy_from_all_cash():
    """전액 현금 계좌에서 첫 매수 — 예수 미차감·총평 이중합산을 역산한다."""
    import time

    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 658_357, "total": 658_357},
        },
        "positions": {
            "411060": {
                "qty": 11,
                "buy_p": 27885.0,
                "buy_time": time.time() - 30,
            },
        },
    }
    stock = 11 * 27885.0
    cash, total = _sanitize_kis_cash_total_persist("KR", state, 658_178.0, stock)
    assert abs(cash - (658_357 - stock)) < 1.0
    assert abs(total - 658_357.0) < 1.0


def test_sanitize_persist_repairs_kr_already_inflated_snap():
    """이미 예수+보유로 부풀려진 스냅샷을 다음 조회에서 매입대금으로 복구."""
    import time

    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    spend = 11 * 27885.0
    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 658_178, "total": 967_223},
        },
        "positions": {
            "411060": {
                "qty": 11,
                "buy_p": 27885.0,
                "buy_time": time.time() - 60,
            },
        },
    }
    cash, total = _sanitize_kis_cash_total_persist("KR", state, 658_178.0, spend)
    assert abs(cash - (658_178 - spend)) < 1.0
    assert abs(total - (cash + spend)) < 1.0
    assert total < 700_000


def test_sanitize_b3_skips_us_when_cash_already_deducted():
    """TLT 매수 후 예수 $1,147 — B3 재차감 금지."""
    import time

    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    spend = 13 * 82.93
    prev_cash = 2225.27
    post_cash = prev_cash - spend  # ~1147
    stock = 1169.48 + spend
    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": prev_cash, "total": prev_cash + 1169.48},
        },
        "positions": {
            "TLT": {
                "qty": 13,
                "buy_p": 82.93,
                "buy_time": time.time() - 30,
            },
        },
    }
    cash, total = _sanitize_kis_cash_total_persist("US", state, post_cash, stock)
    assert abs(cash - post_cash) < 1.0
    assert total > 3000.0


def test_write_snapshot_rejects_force_post_buy_total_crash():
    from services.ledger_valuation import write_kis_display_snapshot_part

    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 2225.0, "total": 3394.0},
        }
    }
    write_kis_display_snapshot_part(
        state, "US", cash=69.0, total=2313.0, force=True
    )
    assert state["last_kis_display_snapshot"]["us"]["total"] == 3394.0


def test_market_equity_cash_only_us_not_legacy():
    """전액 현금(cash≈total) + 보유 0 → 레거시 last_us_cash_usd 무시."""
    from services.ledger_valuation import display_cash_from_state, market_equity_for_risk

    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 3378.93, "total": 3378.93},
        },
        "last_us_cash_usd": 37.91,
        "positions": {},
    }
    assert abs(display_cash_from_state(state, "US") - 3378.93) < 0.01
    assert abs(market_equity_for_risk(state, "US") - 3378.93) < 0.01


def test_market_equity_for_risk_uses_cash_plus_holdings():
    from services.ledger_valuation import market_equity_for_risk

    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 2213.0, "total": 2313.0},
        },
        "positions": {
            "TLT": {"qty": 13.0, "buy_p": 82.93, "curr_p": 82.92},
            "DXCM": {"qty": 13.0, "buy_p": 87.06, "curr_p": 90.0},
        },
    }
    eq = market_equity_for_risk(state, "US")
    assert eq > 3300.0


def test_market_equity_for_risk_orphaned_cash_after_kr_sell():
    """장부만 비고 스냅 예수 정체 → snap_total 폴백 (Phase5 오발동 방지)."""
    from services.ledger_valuation import market_equity_for_risk

    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 100_000.0, "total": 700_000.0},
        },
        "positions": {},
    }
    eq = market_equity_for_risk(state, "KR")
    assert abs(eq - 700_000.0) < 1.0


def test_market_equity_for_risk_orphaned_with_partial_holdings():
    """분할 매도 후 장부 qty↓·snap_total 유지 → snap_total."""
    from services.ledger_valuation import market_equity_for_risk

    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 350_510.0, "total": 667_792.0},
        },
        "positions": {},
    }
    eq = market_equity_for_risk(state, "KR")
    assert abs(eq - 667_792.0) < 1.0


def test_market_equity_for_risk_uses_ledger_when_above_snap():
    """보유 반영 ledger가 snap보다 충분히 크면 ledger 신뢰."""
    from services.ledger_valuation import market_equity_for_risk

    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 2000.0, "total": 3000.0},
        },
        "positions": {
            "TLT": {"qty": 20.0, "curr_p": 100.0},
        },
    }
    eq = market_equity_for_risk(state, "US")
    assert eq > 3000.0 * 1.05


def test_sanitize_rule3_rejects_post_buy_total_crash_without_sell():
    """매도 없이 sanitize 결과 총평 급감 → Rule3 직전 총평 유지."""
    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    state = {
        "last_kis_display_snapshot": {
            "us": {"cash": 2225.0, "total": 3394.0},
        },
    }
    cash, total = _sanitize_kis_cash_total_persist("US", state, 69.0, 2244.0)
    assert abs(total - 3394.0) < 1.0
    assert abs(cash - (3394.0 - 2244.0)) < 1.0


def test_sanitize_keeps_settled_kr_after_legacy_guard_in_snap():
    """레거시 _buy_cash_guard 가 있어도 정상 잔여 예수는 Rule2 스킵."""
    import time

    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    spend = 307_835.0
    leftover = 350_510.0
    state = {
        "last_kis_display_snapshot": {
            "kr": {
                "cash": leftover,
                "total": leftover + spend,
                "_buy_cash_guard": {
                    "cash": 42_675.0,
                    "total": 351_060.0,
                    "ts": time.time(),
                },
            },
        },
        "positions": {
            "411060": {
                "qty": 11,
                "buy_p": 27_985.0,
                "buy_time": time.time() - 60,
            },
        },
    }
    cash, total = _sanitize_kis_cash_total_persist("KR", state, leftover, spend)
    assert abs(cash - leftover) < 1.0
    assert abs(total - (leftover + spend)) < 1.0


def test_sanitize_discards_stale_guard_that_would_halve_settled_kr():
    """정상 잔여 예수 — 재차감 없음 (test_sanitize_keeps_settled_kr_after_legacy_guard_in_snap 과 동일)."""
    test_sanitize_keeps_settled_kr_after_legacy_guard_in_snap()


def test_sanitize_does_not_rededuct_settled_kr_leftover_cash():
    """이미 차감된 잔여 예수(매입과 비슷한 규모)를 한 번 더 빼지 않는다."""
    import time

    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    spend = 11 * 27_985.0
    leftover = 350_510.0
    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": leftover, "total": leftover + spend},
        },
        "positions": {
            "411060": {
                "qty": 11,
                "buy_p": 27_985.0,
                "buy_time": time.time() - 60,
            },
        },
    }
    cash, total = _sanitize_kis_cash_total_persist("KR", state, leftover, spend)
    assert abs(cash - leftover) < 1.0
    assert abs(total - (leftover + spend)) < 1.0


def test_coalesce_recovers_kr_stock_only_blob_after_buy():
    """스냅샷이 보유평가만 남은 경우 last_kr_cash − 매입대금으로 예수 복구."""
    import time

    state = {
        "last_kr_cash_krw": 657_147.0,
        "last_kis_display_snapshot": {
            "kr": {"cash": 0, "total": 209_880},
        },
        "positions": {
            "085620": {
                "qty": 11,
                "buy_p": 19070.0,
                "buy_time": time.time() - 60,
            },
        },
    }
    cash, total = coalesce_ledger_kis_labels(
        "KR",
        state,
        state["last_kis_display_snapshot"]["kr"],
        holdings_current=209_880.0,
        cash_guess=0.0,
        total_guess=209_880.0,
    )
    assert cash > 400_000
    assert total > 600_000
    assert abs(total - (cash + 209_880.0)) < 1.0


def test_sanitize_rule1b_cash_plus_holdings_sep11():
    """2026-09-11: 보유 정체 + 예수=현금+보유 → Rule1b."""
    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    prev_cash = 436_653.0
    stock = 246_000.0
    prev_total = prev_cash + stock  # ~682653
    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": int(prev_cash), "total": int(prev_total)},
        },
        "positions": {},
    }
    bad_cash = prev_cash + stock  # 682653
    cash, total = _sanitize_kis_cash_total_persist("KR", state, bad_cash, stock)
    assert abs(cash - prev_cash) < 1.0
    assert abs(total - (prev_cash + stock)) < 1.0


def test_sanitize_rule1b_cash_equals_prev_total():
    from services.ledger_valuation import _sanitize_kis_cash_total_persist

    prev_cash = 400_000.0
    stock = 200_000.0
    prev_total = 600_000.0
    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": int(prev_cash), "total": int(prev_total)},
        },
        "positions": {},
    }
    cash, total = _sanitize_kis_cash_total_persist("KR", state, prev_total, stock)
    assert abs(cash - (prev_total - stock)) < 1.0
    assert abs(total - prev_total) < 1.0


def test_write_snapshot_rejects_unexplained_total_jump():
    from services.ledger_valuation import write_kis_display_snapshot_part

    state = {
        "last_kis_display_snapshot": {
            "kr": {"cash": 436_653, "total": 667_653},
            "saved_at": "2026-09-11 15:00:00",
        },
        "positions": {},
    }
    write_kis_display_snapshot_part(
        state, "KR", cash=682_653.0, total=928_653.0, force=False
    )
    part = state["last_kis_display_snapshot"]["kr"]
    assert int(part["cash"]) == 436_653
    assert int(part["total"]) == 667_653


def test_market_equity_for_risk_rejects_double_count_vs_last_loop():
    from services.ledger_valuation import market_equity_for_risk

    stock = 246_250.0
    state = {
        "phase5_last_loop_equity_by_market": {"KR": 681_653.0},
        "last_kis_display_snapshot": {
            "kr": {"cash": 682_653, "total": 928_653},
        },
        "positions": {
            "443060": {"qty": 1.0, "curr_p": stock, "buy_p": 230_500.0},
        },
    }
    risk = market_equity_for_risk(state, "KR")
    assert abs(risk - 681_653.0) < 1.0
