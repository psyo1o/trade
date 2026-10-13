#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""섀도우 규칙 사후 평가 — ``logs/shadow_events.jsonl`` + 일봉.

운영 봇·KIS 주문과 분리. 이벤트 가격 대비 1/3/5 거래일 종가.

  py -3.11 analysis/run_shadow_review.py
  py -3.11 analysis/run_shadow_review.py --no-fetch
  py -3.11 analysis/run_shadow_review.py --events logs/shadow_events.jsonl
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOT_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trade_review.config import OUTPUT_DIR  # noqa: E402
from trade_review.price_fetch import fetch_daily_ohlcv  # noqa: E402
from trade_review.shadow_outcome import (  # noqa: E402
    event_session_day,
    forward_returns,
    interpret,
    is_outcome_event,
    load_shadow_events,
)


def _default_events_path() -> Path:
    return BOT_ROOT / "logs" / "shadow_events.jsonl"


def build_rows(events: list[dict], *, use_cache: bool, fetch=fetch_daily_ohlcv) -> list[dict]:
    rows: list[dict] = []
    for event in events:
        if not is_outcome_event(event):
            continue
        market = str(event.get("market") or "").upper()
        ticker = str(event.get("ticker") or "")
        day = event_session_day(event)
        try:
            price = float(event.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            ohlcv = fetch(market, ticker, use_cache=use_cache) if ticker and market else []
        except Exception as exc:
            ohlcv = []
            note_fetch = f"일봉 조회 실패: {type(exc).__name__}"
        else:
            note_fetch = ""
        rets = forward_returns(ohlcv, day, price)
        if note_fetch and not rets.get("ok"):
            rets["note"] = note_fetch
        row = {
            "ts_kst": event.get("ts_kst") or "",
            "session_date": day,
            "rule": event.get("rule") or "",
            "mode": event.get("mode") or "",
            "action": event.get("action") or "",
            "market": market,
            "ticker": ticker,
            "price": price,
            "position_pnl_pct": event.get("position_pnl_pct"),
            "index_change_pct": event.get("index_change_pct"),
            "earnings_date": event.get("earnings_date") or "",
            "trigger": event.get("trigger") or "",
            "ret_1d": rets.get("ret_1d"),
            "ret_3d": rets.get("ret_3d"),
            "ret_5d": rets.get("ret_5d"),
            "note": rets.get("note") or "",
            "read": interpret(str(event.get("rule") or ""), str(event.get("action") or ""), rets),
        }
        rows.append(row)
    return rows


def _fmt(value) -> str:
    if value is None or value == "":
        return ""
    return str(value)


def write_outputs(rows: list[dict], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "shadow_outcomes.csv"
    md_path = out_dir / "shadow_outcomes.md"
    fields = [
        "ts_kst",
        "session_date",
        "rule",
        "mode",
        "action",
        "market",
        "ticker",
        "price",
        "position_pnl_pct",
        "index_change_pct",
        "earnings_date",
        "trigger",
        "ret_1d",
        "ret_3d",
        "ret_5d",
        "note",
        "read",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _fmt(row.get(k)) for k in fields})

    lines = [
        "# 섀도우 규칙 사후 평가",
        "",
        f"생성: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "수익률은 **이벤트 가격**(가상 매수·매도가) 대비 이후 1/3/5 **거래일** 종가.",
        "",
        "- 실적 차단(`would_block`/`block`): 양수면 막힌 매수가 올랐던 것(기회비용), 음수면 하락을 피한 것.",
        "- 국장 급락 청산(`would_sell`/`sell`): 음수면 판 뒤 더 내림(청산 유리), 양수면 반등(청산이 이름).",
        "- `fail_open`·미발동은 이 표에 넣지 않는다.",
        "",
        "| 시각(KST) | 규칙 | 모드 | 시장 | 티커 | 가격 | 보유손익% | 지수% | 1d | 3d | 5d | 해석 |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    if not rows:
        lines.append("|  |  |  |  | (이벤트 없음) |  |  |  |  |  |  |  |")
    for row in rows:
        lines.append(
            "| {ts} | {rule} | {mode} | {market} | {ticker} | {price} | {pnl} | {idx} | {d1} | {d3} | {d5} | {read} |".format(
                ts=_fmt(row.get("ts_kst")),
                rule=_fmt(row.get("rule")),
                mode=_fmt(row.get("mode")),
                market=_fmt(row.get("market")),
                ticker=_fmt(row.get("ticker")),
                price=_fmt(row.get("price")),
                pnl=_fmt(row.get("position_pnl_pct")),
                idx=_fmt(row.get("index_change_pct")),
                d1=_fmt(row.get("ret_1d")),
                d3=_fmt(row.get("ret_3d")),
                d5=_fmt(row.get("ret_5d")),
                read=_fmt(row.get("read")) or _fmt(row.get("note")),
            )
        )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, csv_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="섀도우 규칙 이벤트 사후 수익률")
    parser.add_argument("--events", type=Path, default=_default_events_path())
    parser.add_argument("--no-fetch", action="store_true", help="analysis/cache 일봉만 사용")
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    events = load_shadow_events(args.events)
    rows = build_rows(events, use_cache=not args.no_fetch)
    md_path, csv_path = write_outputs(rows, args.out)
    print(f"events={len(events)} outcomes={len(rows)}")
    print(md_path)
    print(csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
