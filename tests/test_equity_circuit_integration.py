# -*- coding: utf-8 -*-
"""post_buy 스냅샷 오염 · risk 경로 · Phase5 통합 시나리오 (Phase 3)."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from execution.circuit_break import evaluate_per_market_equity_circuits
from execution.guard import get_phase5_peak_market_equity
from services.ledger_valuation import (
    EQUITY_DIVERGENCE_CONFIRM_PCT,
    equity_divergence_metrics,
    market_equity_for_risk,
)


def _post_buy_us_state():
    """TLT 매수 직후 B3 오염 스냅샷 — 예수는 정상·총평만 깎임."""
    return {
        "peak_equity_US": 3465.42,
        "last_kis_display_snapshot": {
            "us": {"cash": 2213.0, "total": 2313.0},
        },
        "positions": {
            "TLT": {"qty": 13.0, "buy_p": 82.93, "curr_p": 82.92},
            "DXCM": {"qty": 13.0, "buy_p": 87.06, "curr_p": 90.0},
        },
    }


def _post_buy_kr_state():
    """411060 매수 직후 B3 오염 — 예수 정상·총평만 반토막."""
    return {
        "peak_equity_KR": 658_895.0,
        "last_kis_display_snapshot": {
            "kr": {"cash": 350_510.0, "total": 351_060.0},
        },
        "positions": {
            "411060": {
                "qty": 11.0,
                "buy_p": 27_985.0,
                "curr_p": 27_990.0,
            },
        },
    }


def test_post_buy_us_risk_vs_snap_divergence():
    state = _post_buy_us_state()
    risk, snap, pct = equity_divergence_metrics(state, "US")
    assert snap == 2313.0
    assert risk > 3300.0
    assert pct >= EQUITY_DIVERGENCE_CONFIRM_PCT


def test_post_buy_us_phase5_no_false_trigger_on_risk():
    state = _post_buy_us_state()
    risk = market_equity_for_risk(state, "US")
    peak = get_phase5_peak_market_equity(state, "US")
    ev = evaluate_per_market_equity_circuits(
        equities={"KR": 0, "US": risk, "COIN": 0},
        peaks={"KR": 0, "US": peak, "COIN": 0},
        market_ok={"KR": False, "US": True, "COIN": False},
        trigger_drawdown_pct=15.0,
    )["US"]
    assert ev["triggered"] is False
    assert float(ev["drawdown_pct"]) < 15.0


def test_post_buy_us_snap_alone_would_false_trigger():
    """스냅샷 총평만 쓰면 오발동 — 설계상 risk 분리 목적."""
    state = _post_buy_us_state()
    snap = float(state["last_kis_display_snapshot"]["us"]["total"])
    peak = get_phase5_peak_market_equity(state, "US")
    ev = evaluate_per_market_equity_circuits(
        equities={"KR": 0, "US": snap, "COIN": 0},
        peaks={"KR": 0, "US": peak, "COIN": 0},
        market_ok={"KR": False, "US": True, "COIN": False},
        trigger_drawdown_pct=15.0,
    )["US"]
    assert ev["triggered"] is True


def test_post_buy_kr_risk_vs_snap_divergence():
    state = _post_buy_kr_state()
    risk, snap, pct = equity_divergence_metrics(state, "KR")
    assert snap == 351_060.0
    assert risk > 600_000.0
    assert pct >= EQUITY_DIVERGENCE_CONFIRM_PCT


def test_post_buy_kr_phase5_no_false_trigger_on_risk():
    state = _post_buy_kr_state()
    risk = market_equity_for_risk(state, "KR")
    peak = get_phase5_peak_market_equity(state, "KR")
    ev = evaluate_per_market_equity_circuits(
        equities={"KR": risk, "US": 0, "COIN": 0},
        peaks={"KR": peak, "US": 0, "COIN": 0},
        market_ok={"KR": True, "US": False, "COIN": False},
        trigger_drawdown_pct=15.0,
    )["KR"]
    assert ev["triggered"] is False


def test_reconfirm_cancels_when_equity_below_threshold():
    from execution import phase5_ops as p5

    state = {
        "peak_equity_US": 4000.0,
        "last_kis_display_snapshot": {"us": {"cash": 3500.0, "total": 3500.0}},
        "positions": {},
    }
    circuits = {
        "US": {
            "triggered": True,
            "drawdown_pct": 20.0,
            "current": 3000.0,
            "peak": 4000.0,
            "reason": "test",
        }
    }

    class _Rb:
        STATE_PATH = Path("dummy")
        ACCOUNT_CIRCUIT_MDD_PCT = 15.0
        ACCOUNT_CIRCUIT_USE_INDEX = False

    def _fake_refresh(st, mk, path):
        st["last_kis_display_snapshot"]["us"] = {"cash": 3900.0, "total": 3900.0}
        return True

    with patch(
        "services.ledger_valuation.refresh_kis_display_snapshot_for_market",
        side_effect=_fake_refresh,
    ):
        with patch.object(p5, "load_state", return_value=state):
            with patch.object(p5, "save_state"):
                confirmed = p5._reconfirm_phase5_triggers_before_liquidation(
                    _Rb(), state, state, circuits, ["US"]
                )
    assert confirmed == []
    assert circuits["US"]["triggered"] is False


def test_reconfirm_defers_to_next_cycle_then_confirms():
    """1회차: pending + 보류 / 2회차(다음 주기): 여전히 MDD면 AI 진입."""
    from execution import phase5_ops as p5

    state = {
        "peak_equity_US": 4000.0,
        "last_kis_display_snapshot": {"us": {"cash": 500.0, "total": 2000.0}},
        "positions": {"XYZ": {"qty": 10.0, "curr_p": 50.0}},
    }
    circuits = {
        "US": {
            "triggered": True,
            "drawdown_pct": 50.0,
            "current": 2000.0,
            "peak": 4000.0,
            "reason": "test",
        }
    }

    class _Rb:
        STATE_PATH = Path("dummy")
        ACCOUNT_CIRCUIT_MDD_PCT = 15.0
        ACCOUNT_CIRCUIT_USE_INDEX = False

    def _fake_refresh(st, mk, path):
        st["last_kis_display_snapshot"]["us"] = {"cash": 500.0, "total": 2000.0}
        return True

    with patch(
        "services.ledger_valuation.refresh_kis_display_snapshot_for_market",
        side_effect=_fake_refresh,
    ):
        with patch.object(p5, "load_state", return_value=state):
            with patch.object(p5, "save_state"):
                c1 = p5._reconfirm_phase5_triggers_before_liquidation(
                    _Rb(), state, state, circuits, ["US"]
                )
    assert c1 == []
    assert circuits["US"].get("reconfirm_deferred") is True
    assert "US" in (state.get("phase5_reconfirm_pending") or {})

    circuits2 = {
        "US": {
            "triggered": True,
            "drawdown_pct": 50.0,
            "current": 2000.0,
            "peak": 4000.0,
            "reason": "test",
        }
    }
    with patch(
        "services.ledger_valuation.refresh_kis_display_snapshot_for_market",
        side_effect=_fake_refresh,
    ):
        with patch.object(p5, "load_state", return_value=state):
            with patch.object(p5, "save_state"):
                c2 = p5._reconfirm_phase5_triggers_before_liquidation(
                    _Rb(), state, state, circuits2, ["US"]
                )
    assert c2 == ["US"]
    assert circuits2["US"]["triggered"] is True
    assert "t2_next_cycle" in circuits2["US"].get("reconfirm_summary", "")
    assert "US" not in (state.get("phase5_reconfirm_pending") or {})


def test_reconfirm_next_cycle_cancels_when_recovered():
    from execution import phase5_ops as p5

    state = {
        "peak_equity_US": 4000.0,
        "last_kis_display_snapshot": {"us": {"cash": 500.0, "total": 2000.0}},
        "positions": {"XYZ": {"qty": 10.0, "curr_p": 50.0}},
        "phase5_reconfirm_pending": {
            "US": {"trail": ["t0"], "ts": 1.0, "peak": 4000.0, "dd": 50.0}
        },
    }
    circuits = {
        "US": {
            "triggered": True,
            "drawdown_pct": 50.0,
            "current": 2000.0,
            "peak": 4000.0,
            "reason": "test",
        }
    }

    class _Rb:
        STATE_PATH = Path("dummy")
        ACCOUNT_CIRCUIT_MDD_PCT = 15.0
        ACCOUNT_CIRCUIT_USE_INDEX = False

    def _fake_refresh(st, mk, path):
        # 다음 주기에 정상 회복
        st["last_kis_display_snapshot"]["us"] = {"cash": 3900.0, "total": 3900.0}
        st["positions"] = {}
        return True

    with patch(
        "services.ledger_valuation.refresh_kis_display_snapshot_for_market",
        side_effect=_fake_refresh,
    ):
        with patch.object(p5, "load_state", return_value=state):
            with patch.object(p5, "save_state"):
                confirmed = p5._reconfirm_phase5_triggers_before_liquidation(
                    _Rb(), state, state, circuits, ["US"]
                )
    assert confirmed == []
    assert circuits["US"]["triggered"] is False
    assert "US" not in (state.get("phase5_reconfirm_pending") or {})


def test_reconfirm_always_refreshes_kis():
    from execution import phase5_ops as p5

    state = {
        "peak_equity_KR": 700_000.0,
        "last_kis_display_snapshot": {
            "kr": {"cash": 100_000.0, "total": 100_000.0},
        },
        "positions": {},
    }
    circuits = {
        "KR": {
            "triggered": True,
            "drawdown_pct": 85.0,
            "current": 100_000.0,
            "peak": 700_000.0,
            "reason": "test",
        }
    }

    class _Rb:
        STATE_PATH = Path("dummy")
        ACCOUNT_CIRCUIT_MDD_PCT = 15.0
        ACCOUNT_CIRCUIT_USE_INDEX = False

    refresh_calls: list[str] = []

    def _fake_refresh(st, mk, path):
        refresh_calls.append(mk)
        st["last_kis_display_snapshot"]["kr"] = {
            "cash": 700_000.0,
            "total": 700_000.0,
        }
        return True

    with patch(
        "services.ledger_valuation.refresh_kis_display_snapshot_for_market",
        side_effect=_fake_refresh,
    ):
        with patch.object(p5, "load_state", return_value=state):
            with patch.object(p5, "save_state"):
                confirmed = p5._reconfirm_phase5_triggers_before_liquidation(
                    _Rb(), state, state, circuits, ["KR"]
                )
    assert refresh_calls == ["KR"]
    assert confirmed == []
