# -*- coding: utf-8 -*-
"""market_cycles — rb. 접두 누락·사이클 로컬 보정."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ROOT / "execution" / "market_cycles" / "kr_cycle.py",
    ROOT / "execution" / "market_cycles" / "us_cycle.py",
    ROOT / "execution" / "market_cycles" / "coin_cycle.py",
]

REPLACEMENTS = [
    (", _to_float", ", rb._to_float"),
    (", get_cached_ohlcv,", ", rb.get_cached_ohlcv,"),
    ("WEATHER_LABEL_BEAR", "rb.WEATHER_LABEL_BEAR"),
    ("BINANCE_UNIVERSE_TOP", "rb.BINANCE_UNIVERSE_TOP"),
    ("UPBIT_UNIVERSE_TOP", "rb.UPBIT_UNIVERSE_TOP"),
]

PREAMBLE_LINE = (
    "    _alpha_target_vol = float(rb.config.get(\"alpha_target_vol\", 0.02))\n"
)
PREAMBLE_AFTER = "    _buy_cycle_tag = ctx.buy_cycle_tag\n"


def main() -> None:
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        if "import traceback\n" not in text:
            text = text.replace(
                "import pytz\n\n",
                "import pytz\nimport traceback\n\n",
            )
        if PREAMBLE_LINE.strip() not in text:
            text = text.replace(PREAMBLE_AFTER, PREAMBLE_AFTER + PREAMBLE_LINE)
        for old, new in REPLACEMENTS:
            if old == "WEATHER_LABEL_BEAR":
                text = re.sub(r"(?<!\.)WEATHER_LABEL_BEAR", new, text)
            else:
                text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
        print("fixed", path.name)


if __name__ == "__main__":
    main()
