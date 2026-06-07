# -*- coding: utf-8 -*-
"""market_cycles — 표준 import·rb 상수 보정."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CYCLES = ROOT / "execution" / "market_cycles"

IMPORTS = """import time
from datetime import datetime

import pandas as pd
import pytz

"""

CONST_MAP = {
    "kr": [
        ("STATE_PATH", "rb.STATE_PATH"),
        ("MAX_POSITIONS_KR", "rb.MAX_POSITIONS_KR"),
        ("BUY_WINDOW_MINUTES_BEFORE_CLOSE", "rb.BUY_WINDOW_MINUTES_BEFORE_CLOSE"),
        ("INDEX_CRASH_KR", "rb.INDEX_CRASH_KR"),
        ("WEATHER_LABEL_BEAR", "rb.WEATHER_LABEL_BEAR"),
        ("AI_FALSE_BREAKOUT_THRESHOLD,", "rb.AI_FALSE_BREAKOUT_THRESHOLD,"),
        ("normalize_ticker,", "rb.normalize_ticker,"),
    ],
    "us": [
        ("STATE_PATH", "rb.STATE_PATH"),
        ("MAX_POSITIONS_US", "rb.MAX_POSITIONS_US"),
        ("BUY_WINDOW_MINUTES_BEFORE_CLOSE", "rb.BUY_WINDOW_MINUTES_BEFORE_CLOSE"),
        ("INDEX_CRASH_US", "rb.INDEX_CRASH_US"),
        ("WEATHER_LABEL_BEAR", "rb.WEATHER_LABEL_BEAR"),
        ("AI_FALSE_BREAKOUT_THRESHOLD,", "rb.AI_FALSE_BREAKOUT_THRESHOLD,"),
        ("normalize_ticker,", "rb.normalize_ticker,"),
    ],
    "coin": [
        ("STATE_PATH", "rb.STATE_PATH"),
        ("MAX_POSITIONS_COIN", "rb.MAX_POSITIONS_COIN"),
        ("BUY_WINDOW_MINUTES_BEFORE_CLOSE", "rb.BUY_WINDOW_MINUTES_BEFORE_CLOSE"),
        ("INDEX_CRASH_COIN", "rb.INDEX_CRASH_COIN"),
        ("AI_FALSE_BREAKOUT_THRESHOLD_COIN", "rb.AI_FALSE_BREAKOUT_THRESHOLD_COIN"),
    ],
}


def patch_file(path: Path, name: str) -> None:
    text = path.read_text(encoding="utf-8")
    if "import time\n" not in text:
        text = text.replace(
            "from execution.market_cycles.context import TradingCycleContext\n\n",
            "from execution.market_cycles.context import TradingCycleContext\n\n" + IMPORTS,
        )
    for old, new in CONST_MAP[name]:
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print("patched", path.name)


def main() -> None:
    for name in ("kr", "us", "coin"):
        patch_file(CYCLES / f"{name}_cycle.py", name)


if __name__ == "__main__":
    main()
