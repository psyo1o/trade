#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
매매 기록 사후 분석 — 운영 봇과 완전 분리.

  py -3.11 analysis/run_analysis.py
  py -3.11 analysis/run_analysis.py --no-fetch   # 캐시만 사용
  py -3.11 analysis/run_analysis.py --refresh    # OHLCV 캐시 무시
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trade_review.config import (  # noqa: E402
    BOT_ROOT,
    CACHE_DIR,
    ERAS_PATH,
    OUTPUT_DIR,
    TRADE_HISTORY_PATH,
)
from trade_review.load_trades import build_round_trips, load_events  # noqa: E402
from trade_review.post_exit import enrich_trips  # noqa: E402
from trade_review.report import export_all  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="trade_history 사후 분석 (봇 비침범)")
    ap.add_argument("--history", type=Path, default=TRADE_HISTORY_PATH)
    ap.add_argument("--output", type=Path, default=OUTPUT_DIR)
    ap.add_argument("--no-fetch", action="store_true", help="OHLCV 신규 조회 생략(캐시만)")
    ap.add_argument("--refresh", action="store_true", help="OHLCV 캐시 삭제 후 재조회")
    args = ap.parse_args()

    if not args.history.is_file():
        print(f"[ERR] trade_history 없음: {args.history}")
        return 1

    if args.refresh and CACHE_DIR.is_dir():
        shutil.rmtree(CACHE_DIR)
        print(f"[cache] 삭제: {CACHE_DIR}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)

    print(f"bot root: {BOT_ROOT}")
    print(f"history: {args.history}")
    events = load_events(args.history)
    print(f"  이벤트 {len(events)}건 (BUY+SELL)")

    trips, orphans = build_round_trips(events, eras_path=ERAS_PATH)
    print(f"  라운드트립 {len(trips)}건, 고아 매도 {len(orphans)}건")

    if args.no_fetch:
        print("[skip] OHLCV fetch (--no-fetch)")
    else:
        print("매도 후 주가 조회 중 (analysis/cache/)...")
        enrich_trips(trips, use_cache=True)

    paths = export_all(args.output, trips, orphans, ERAS_PATH)
    print(f"\n[OK] {datetime.now():%Y-%m-%d %H:%M:%S}")
    for k, p in paths.items():
        print(f"   [{k}] {p}")
    print(f"\n리포트: {paths['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
