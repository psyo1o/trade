# -*- coding: utf-8 -*-
"""Phase5 — 보유 없음 스킵 등."""
from __future__ import annotations

from unittest.mock import patch

from execution import phase5_ops as p5


def test_index_circuit_skips_when_no_holdings():
    state = {"positions": {}}
    st = {}
    market_ok = {"KR": True, "US": False, "COIN": False}

    class _Rb:
        STATE_PATH = __import__("pathlib").Path("dummy")
        ACCOUNT_CIRCUIT_MDD_PCT = 15.0

    triggered_ev = {
        "KR": {
            "triggered": True,
            "drawdown_pct": 24.0,
            "benchmark": "069500.KS",
            "reason": "would trigger",
        }
    }

    with patch(
        "execution.phase5_index_circuit.evaluate_per_market_index_circuits",
        return_value=dict(triggered_ev),
    ):
        with patch.object(p5, "load_state", return_value=st):
            with patch.object(p5, "_liquidate_triggered_markets") as liq:
                p5._run_per_market_index_circuits(_Rb(), state, st, market_ok=market_ok)
                liq.assert_called_once()
                circuits = liq.call_args[0][3]
                assert circuits["KR"]["triggered"] is False
                assert "보유 없음" in circuits["KR"]["reason"]


def test_liquidate_never_runs_without_holdings():
    state = {"positions": {}}
    st = {}
    circuits = {"KR": {"triggered": True, "reason": "x"}}

    class _Rb:
        STATE_PATH = __import__("pathlib").Path("dummy")

    with patch.object(p5, "load_state", return_value=st):
        with patch.object(p5, "prune_stale_pending"):
            with patch.object(p5, "try_pending_liquidation"):
                with patch.object(p5, "_reconfirm_phase5_triggers_before_liquidation") as rec:
                    with patch.object(p5, "liquidate_market") as sell:
                        p5._liquidate_triggered_markets(
                            _Rb(), state, st, circuits, {"KR": True}, kind="지수"
                        )
                        rec.assert_not_called()
                        sell.assert_not_called()


def test_market_has_ledger_positions_requires_positive_qty():
    assert p5.market_has_ledger_positions({"positions": {"005930": {"qty": 0}}}, "KR") is False
    assert p5.market_has_ledger_positions({"positions": {"005930": {"qty": 3}}}, "KR") is True


def test_mdd_circuit_skips_when_no_holdings():
    state = {"positions": {}}
    st = {"peak_equity_KR": 1_000_000.0}
    market_ok = {"KR": True, "US": False, "COIN": False}

    class _Rb:
        STATE_PATH = __import__("pathlib").Path("dummy")
        ACCOUNT_CIRCUIT_MDD_PCT = 15.0

    with patch.object(p5, "load_state", return_value=st):
        with patch("execution.phase5_ops.migrate_coin_peak_unit_if_needed", return_value=False):
            with patch("execution.phase5_ops.apply_phase5_trailing_market_peaks"):
                with patch("execution.phase5_ops.get_phase5_peak_market_equity", return_value=1_000_000.0):
                    with patch.object(p5, "_liquidate_triggered_markets") as liq:
                        p5._run_per_market_mdd_circuits(
                            _Rb(),
                            state,
                            st,
                            kr_krw=500_000.0,
                            us_usd=0.0,
                            coin_native=0.0,
                            market_ok=market_ok,
                        )
                        liq.assert_called_once()
                        circuits = liq.call_args[0][3]
                        assert circuits["KR"]["triggered"] is False
                        assert "보유 없음" in circuits["KR"]["reason"]
