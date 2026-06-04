# -*- coding: utf-8 -*-
"""merge_disk_if_newer + persist_position_set 경합 회귀."""
from __future__ import annotations

import json
from pathlib import Path

from execution.guard import load_state, save_state
from execution import ledger_apply


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
