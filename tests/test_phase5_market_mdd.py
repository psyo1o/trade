# -*- coding: utf-8 -*-
"""Phase5 시장별 평가 MDD · 모드 전환 쿨다운 해제."""
from __future__ import annotations

import tempfile
from pathlib import Path

from execution.guard import (
    ACCOUNT_CIRCUIT_COOLDOWN_KEY,
    ACCOUNT_CIRCUIT_MODE_KEY,
    PHASE5_MARKET_COOLDOWNS_KEY,
    PHASE5_POST_BUY_GRACE_SEC,
    adjust_peak_equity_for_capital,
    apply_phase5_trailing_market_peaks,
    check_mdd_break,
    get_phase5_peak_market_equity,
    market_in_post_buy_grace,
    sync_account_circuit_mode,
)


def test_trailing_inits_missing_peak():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot_state.json"
        state = {"positions": {}}
        apply_phase5_trailing_market_peaks(
            state,
            {"KR": 658_357.0, "US": 3462.0, "COIN": 870_000.0},
            path,
        )
        assert abs(get_phase5_peak_market_equity(state, "KR") - 658_357.0) < 1
        assert abs(get_phase5_peak_market_equity(state, "US") - 3462.0) < 0.01


def test_us_deposit_does_not_change_kr_peak():
    state = {"peak_equity_KR": 1_000_000.0, "peak_equity_US": 3_000.0}
    adjust_peak_equity_for_capital(state, "US", 500.0)
    assert state["peak_equity_KR"] == 1_000_000.0
    assert abs(state["peak_equity_US"] - 3_500.0) < 1e-9


def test_kr_withdraw_lowers_only_kr_peak():
    state = {"peak_equity_KR": 1_000_000.0, "peak_equity_COIN": 800_000.0}
    adjust_peak_equity_for_capital(state, "KR", -200_000.0)
    assert state["peak_equity_KR"] == 800_000.0
    assert state["peak_equity_COIN"] == 800_000.0


def test_mode_switch_clears_share_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot_state.json"
        state = {
            ACCOUNT_CIRCUIT_MODE_KEY: "share",
            PHASE5_MARKET_COOLDOWNS_KEY: {"KR": "2099-01-01T00:00:00"},
            ACCOUNT_CIRCUIT_COOLDOWN_KEY: "2099-01-01T00:00:00",
            "phase5_pending_liquidation": True,
            "phase5_pending_liquidation_markets": ["KR"],
        }
        changed = sync_account_circuit_mode(state, "per_market_mdd", path)
        assert changed is True
        assert PHASE5_MARKET_COOLDOWNS_KEY not in state
        assert ACCOUNT_CIRCUIT_COOLDOWN_KEY not in state
        assert state.get("phase5_pending_liquidation") is False
        assert state.get(ACCOUNT_CIRCUIT_MODE_KEY) == "per_market_mdd"


def test_label_correction_clamps_kr_peak_and_clears_circuit():
    """이중합산 고점(현금+보유를 두 번 더한 값)은 총평 수정 후 MDD 로 보지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot_state.json"
        state = {
            "positions": {
                "411060": {
                    "buy_p": 27985.0,
                    "qty": 11.0,
                    "curr_p": 27990.0,
                    "buy_time": 1.0,
                }
            },
            "peak_equity_KR": 966_068.0,
            "peak_equity_US": 3_462.24,
            "peak_equity_COIN": 870_000.0,
            PHASE5_MARKET_COOLDOWNS_KEY: {"KR": "2099-01-01T00:00:00"},
            "phase5_pending_liquidation": True,
            "phase5_pending_liquidation_markets": ["KR"],
            "account_circuit_market_peak_reset_pending": {"KR": True},
        }
        apply_phase5_trailing_market_peaks(
            state,
            {"KR": 658_895.0, "US": 3_462.24, "COIN": 870_000.0},
            path,
        )
        assert abs(get_phase5_peak_market_equity(state, "KR") - 658_895.0) < 1.0
        assert "KR" not in (state.get(PHASE5_MARKET_COOLDOWNS_KEY) or {})
        assert state.get("phase5_pending_liquidation") is False
        assert not state.get("phase5_pending_liquidation_markets")


def test_real_mdd_drop_does_not_clamp_peak():
    """보유평가와 무관한 실제 급락은 고점을 깎지 않는다."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot_state.json"
        state = {
            "positions": {
                "411060": {
                    "buy_p": 27985.0,
                    "qty": 11.0,
                    "curr_p": 20000.0,
                    "buy_time": 1.0,
                }
            },
            "peak_equity_KR": 658_357.0,
        }
        apply_phase5_trailing_market_peaks(
            state,
            {"KR": 500_000.0, "US": 3_000.0, "COIN": 800_000.0},
            path,
        )
        assert abs(get_phase5_peak_market_equity(state, "KR") - 658_357.0) < 1.0


def test_spike_does_not_become_next_loop_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot_state.json"
        state = {
            "positions": {},
            "peak_equity_KR": 658_357.0,
            "phase5_last_loop_equity_by_market": {"KR": 658_357.0},
        }
        apply_phase5_trailing_market_peaks(
            state,
            {"KR": 966_068.0, "US": 3_000.0, "COIN": 800_000.0},
            path,
        )
        assert abs(get_phase5_peak_market_equity(state, "KR") - 658_357.0) < 1.0
        last = state.get("phase5_last_loop_equity_by_market") or {}
        assert abs(float(last.get("KR", 0) or 0) - 658_357.0) < 1.0



def test_check_mdd_break_disabled_always_allows():
    """계좌 -5% MDD 매수 차단 폐지 — 항상 True (Phase5 15%만 유효)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot_state.json"
        state = {
            "peak_equity_KR": 658_895.0,
            "positions": {
                "411060": {
                    "qty": 11.0,
                    "buy_p": 27_985.0,
                    "curr_p": 27_985.0,
                }
            },
        }
        assert check_mdd_break("KR", 351_060.0, state, path) is True
        assert check_mdd_break("KR", 600_000.0, state, path) is True
        assert check_mdd_break("US", 1.0, {"peak_equity_US": 10000.0}, path) is True


def test_market_in_post_buy_grace_after_recent_us_buy():
    import time

    state = {
        "positions": {
            "TLT": {"qty": 13, "buy_p": 82.93, "buy_time": time.time() - 120},
        }
    }
    assert market_in_post_buy_grace(state, "US", grace_sec=PHASE5_POST_BUY_GRACE_SEC) is True
    assert market_in_post_buy_grace(state, "US", grace_sec=60) is False


def test_market_in_post_buy_grace_false_when_no_recent_buy():
    import time

    state = {
        "positions": {
            "TLT": {"qty": 13, "buy_p": 82.93, "buy_time": time.time() - 7200},
        }
    }
    assert market_in_post_buy_grace(state, "US") is False
