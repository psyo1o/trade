# -*- coding: utf-8 -*-
"""V8 절대 손절 캡(-8% 주식, -12% 코인) 단위 검증."""
from strategy.rules import (
    V8_MAX_STOP_CAP_MULT_COIN,
    V8_MAX_STOP_CAP_MULT_EQUITY,
    _v8_max_stop_cap_floor,
    get_final_exit_price,
)


def test_v8_cap_mult_constants():
    assert V8_MAX_STOP_CAP_MULT_EQUITY == 0.92
    assert V8_MAX_STOP_CAP_MULT_COIN == 0.88


def test_v8_cap_floor_by_market():
    assert _v8_max_stop_cap_floor(10_000, ticker="005930") == 9_200
    assert _v8_max_stop_cap_floor(100.0, ticker="AAPL") == 92.0
    assert _v8_max_stop_cap_floor(50_000, ticker="KRW-BTC") == 44_000


def test_get_final_exit_price_respects_cap():
    buy = 10_000.0
    pos = {
        "buy_p": buy,
        "max_p": buy,
        "sl_p": buy * 0.5,
        "current_atr": buy * 0.05,
    }
    line = get_final_exit_price("005930", buy, pos, [{"h": buy, "l": buy * 0.9, "c": buy}] * 20)
    assert line >= buy * V8_MAX_STOP_CAP_MULT_EQUITY
