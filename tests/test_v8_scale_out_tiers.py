# -*- coding: utf-8 -*-
"""V8 다단계 Scale-Out — 2차(6×ATR) 트리거."""
from __future__ import annotations

import unittest

from execution.scale_out import (
    SCALE_OUT_ENTRY_ATR_MULT,
    SCALE_OUT_SECOND_ENTRY_ATR_MULT,
    position_scale_out_done,
    position_second_scale_out_done,
    post_partial_ledger,
    scale_out_price_target_hit,
    scale_out_second_trigger_ok,
)


class TestV8ScaleOutTiers(unittest.TestCase):
    def test_first_tier_at_3atr(self):
        buy = 100.0
        atr = 2.0
        hit, mode, tgt = scale_out_price_target_hit(
            buy, buy + atr * SCALE_OUT_ENTRY_ATR_MULT, atr
        )
        self.assertTrue(hit)
        self.assertEqual(mode, "entry_atr")
        self.assertAlmostEqual(tgt, buy + atr * SCALE_OUT_ENTRY_ATR_MULT)

    def test_second_tier_requires_first_done(self):
        pos = {"buy_p": 100.0, "scale_out_done": False}
        ok, _, _ = scale_out_second_trigger_ok(pos, 100.0, 130.0, 2.0, 10_000_000.0)
        self.assertFalse(ok)

    def test_second_tier_at_6atr(self):
        buy = 100.0
        atr = 2.0
        pos = {"buy_p": buy, "scale_out_done": True, "second_scale_out_done": False}
        px = buy + atr * SCALE_OUT_SECOND_ENTRY_ATR_MULT + 0.01
        ok, mode, tgt = scale_out_second_trigger_ok(pos, buy, px, atr, 10_000_000.0)
        self.assertTrue(ok)
        self.assertEqual(mode, "entry_atr")
        self.assertAlmostEqual(tgt, buy + atr * SCALE_OUT_SECOND_ENTRY_ATR_MULT)

    def test_post_partial_sets_second_flag(self):
        pos = {"buy_p": 100.0, "qty": 10, "scale_out_done": True}
        out = post_partial_ledger(
            pos, 5.0, 110.0, 10.0, set_scale_out_done=False, set_second_scale_out_done=True
        )
        self.assertTrue(position_scale_out_done(out))
        self.assertTrue(position_second_scale_out_done(out))
        self.assertEqual(out["qty"], 5)


if __name__ == "__main__":
    unittest.main()
