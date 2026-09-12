# -*- coding: utf-8 -*-
"""VKOSPI 국장 매수 오토 블락 — 동적 20MA 스파이크."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from api.macro_data import (
    _coerce_vkospi,
    fetch_vkospi,
    vkospi_is_dynamic_spike,
    vkospi_ma20,
)


class TestVkospiFetch(unittest.TestCase):
    def test_coerce_rejects_kospi_level(self):
        self.assertIsNone(_coerce_vkospi("6826.33"))
        self.assertIsNone(_coerce_vkospi("0"))
        self.assertAlmostEqual(_coerce_vkospi("25.0"), 25.0)
        self.assertAlmostEqual(_coerce_vkospi("55.28"), 55.28)
        self.assertAlmostEqual(_coerce_vkospi("89.07"), 89.07)

    @patch("api.macro_data._vkospi_from_krx", return_value=None)
    @patch("api.macro_data._vkospi_from_kis", return_value=55.28)
    def test_fetch_uses_kis_index(self, *_mocks):
        import api.macro_data as md

        md._vkospi_cache = None
        self.assertAlmostEqual(fetch_vkospi(), 55.28)
        md._vkospi_cache = None

    @patch("api.macro_data._vkospi_from_krx", side_effect=RuntimeError("x"))
    @patch("api.macro_data._vkospi_from_kis", side_effect=RuntimeError("x"))
    def test_fetch_swallows_errors(self, *_mocks):
        import api.macro_data as md

        md._vkospi_cache = None
        self.assertIsNone(fetch_vkospi())
        md._vkospi_cache = None

    def test_kis_parses_bstp_nmix_prpr(self):
        import api.macro_data as md

        class _Broker:
            access_token = "tok"
            api_key = "k"
            api_secret = "s"
            base_url = "https://openapi.koreainvestment.com:9443"

        fake_res = type(
            "R",
            (),
            {
                "status_code": 200,
                "json": lambda self: {
                    "rt_cd": "0",
                    "output": {"bstp_nmix_prpr": "55.28"},
                },
            },
        )()
        with patch("api.kis_api.broker_kr", _Broker()), patch(
            "api.kis_api.KIS_TOKEN", "tok"
        ), patch(
            "api.kis_api._cfg", {"kis_key": "k", "kis_secret": "s"}
        ), patch(
            "api.macro_data.requests.get", return_value=fake_res
        ):
            self.assertAlmostEqual(md._vkospi_from_kis(), 55.28)

    def test_fetch_does_not_use_realized_vol_fallback(self):
        import inspect

        import api.macro_data as md

        src = inspect.getsource(md.fetch_vkospi)
        self.assertIn("_vkospi_from_kis", src)
        self.assertNotIn("_vkospi_from_kpi200_realized", src)
        self.assertNotIn("_vkospi_from_pykrx", src)


class TestVkospiDynamicSpike(unittest.TestCase):
    def test_ma20_uses_available_bars(self):
        self.assertAlmostEqual(vkospi_ma20([10.0, 20.0, 30.0]), 20.0)
        closes = [10.0] * 25
        closes[-1] = 30.0
        # last 20: 19*10 + 30 = 220 → 11.0
        self.assertAlmostEqual(vkospi_ma20(closes), 11.0)

    def test_spike_requires_ma_and_floor(self):
        # 비율 스파이크지만 절대값 20 미만 → 차단 안 함
        self.assertFalse(vkospi_is_dynamic_spike(15.0, 10.0))
        # 20 이상이지만 MA 대비 30% 미만
        self.assertFalse(vkospi_is_dynamic_spike(25.0, 20.0))
        # 둘 다 충족
        self.assertTrue(vkospi_is_dynamic_spike(27.0, 20.0))
        self.assertTrue(vkospi_is_dynamic_spike(26.1, 20.0))


class TestVkospiKrBlock(unittest.TestCase):
    def test_kr_only_lock_on_dynamic_spike(self):
        import run_bot as rb

        snap = {
            "enabled": True,
            "market_buy_allowed": {"KR": True, "US": True, "COIN": True},
            "market_buy_block_reason": {"KR": "", "US": "", "COIN": ""},
        }
        with patch(
            "api.macro_data.vkospi_dynamic_spike_state",
            return_value={"current": 27.0, "ma20": 20.0},
        ), patch("builtins.print") as mock_print:
            rb._apply_vkospi_kr_buy_block(snap)
        self.assertFalse(snap["market_buy_allowed"]["KR"])
        self.assertTrue(snap["market_buy_allowed"]["US"])
        self.assertTrue(snap["market_buy_allowed"]["COIN"])
        mock_print.assert_called()
        msg = mock_print.call_args[0][0]
        self.assertIn("VKOSPI 단기 급등 감지", msg)
        self.assertIn("현재: 27.0", msg)
        self.assertIn("20일 평균: 20.0", msg)
        self.assertIn("국장 신규 매수 강제 차단", msg)

    def test_high_level_without_spike_does_not_lock(self):
        """옛 고정 25 룰 폐기 — 레벨만 높아도 MA 대비 급등 아니면 통과."""
        import run_bot as rb

        snap = {"market_buy_allowed": {"KR": True, "US": True, "COIN": True}}
        with patch(
            "api.macro_data.vkospi_dynamic_spike_state",
            return_value={"current": 28.0, "ma20": 26.0},
        ), patch("builtins.print") as mock_print:
            rb._apply_vkospi_kr_buy_block(snap)
        self.assertTrue(snap["market_buy_allowed"]["KR"])
        mock_print.assert_not_called()

    def test_low_level_ratio_spike_ignored(self):
        import run_bot as rb

        snap = {"market_buy_allowed": {"KR": True, "US": True, "COIN": True}}
        with patch(
            "api.macro_data.vkospi_dynamic_spike_state",
            return_value={"current": 15.0, "ma20": 10.0},
        ), patch("builtins.print") as mock_print:
            rb._apply_vkospi_kr_buy_block(snap)
        self.assertTrue(snap["market_buy_allowed"]["KR"])
        mock_print.assert_not_called()

    def test_fetch_failure_is_silent(self):
        import run_bot as rb

        snap = {"market_buy_allowed": {"KR": True, "US": True, "COIN": True}}
        with patch(
            "api.macro_data.vkospi_dynamic_spike_state",
            side_effect=RuntimeError("net"),
        ), patch("builtins.print") as mock_print:
            rb._apply_vkospi_kr_buy_block(snap)
        self.assertTrue(snap["market_buy_allowed"]["KR"])
        mock_print.assert_not_called()

    def test_missing_state_is_silent(self):
        import run_bot as rb

        snap = {"market_buy_allowed": {"KR": True, "US": True, "COIN": True}}
        with patch("api.macro_data.vkospi_dynamic_spike_state", return_value=None), patch(
            "builtins.print"
        ) as mock_print:
            rb._apply_vkospi_kr_buy_block(snap)
        self.assertTrue(snap["market_buy_allowed"]["KR"])
        mock_print.assert_not_called()

    def test_only_wired_on_kr_buy_cycle_not_market_context(self):
        import inspect

        import run_bot as rb
        from execution.market_cycles.kr_buy_cycle import run_kr_buy_cycle
        from execution.market_cycles.us_buy_cycle import run_us_buy_cycle

        ctx_src = inspect.getsource(rb._build_market_context)
        self.assertNotIn("_apply_vkospi_kr_buy_block", ctx_src)
        self.assertNotIn("신규 매수 차단", ctx_src)
        self.assertIn("_apply_vkospi_kr_buy_block", inspect.getsource(run_kr_buy_cycle))
        self.assertIn("미장 신규 매수 강제 차단", inspect.getsource(run_us_buy_cycle))


if __name__ == "__main__":
    unittest.main()
