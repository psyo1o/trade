# -*- coding: utf-8 -*-
"""merge_disk_if_newer + persist_position_set 경합 회귀."""
from __future__ import annotations

from pathlib import Path

from execution.guard import load_state, save_state
from execution import ledger_apply


def test_merge_disk_keeps_bot_stats_when_gui_bumped_gen(tmp_path: Path) -> None:
    """GUI가 state_gen만 올린 뒤 봇 청산 stats 저장 시 승/패가 역행하지 않아야 한다."""
    state_path = tmp_path / "bot_state.json"
    save_state(
        state_path,
        {
            "state_gen": 0,
            "positions": {"AAPL": {"buy_p": 100.0, "qty": 1}},
            "stats": {"wins": 2, "losses": 1, "total_profit": 5.0},
        },
    )
    gui_state = load_state(state_path)
    gui_state["positions"]["AAPL"]["max_p"] = 110.0
    save_state(state_path, gui_state)

    bot_state = load_state(state_path)
    bot_state["state_gen"] = int(bot_state.get("state_gen", 0) or 0) - 1
    bot_state.setdefault("stats", {})["wins"] = 3
    bot_state["stats"]["total_profit"] = 12.0
    bot_state["positions"].pop("AAPL", None)

    ok = ledger_apply.persist_position_remove(
        bot_state,
        "AAPL",
        context="US EXIT test",
        state_path=state_path,
    )
    assert ok is True
    latest = load_state(state_path)
    assert latest["stats"]["wins"] == 3
    assert float(latest["stats"]["total_profit"]) == 12.0
    assert "AAPL" not in latest.get("positions", {})


def test_persist_survives_newer_disk_positions(tmp_path: Path) -> None:
    """GUI가 state_gen을 올린 뒤 봇이 신규 매수 등록해도 positions가 유지되어야 한다."""
    state_path = tmp_path / "bot_state.json"
    disk_state = {
        "state_gen": 2,
        "positions": {"ANET": {"buy_p": 100.0, "qty": 1}},
        "cooldown": {},
    }
    save_state(state_path, disk_state)

    bot_state = {
        "state_gen": 1,
        "positions": {"ANET": {"buy_p": 100.0, "qty": 1}},
        "order_idempotency": {"buy:US:ETN:0": {"status": "filled"}},
    }
    payload = {"buy_p": 420.33, "qty": 1.0, "sl_p": 398.64, "tier": "SWING_FIB"}

    ok = ledger_apply.persist_position_set(
        bot_state,
        "ETN",
        payload,
        context="US BUY TWAP",
        state_path=state_path,
    )

    assert ok is True
    latest = load_state(state_path)
    assert "ETN" in latest.get("positions", {})
    assert latest["positions"]["ETN"]["buy_p"] == 420.33
    assert "ANET" in latest.get("positions", {})
    assert int(latest.get("state_gen", 0)) > 2
