# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

from services.trade_history_ledger import (
    enrich_position_from_buy_history,
    find_last_buy_row,
    is_swing_trade_reason,
    ledger_extra_from_buy_payload,
    parse_history_timestamp,
    position_needs_history_enrich,
)


def test_ledger_extra_from_buy_payload():
    extra = ledger_extra_from_buy_payload(
        {
            "strategy_type": "SWING_FIB",
            "entry_fib_level": 398.64,
            "sl_p": 398.64,
            "buy_time": 1717458785.0,
            "entry_atr": 8.5,
        }
    )
    assert extra["strategy_type"] == "SWING_FIB"
    assert extra["entry_fib_level"] == 398.64
    assert extra["buy_time"] == 1717458785.0


def test_parse_history_timestamp():
    ts = parse_history_timestamp("2026-06-04 04:33:05")
    assert ts is not None
    assert ts > 0


def test_enrich_swing_from_reason_only(tmp_path: Path):
    hist = tmp_path / "trade_history.json"
    hist.write_text(
        json.dumps(
            [
                {
                    "timestamp": "2026-06-04 04:33:05",
                    "market": "US",
                    "ticker": "ETN",
                    "side": "BUY",
                    "qty": 1,
                    "price": 420.33,
                    "reason": "SWING_FIB",
                }
            ]
        ),
        encoding="utf-8",
    )
    pos = {"buy_p": 420.32, "qty": 1.0, "tier": "SWING_FIB", "scale_out_done": False}
    ohlcv = [{"o": 400, "h": 425, "l": 395, "c": 420, "v": 1e6}] * 65
    ok = enrich_position_from_buy_history(
        pos,
        "ETN",
        "US",
        ohlcv=ohlcv,
        history_path=hist,
    )
    assert ok is True
    assert pos.get("strategy_type") == "SWING_FIB"
    assert float(pos.get("buy_time", 0)) > 0
    assert float(pos.get("entry_fib_level", 0)) > 0
    assert float(pos.get("entry_initial_risk_1r", 0)) > 0


def test_position_needs_enrich_for_swing_without_fib():
    assert position_needs_history_enrich(
        {"tier": "SWING_FIB", "strategy_type": "SWING_FIB", "buy_date": "2026-06-04"}
    )


def test_find_last_buy_row(tmp_path: Path):
    hist = tmp_path / "h.json"
    hist.write_text(
        json.dumps(
            [
                {"market": "US", "ticker": "X", "side": "SELL", "price": 1},
                {"market": "US", "ticker": "ETN", "side": "BUY", "price": 420.0, "reason": "SWING_FIB"},
            ]
        ),
        encoding="utf-8",
    )
    row = find_last_buy_row("ETN", "US", history_path=hist)
    assert row is not None
    assert is_swing_trade_reason(row.get("reason"))
