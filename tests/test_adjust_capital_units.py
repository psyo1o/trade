# -*- coding: utf-8 -*-
"""고점 보정 — 미장 USD·코인 USDT 입력이 시장 고점에 그대로, 합산 고점엔 환산액으로 반영."""
import json
from unittest.mock import patch

import adjust_capital as ac


def _write_state(path, extra: dict):
    base = {"positions": {}, "peak_total_equity": 10_000_000.0}
    base.update(extra)
    path.write_text(json.dumps(base), encoding="utf-8")


def _run(tmp_path, market, *, withdraw=False, amount=None, amount_krw=None, extra=None, unit="USD", fx=1400.0):
    p = tmp_path / "bot_state.json"
    _write_state(p, extra or {})
    with patch.object(ac, "_refresh_aux_snapshot"), patch.object(
        ac, "capital_amount_unit", return_value=unit
    ), patch.object(ac, "_krw_per_unit", return_value=fx), patch.object(
        ac, "estimate_usdkrw", return_value=fx
    ), patch.object(ac, "portfolio_total_krw_estimated", return_value=20_000_000.0), patch.object(
        ac, "apply_phase5_share_anchor"
    ):
        ok, msg = ac.apply_capital_peak_adjustment(
            withdraw=withdraw,
            amount=amount,
            amount_krw=amount_krw,
            state_path=p,
            market=market,
        )
    return ok, msg, json.loads(p.read_text(encoding="utf-8"))


def test_us_deposit_in_usd(tmp_path):
    ok, msg, st = _run(tmp_path, "US", amount=1000.0, extra={"peak_equity_US": 3500.0})
    assert ok, msg
    assert st["peak_equity_US"] == 4500.0
    assert st["peak_total_equity"] == 10_000_000.0 + 1000.0 * 1400.0
    log = st["capital_adjustments"][-1]
    assert log["amount_unit"] == "USD"
    assert log["amount_native"] == 1000.0
    assert log["amount_krw"] == 1_400_000.0
    assert "1,000.00 USD" in msg


def test_coin_usdt_withdraw(tmp_path):
    ok, msg, st = _run(
        tmp_path,
        "COIN",
        withdraw=True,
        amount=200.0,
        unit="USDT",
        fx=1350.0,
        extra={"peak_equity_COIN": 1000.0},
    )
    assert ok, msg
    assert st["peak_equity_COIN"] == 800.0
    assert st["peak_total_equity"] == 10_000_000.0 - 200.0 * 1350.0


def test_kr_amount_is_krw(tmp_path):
    ok, msg, st = _run(
        tmp_path, "KR", amount=500_000.0, unit="KRW", fx=1.0, extra={"peak_equity_KR": 700_000.0}
    )
    assert ok, msg
    assert st["peak_equity_KR"] == 1_200_000.0
    assert st["peak_total_equity"] == 10_500_000.0


def test_legacy_amount_krw_converted_for_us(tmp_path):
    ok, msg, st = _run(tmp_path, "US", amount_krw=1_400_000.0, extra={"peak_equity_US": 3500.0})
    assert ok, msg
    assert st["peak_equity_US"] == 4500.0
