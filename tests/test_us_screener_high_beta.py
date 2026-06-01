# -*- coding: utf-8 -*-
"""미장 고베타 유니버스 — 섹터 배제·라운드로빈."""
from __future__ import annotations

import unittest

from us_screener import (
    _round_robin_sector_fill,
    is_excluded_gics_sector,
)


class TestUsScreenerHighBeta(unittest.TestCase):
    def test_excluded_sectors(self):
        self.assertTrue(is_excluded_gics_sector("Utilities"))
        self.assertTrue(is_excluded_gics_sector("Consumer Staples"))
        self.assertTrue(is_excluded_gics_sector("Consumer Defensive"))
        self.assertTrue(is_excluded_gics_sector("Real Estate"))
        self.assertTrue(is_excluded_gics_sector("Basic Materials"))
        self.assertFalse(is_excluded_gics_sector("Healthcare"))
        self.assertFalse(is_excluded_gics_sector("Technology"))

    def test_round_robin_balances_sectors(self):
        rows = [
            ("A", "Healthcare", "healthcare"),
            ("B", "Healthcare", "healthcare"),
            ("C", "Energy", "energy"),
            ("D", "Financials", "financials"),
        ]
        caps = {"A": 300, "B": 200, "C": 150, "D": 100}
        universe: set[str] = set()
        picked = _round_robin_sector_fill(rows, caps, universe, slots=3)
        self.assertEqual(len(picked), 3)
        self.assertEqual(set(picked), {"A", "C", "D"})


if __name__ == "__main__":
    unittest.main()
