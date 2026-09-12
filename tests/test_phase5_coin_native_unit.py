# -*- coding: utf-8 -*-
"""Phase5 COIN 견적통화(업비트 원 / 바이낸스 USDT) · peak 단위 마이그레이션."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from execution.guard import (
    PEAK_EQUITY_COIN_UNIT_KEY,
    get_phase5_peak_market_equity,
    migrate_coin_peak_unit_if_needed,
)


def test_persist_circuit_aux_coin_binance_dual_write():
    from api import coin_broker

    state: dict = {}
    with patch.object(coin_broker.coin_config, "is_binance", return_value=True):
        with patch.object(coin_broker, "get_krw_per_usdt", return_value=1400.0):
            n, krw = coin_broker.persist_circuit_aux_coin(state, 100.0, krw_per_usdt=1400.0)
    assert abs(n - 100.0) < 1e-9
    assert abs(krw - 140_000.0) < 1e-6
    assert state["circuit_aux_last_coin_native"] == 100.0
    assert abs(state["circuit_aux_last_coin_krw"] - 140_000.0) < 1e-6
    assert state["circuit_aux_last_coin_unit"] == "USDT"


def test_persist_circuit_aux_coin_upbit_same_unit():
    from api import coin_broker

    state: dict = {}
    with patch.object(coin_broker.coin_config, "is_binance", return_value=False):
        n, krw = coin_broker.persist_circuit_aux_coin(state, 870_000.0)
    assert abs(n - 870_000.0) < 1e-6
    assert abs(krw - 870_000.0) < 1e-6
    assert state["circuit_aux_last_coin_unit"] == "KRW"


def test_migrate_resets_legacy_krw_peak_on_binance():
    """구 peak(원 수백만) vs USDT 현재 → 오발동 방지 리셋."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot_state.json"
        state = {
            "peak_equity_COIN": 870_000.0,  # legacy KRW
            "circuit_aux_last_coin_native": 620.0,
        }
        with patch("api.coin_broker.coin_config.is_binance", return_value=True):
            changed = migrate_coin_peak_unit_if_needed(state, 620.0, path)
        assert changed is True
        assert abs(get_phase5_peak_market_equity(state, "COIN") - 620.0) < 1e-9
        assert state.get(PEAK_EQUITY_COIN_UNIT_KEY) == "USDT"


def test_migrate_noop_when_unit_already_usdt():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bot_state.json"
        state = {
            "peak_equity_COIN": 650.0,
            PEAK_EQUITY_COIN_UNIT_KEY: "USDT",
        }
        with patch("api.coin_broker.coin_config.is_binance", return_value=True):
            changed = migrate_coin_peak_unit_if_needed(state, 640.0, path)
        assert changed is False
        assert abs(get_phase5_peak_market_equity(state, "COIN") - 650.0) < 1e-9


def test_circuit_aux_coin_native_fallback_from_krw_binance():
    from api import coin_broker

    state = {"circuit_aux_last_coin_krw": 140_000.0}
    with patch.object(coin_broker.coin_config, "is_binance", return_value=True):
        with patch.object(coin_broker, "get_krw_per_usdt", return_value=1400.0):
            assert abs(coin_broker.circuit_aux_coin_native(state) - 100.0) < 1e-6
