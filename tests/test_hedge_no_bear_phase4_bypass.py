# -*- coding: utf-8 -*-
"""BEAR/Phase4: hedge buy bypass removed."""
from unittest.mock import patch

import run_bot as rb


def test_phase4_hedge_only_active_always_false():
    snap = {"market_buy_allowed": {"KR": False, "US": False, "COIN": False}}
    assert rb._phase4_hedge_only_active(snap, "KR") is False
    assert rb._phase4_hedge_only_active(snap, "US") is False
    assert rb._phase4_hedge_only_active(snap, "COIN") is False


def test_apply_phase4_hedge_buy_targets_blocks_all_when_not_allowed(capsys):
    targets = ["005930", "261240", "TLT"]
    snap = {
        "market_buy_allowed": {"KR": False},
        "market_buy_block_reason": {"KR": "test-block"},
    }
    out = rb._apply_phase4_hedge_buy_targets(targets, snap, "KR")
    assert out == []
    logged = capsys.readouterr().out
    assert "전면 차단" in logged
    assert "헷지 포함" in logged


def test_apply_phase4_hedge_buy_targets_passthrough_when_allowed():
    targets = ["005930", "261240"]
    snap = {"market_buy_allowed": {"KR": True}}
    out = rb._apply_phase4_hedge_buy_targets(targets, snap, "KR")
    assert out == targets


def test_can_open_new_respecting_hedge_bypass_matches_can_open_new():
    hedge = "261240"
    state = {"positions": {}}
    with patch.object(rb, "can_open_new", return_value=False) as mocked:
        assert (
            rb._can_open_new_respecting_hedge_bypass(
                hedge, state, "KR", int(rb.MAX_POSITIONS_KR)
            )
            is False
        )
        mocked.assert_called_once_with(
            hedge, state, max_positions=int(rb.MAX_POSITIONS_KR)
        )

    with patch.object(rb, "can_open_new", return_value=True) as mocked:
        assert (
            rb._can_open_new_respecting_hedge_bypass(
                hedge, state, "KR", int(rb.MAX_POSITIONS_KR)
            )
            is True
        )
