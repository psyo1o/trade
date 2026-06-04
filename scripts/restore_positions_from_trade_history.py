#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
``trade_history.json`` 최근 BUY 기준으로 ``bot_state.json`` positions 를 보강·복구.

Usage:
    python scripts/restore_positions_from_trade_history.py
    python scripts/restore_positions_from_trade_history.py ETN GM FAST
    python scripts/restore_positions_from_trade_history.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.guard import load_state, save_state
from strategy.rules import get_ohlcv_yfinance
from services.trade_history_ledger import (
    enrich_position_from_buy_history,
    find_last_buy_row,
    position_needs_history_enrich,
)


def _market_for(ticker: str) -> str:
    t = str(ticker or "").strip().upper()
    if t.startswith("KRW-") or t.startswith("USDT-"):
        return "COIN"
    if t.isdigit():
        return "KR"
    return "US"


def main() -> int:
    parser = argparse.ArgumentParser(description="매매내역 기준 장부 복구")
    parser.add_argument("tickers", nargs="*", help="티커 (생략 시 보강 필요 종목 전체)")
    parser.add_argument("--state", type=Path, default=ROOT / "bot_state.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state = load_state(args.state)
    positions = state.setdefault("positions", {})
    if not isinstance(positions, dict):
        print("❌ positions 가 dict 가 아닙니다.")
        return 1

    if args.tickers:
        targets = [str(t).strip().upper() for t in args.tickers if str(t).strip()]
    else:
        targets = [
            tk
            for tk, pos in positions.items()
            if position_needs_history_enrich(pos if isinstance(pos, dict) else {})
        ]

    if not targets:
        print("✅ 보강 대상 없음 (또는 티커 미지정)")
        return 0

    changed_any = False
    for ticker in targets:
        pos = positions.get(ticker)
        if not isinstance(pos, dict):
            print(f"  ⏭️ {ticker}: 장부에 없음 — 스킵")
            continue
        hist = find_last_buy_row(ticker, _market_for(ticker))
        if not hist:
            print(f"  ⚠️ {ticker}: trade_history BUY 없음 — 스킵")
            continue
        ohlcv = None
        if _market_for(ticker) != "COIN":
            try:
                ohlcv = get_ohlcv_yfinance(ticker)
            except Exception as e:
                print(f"  ⚠️ {ticker}: OHLCV 조회 실패 ({e})")
        before = json.dumps(pos, ensure_ascii=False, sort_keys=True)
        ok = enrich_position_from_buy_history(
            pos,
            ticker,
            _market_for(ticker),
            ohlcv=ohlcv,
            live_qty=float(pos.get("qty") or 0) or None,
            live_avg_p=float(pos.get("buy_p") or 0) or None,
        )
        after = json.dumps(pos, ensure_ascii=False, sort_keys=True)
        if ok or before != after:
            positions[ticker] = pos
            changed_any = True
            fib = pos.get("entry_fib_level", "?")
            sl = pos.get("sl_p", "?")
            bt = pos.get("buy_time", "?")
            print(
                f"  ✅ {ticker} — tier={pos.get('tier')} "
                f"buy_p={pos.get('buy_p')} sl_p={sl} fib={fib} buy_time={bt}"
            )
        else:
            print(f"  ⏭️ {ticker}: 변경 없음")

    if changed_any and not args.dry_run:
        save_state(args.state, state)
        print(f"💾 저장 완료: {args.state}")
    elif args.dry_run:
        print("🔍 dry-run — 저장 안 함")
    else:
        print("변경 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
