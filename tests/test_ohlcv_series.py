# -*- coding: utf-8 -*-
"""OHLCV 정렬·유효성 — KIS 꼬리 봉·생존신고/매도 루프 공통."""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.ohlcv_store import (
    finalize_ohlcv_daily,
    normalize_ohlcv_series,
    ohlcv_series_valid,
    select_validated_equity_ohlcv,
    select_validated_kr_ohlcv,
)


def _bars_with_dates(closes: list[tuple[str, float]]) -> list:
    return [
        {"d": d, "o": c, "h": c, "l": c, "c": c, "v": 1.0}
        for d, c in closes
    ]


class TestOhlcvSeriesNormalize(unittest.TestCase):
    def test_sort_and_dedupe_by_date(self):
        raw = _bars_with_dates(
            [
                ("20260602", 100.0),
                ("20260604", 120.0),
                ("20260603", 110.0),
                ("20260604", 121.0),
            ]
        )
        out = normalize_ohlcv_series(raw)
        self.assertEqual([b["d"] for b in out], ["20260602", "20260603", "20260604"])
        self.assertEqual(out[-1]["c"], 121.0)

    def test_rejects_stale_tail_without_dates(self):
        """꼬리가 옛날 저가로 끝나면 비정상(ANET 캐시 꼬임 패턴)."""
        highs = [{"o": 170, "h": 170, "l": 170, "c": 170.0, "v": 1.0} for _ in range(55)]
        tail = [{"o": 125, "h": 125, "l": 125, "c": 125.0, "v": 1.0} for _ in range(5)]
        series = highs + tail
        self.assertFalse(ohlcv_series_valid(series))

    def test_accepts_recent_tail_without_dates(self):
        closes = [160.0 + i * 0.1 for i in range(58)] + [
            162.0,
            161.5,
            162.2,
            161.8,
            162.4,
        ]
        series = [{"o": c, "h": c, "l": c, "c": c, "v": 1.0} for c in closes]
        self.assertTrue(ohlcv_series_valid(series))


class TestSelectValidatedEquityOhlcv(unittest.TestCase):
    def _recent_yf(self) -> list:
        """Yahoo 2026-06 초 ANET 패턴 — 최신이 꼬리."""
        return _bars_with_dates(
            [
                ("20260529", 159.47),
                ("20260601", 170.68),
                ("20260602", 175.33),
                ("20260603", 174.37),
                ("20260604", 158.11),
            ]
        )

    def test_picks_yfinance_when_kis_tail_stale(self):
        """꼬린 KIS(옛날 일자 꼬리) vs 최신 yfinance → yfinance."""
        kis_bad = normalize_ohlcv_series(
            _bars_with_dates(
                [(f"202401{(i % 28) + 1:02d}", 100.0 + i) for i in range(195)]
                + [
                    ("20260112", 123.42),
                    ("20260113", 129.93),
                    ("20260114", 125.09),
                    ("20260115", 130.59),
                    ("20260116", 129.83),
                ]
            )
        )
        yf_good = normalize_ohlcv_series(
            _bars_with_dates(
                [(f"202508{(i % 28) + 1:02d}", 120.0 + i * 0.1) for i in range(180)]
                + [
                    ("20260529", 159.47),
                    ("20260601", 170.68),
                    ("20260602", 175.33),
                    ("20260603", 174.37),
                    ("20260604", 158.11),
                ]
            )
        )
        out = select_validated_equity_ohlcv(kis_bad, yf_good, ticker="ANET")
        self.assertEqual(out[-1]["c"], 158.11)

    def test_picks_kis_when_aligned_with_yfinance(self):
        yf = self._recent_yf()
        kis = list(yf)
        out = select_validated_equity_ohlcv(kis, yf, ticker="ANET")
        self.assertEqual(out[-1]["c"], 158.11)


class TestKrOhlcvFinalize(unittest.TestCase):
    def test_finalize_sorts_kis_style_rows(self):
        raw = _bars_with_dates(
            [(f"202605{(i % 28) + 1:02d}", 60000.0 + i) for i in range(12)]
            + [
                ("20260604", 70000.0),
                ("20260602", 68000.0),
                ("20260603", 69000.0),
            ]
        )
        out = finalize_ohlcv_daily(raw, ticker="005930", source="test")
        self.assertEqual(len(out), 15)
        self.assertEqual(out[-3]["d"], "20260602")
        self.assertEqual(out[-1]["d"], "20260604")
        self.assertEqual(out[-1]["c"], 70000.0)

    def test_kr_select_pykrx_when_kis_stale(self):
        kis_bad = normalize_ohlcv_series(
            _bars_with_dates([(f"202401{(i % 28) + 1:02d}", 50000.0 + i) for i in range(200)])
        )
        pykrx_ok = normalize_ohlcv_series(
            _bars_with_dates(
                [(f"202508{(i % 28) + 1:02d}", 60000.0 + i) for i in range(195)]
                + [
                    ("20260602", 68000.0),
                    ("20260603", 69000.0),
                    ("20260604", 70000.0),
                ]
            )
        )
        out = select_validated_kr_ohlcv(kis_bad, pykrx_ok, ticker="005930")
        self.assertEqual(out[-1]["c"], 70000.0)


class TestSwingHalfScaleOutFlag(unittest.TestCase):
    def test_post_partial_sets_scale_out_done(self):
        from execution.scale_out import post_partial_ledger

        pos = {"buy_p": 100.0, "qty": 4, "sl_p": 95.0, "max_p": 110.0}
        out = post_partial_ledger(pos, 2.0, 108.0, 4.0, set_scale_out_done=True)
        self.assertTrue(out.get("scale_out_done"))
        self.assertEqual(out.get("qty"), 2)


if __name__ == "__main__":
    unittest.main()
