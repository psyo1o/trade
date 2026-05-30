# -*- coding: utf-8 -*-
"""OHLCV 확보 파이프라인 — 최소 봉 수·폴백 체인·매매 로직 연동 회귀."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

# 프로젝트 루트
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.ohlcv_store import OHLCV_MIN_BARS, ohlcv_len_ok


def _ohlcv_rows(n: int, close: float = 50_000.0) -> list:
    return [
        {"o": close, "h": close * 1.01, "l": close * 0.99, "c": close, "v": 1_000_000.0}
        for _ in range(n)
    ]


class TestOhlcvMinBarsContract(unittest.TestCase):
    """문서·코드가 기대하는 최소 봉 수."""

    def test_contract_values(self):
        self.assertEqual(OHLCV_MIN_BARS["v8_entry"], 120)
        self.assertEqual(OHLCV_MIN_BARS["swing"], 60)
        self.assertEqual(OHLCV_MIN_BARS["cache_target"], 200)

    def test_ohlcv_len_ok_helper(self):
        self.assertFalse(ohlcv_len_ok(_ohlcv_rows(119), "v8_entry"))
        self.assertTrue(ohlcv_len_ok(_ohlcv_rows(120), "v8_entry"))
        self.assertTrue(ohlcv_len_ok(_ohlcv_rows(200), "cache_target"))


class TestV8EntryOhlcvGate(unittest.TestCase):
    def test_calculate_pro_signals_blocks_under_120(self):
        from strategy.rules import calculate_pro_signals

        ok, _, reason = calculate_pro_signals(
            _ohlcv_rows(119),
            "",
            ticker="005930",
            name="테스트",
            idx=1,
            total=1,
        )
        self.assertFalse(ok)
        self.assertIn("120", str(reason))

    def test_calculate_pro_signals_not_blocked_at_120_bars(self):
        from strategy.rules import calculate_pro_signals

        ok, _, reason = calculate_pro_signals(
            _ohlcv_rows(120),
            "",
            ticker="005930",
            name="테스트",
            idx=1,
            total=1,
        )
        if not ok:
            self.assertNotIn("120일선 데이터 부족", str(reason))


class TestSwingEntryOhlcvGate(unittest.TestCase):
    def test_swing_blocks_under_60(self):
        from strategy.rules import check_swing_entry

        df = pd.DataFrame(_ohlcv_rows(59))
        ok, _, reason = check_swing_entry(df)
        self.assertFalse(ok)
        self.assertIn("60", reason)

    def test_swing_accepts_60_bars(self):
        from strategy.rules import check_swing_entry

        df = pd.DataFrame(_ohlcv_rows(60))
        ok, _, reason = check_swing_entry(df)
        self.assertNotIn("봉 부족", str(reason))


class TestGetCachedOhlcvFallbackChain(unittest.TestCase):
    """get_cached_ohlcv — KIS 부족 시 pykrx로 200봉 보강(모킹)."""

    def setUp(self):
        import run_bot

        run_bot._ohlcv_cache.clear()

    def tearDown(self):
        import run_bot

        run_bot._ohlcv_cache.clear()

    @patch("run_bot.kis_equities_weekend_suppress_window_kst", return_value=False)
    @patch("run_bot.get_ohlcv_yfinance", return_value=[])
    @patch("run_bot.get_ohlcv_stooq", return_value=[])
    @patch("run_bot.get_ohlcv_pykrx")
    @patch("run_bot.get_ohlcv_kis_domestic_daily")
    @patch("utils.ohlcv_store.load_disk_ohlcv", return_value=None)
    def test_kr_kis_then_pykrx_reaches_200(
        self,
        _disk,
        mock_kis,
        mock_pykrx,
        _stooq,
        _yf,
        _wknd,
    ):
        import run_bot

        mock_kis.return_value = _ohlcv_rows(80)
        mock_pykrx.return_value = _ohlcv_rows(220)
        broker = MagicMock()
        out = run_bot.get_cached_ohlcv("005930", broker=broker)
        self.assertGreaterEqual(len(out), 200)
        mock_kis.assert_called()
        mock_pykrx.assert_called()
        self.assertTrue(ohlcv_len_ok(out, "v8_entry"))
        self.assertTrue(ohlcv_len_ok(out, "cache_target"))

    @patch("run_bot.kis_equities_weekend_suppress_window_kst", return_value=False)
    @patch("run_bot.get_ohlcv_yfinance")
    @patch("run_bot.get_ohlcv_stooq", return_value=[])
    @patch("api.kis_api.get_ohlcv_kis_us_daily")
    @patch("utils.ohlcv_store.load_disk_ohlcv", return_value=None)
    @patch("api.kis_api.broker_us", new_callable=MagicMock)
    def test_us_kis_then_yfinance_reaches_200(
        self,
        _bus,
        _disk,
        mock_kis_us,
        _stooq,
        mock_yf,
        _wknd,
    ):
        import run_bot

        mock_kis_us.return_value = _ohlcv_rows(150)
        mock_yf.return_value = _ohlcv_rows(210)
        out = run_bot.get_cached_ohlcv("AAPL", broker=None)
        self.assertGreaterEqual(len(out), 200)
        mock_kis_us.assert_called()
        mock_yf.assert_called()

    @patch("utils.ohlcv_store.load_disk_ohlcv")
    def test_disk_cache_short_circuits_fetch(self, mock_disk):
        import run_bot

        mock_disk.return_value = _ohlcv_rows(200)
        broker = MagicMock()
        with patch("run_bot.get_ohlcv_kis_domestic_daily") as mock_kis:
            out = run_bot.get_cached_ohlcv("005930", broker=broker)
            self.assertEqual(len(out), 200)
            mock_kis.assert_not_called()


class TestExitLinesWithSyntheticOhlcv(unittest.TestCase):
    def test_v8_exit_line_with_60_bars(self):
        from strategy.rules import get_final_exit_price

        buy = 10_000.0
        pos = {"buy_p": buy, "max_p": buy, "sl_p": buy * 0.9, "current_atr": 200.0}
        line = get_final_exit_price("005930", buy, pos, _ohlcv_rows(60, buy))
        self.assertGreater(line, 0)

    def test_swing_exit_display_with_60_bars(self):
        from strategy.rules import get_swing_exit_display_price

        buy = 100.0
        pos = {
            "buy_p": buy,
            "entry_fib_level": 95.0,
            "strategy_type": "SWING_FIB",
            "max_p": buy,
        }
        line = get_swing_exit_display_price(
            buy, pos, _ohlcv_rows(60, buy), market="KR", ticker="005930"
        )
        self.assertGreater(line, 0)


@unittest.skipUnless(
    os.environ.get("BOT_OHLCV_LIVE_TEST", "1") == "1",
    "실조회 비활성: BOT_OHLCV_LIVE_TEST=0",
)
class TestOhlcvLiveProviders(unittest.TestCase):
    """네트워크·KRX/pykrx 실조회 — CI/로컬 기본 ON, 끄려면 BOT_OHLCV_LIVE_TEST=0."""

    def test_pykrx_samsung_meets_v8_and_cache(self):
        from strategy.rules import get_ohlcv_pykrx

        rows = get_ohlcv_pykrx("005930")
        self.assertGreaterEqual(len(rows), OHLCV_MIN_BARS["cache_target"])
        self.assertTrue(ohlcv_len_ok(rows, "v8_entry"))
        self.assertTrue(ohlcv_len_ok(rows, "swing"))
        c = float(rows[-1]["c"])
        self.assertGreater(c, 0)

    def test_get_cached_ohlcv_kr_live_pykrx_path(self):
        import run_bot

        run_bot._ohlcv_cache.clear()
        with patch("run_bot.get_ohlcv_kis_domestic_daily", return_value=[]):
            with patch("run_bot.get_ohlcv_yfinance", return_value=[]):
                with patch("utils.ohlcv_store.load_disk_ohlcv", return_value=None):
                    with patch(
                        "run_bot.kis_equities_weekend_suppress_window_kst",
                        return_value=True,
                    ):
                        out = run_bot.get_cached_ohlcv(
                            "005930", broker=MagicMock()
                        )
        self.assertGreaterEqual(len(out), OHLCV_MIN_BARS["v8_entry"])


if __name__ == "__main__":
    unittest.main()
