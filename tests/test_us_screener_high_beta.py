# -*- coding: utf-8 -*-
"""미장 고베타 유니버스 — 섹터 배제·라운드로빈·NDX 위키 파싱."""
from __future__ import annotations

import unittest

import pandas as pd

from us_screener import (
    NDX_WIKI_URLS,
    SECTOR_COL_CANDIDATES,
    SYMBOL_COL_CANDIDATES,
    _find_column,
    _normalize_wiki_col_name,
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

    def test_ndx_wiki_url_uses_nasdaq_casing(self):
        self.assertTrue(
            any("List_of_NASDAQ-100_companies" in u for u in NDX_WIKI_URLS),
            msg="위키 NDX 구성 목록 URL 대소문자(NASDAQ) 필수 — Nasdaq 표기는 404",
        )

    def test_normalize_wiki_col_strips_footnote(self):
        self.assertEqual(_normalize_wiki_col_name("ICB Industry[1]"), "ICB Industry")
        self.assertEqual(_normalize_wiki_col_name("GICS Sector"), "GICS Sector")

    def test_find_column_ticker_and_icb_industry(self):
        table = pd.DataFrame(
            {
                "Ticker": ["AAPL", "NVDA"],
                "Company": ["Apple", "Nvidia"],
                "ICB Industry[1]": ["Technology", "Technology"],
                "ICB Subsector[1]": ["Hardware", "Semiconductors"],
            }
        )
        self.assertEqual(_find_column(table, SYMBOL_COL_CANDIDATES), "Ticker")
        self.assertEqual(_find_column(table, SECTOR_COL_CANDIDATES), "ICB Industry[1]")


if __name__ == "__main__":
    unittest.main()
