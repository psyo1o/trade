# -*- coding: utf-8 -*-
"""One-shot B-1 merge into run_bot.py (완료됨 — 2026-06 재실행 불필요).

B-1 사이클 코드는 ``execution/market_cycles/{kr,us,coin}_cycle.py`` 에 있으며,
과거 중간 산출물 ``services/_market_cycles_extracted.py`` 는 제거되었다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRACTED = ROOT / "services" / "_market_cycles_extracted.py"


def main() -> None:
    if not EXTRACTED.is_file():
        print(
            "B-1 merge already complete — use execution/market_cycles/*.py",
            file=sys.stderr,
        )
        raise SystemExit(0)
    raise SystemExit(f"unexpected {EXTRACTED} — remove or archive before re-running")


if __name__ == "__main__":
    main()
