# -*- coding: utf-8 -*-
"""섀도우 이벤트와 매매 내역을 읽어 규칙별 건수·반사실을 출력한다.

예::

    python tools/analyze_shadow.py
    python tools/analyze_shadow.py --csv shadow_report.csv

기본 경로는 저장소 루트의 ``shadow_events.jsonl``, ``trade_history.json``
(``trade_history.json`` 과 같은 데이터 디렉터리).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.shadow_report import analyze_files, write_csv  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="섀도우 규칙 로그 복기")
    parser.add_argument(
        "--events",
        default=str(ROOT / "shadow_events.jsonl"),
        help="shadow_events.jsonl 경로",
    )
    parser.add_argument(
        "--history",
        default=str(ROOT / "trade_history.json"),
        help="trade_history.json 경로",
    )
    parser.add_argument("--csv", default="", help="선택 CSV 출력 경로")
    args = parser.parse_args(argv)
    text, rows = analyze_files(args.events, args.history)
    sys.stdout.write(text)
    if args.csv:
        write_csv(args.csv, rows)
        print(f"CSV 저장: {args.csv} ({len(rows)}행)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
