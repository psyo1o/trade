# -*- coding: utf-8 -*-
"""KIS 전역 rate limiter — 슬라이딩 윈도우."""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from api import kis_rate_limit as rl


class TestKisRateLimit(unittest.TestCase):
    def test_min_interval_inverse_of_mps(self):
        with patch.object(rl, "max_calls_per_sec", return_value=10.0):
            self.assertAlmostEqual(rl.min_interval_sec(), 0.1)

    def test_wait_for_slot_serializes_burst(self):
        with patch.object(rl, "max_calls_per_sec", return_value=2.0):
            rl._window_hits.clear()
            rl._last_mono = 0.0
            t0 = time.monotonic()
            rl.wait_for_slot()
            rl.wait_for_slot()
            rl.wait_for_slot()
            elapsed = time.monotonic() - t0
            self.assertGreaterEqual(elapsed, 0.8)


if __name__ == "__main__":
    unittest.main()
