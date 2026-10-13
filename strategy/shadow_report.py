# -*- coding: utf-8 -*-
"""shadow_events.jsonl + trade_history.json 반사실 요약."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from strategy.shadow_rules import RULE_EARNINGS, RULE_KR_CRASH

KST = ZoneInfo("Asia/Seoul")


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if "T" in text:
            dt = datetime.fromisoformat(text)
        else:
            dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def load_jsonl(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    rows: list[dict] = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def load_trade_history(path: Path | str) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _side(row: dict) -> str:
    return str(row.get("side") or row.get("action") or "").strip().upper()


def _ticker(row: dict) -> str:
    return str(row.get("ticker") or "").strip().upper()


def _market(row: dict) -> str:
    return str(row.get("market") or "").strip().upper()


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_trip_sell(trades: list[dict], *, ticker: str, market: str, after: datetime) -> dict | None:
    """``after`` 이후 첫 BUY 에 대응하는 마지막 SELL (다음 BUY 전)."""
    sym = ticker.strip().upper()
    mk = market.strip().upper()
    window = after - timedelta(minutes=5)
    buys = []
    for row in trades:
        if _ticker(row) != sym:
            continue
        if mk and _market(row) not in ("", mk):
            continue
        ts = parse_ts(row.get("timestamp"))
        if ts is None or ts < window:
            continue
        if _side(row).startswith("BUY"):
            buys.append((ts, row))
    buys.sort(key=lambda x: x[0])
    if not buys:
        return None
    buy_ts = buys[0][0]
    next_buy = buys[1][0] if len(buys) > 1 else None
    sells = []
    for row in trades:
        if _ticker(row) != sym or not _side(row).startswith("SELL"):
            continue
        if mk and _market(row) not in ("", mk):
            continue
        ts = parse_ts(row.get("timestamp"))
        if ts is None or ts < buy_ts:
            continue
        if next_buy is not None and ts >= next_buy:
            continue
        sells.append((ts, row))
    if not sells:
        return None
    sells.sort(key=lambda x: x[0])
    return sells[-1][1]


def _later_exit(trades: list[dict], *, ticker: str, market: str, after: datetime) -> dict | None:
    """이벤트 이후, 다음 BUY 이전의 마지막 SELL."""
    sym = ticker.strip().upper()
    mk = market.strip().upper()
    later_buy = None
    sells = []
    for row in trades:
        if _ticker(row) != sym:
            continue
        if mk and _market(row) not in ("", mk):
            continue
        ts = parse_ts(row.get("timestamp"))
        if ts is None or ts < after:
            continue
        if _side(row).startswith("BUY"):
            if later_buy is None or ts < later_buy:
                later_buy = ts
        elif _side(row).startswith("SELL"):
            sells.append((ts, row))
    if later_buy is not None:
        sells = [item for item in sells if item[0] < later_buy]
    if not sells:
        return None
    sells.sort(key=lambda x: x[0])
    return sells[-1][1]


def build_rows(events: list[dict], trades: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for ev in events:
        rule = str(ev.get("rule") or "")
        details = ev.get("details") if isinstance(ev.get("details"), dict) else {}
        outcome = str(details.get("outcome") or "")
        mode = str(ev.get("mode") or "")
        ticker = str(ev.get("ticker") or "")
        market = str(ev.get("market") or "")
        et = parse_ts(ev.get("timestamp"))
        base = {
            "rule": rule,
            "mode": mode,
            "market": market,
            "ticker": ticker,
            "event_time": ev.get("timestamp") or "",
            "outcome": outcome,
            "earnings_date": details.get("earnings_date") or "",
            "trading_days_until": details.get("trading_days_until"),
            "shadow_price": details.get("price"),
            "shadow_qty": details.get("qty"),
            "avg_price": details.get("avg_price"),
            "unrealized_pct": details.get("unrealized_pct"),
            "trigger": _trigger_text(details),
            "actual_exit_time": "",
            "actual_exit_price": "",
            "profit_rate": "",
            "price_move_pct": "",
            "note": "",
        }
        if et is None:
            base["note"] = "시각 파싱 실패"
            rows.append(base)
            continue
        if rule == RULE_EARNINGS and mode == "shadow" and outcome == "would_block":
            sell = _round_trip_sell(trades, ticker=ticker, market="US", after=et)
            if sell is None:
                base["note"] = "아직 청산 없음 (또는 매수 기록 없음)"
            else:
                base["actual_exit_time"] = sell.get("timestamp") or ""
                base["actual_exit_price"] = sell.get("price") if sell.get("price") is not None else ""
                pr = _num(sell.get("profit_rate"))
                base["profit_rate"] = pr if pr is not None else ""
                base["note"] = "섀도우 매수 후 실제 청산 수익률"
        elif rule == RULE_KR_CRASH and mode == "shadow" and outcome == "would_sell":
            sell = _later_exit(trades, ticker=ticker, market="KR", after=et)
            shadow_px = _num(details.get("price"))
            if sell is None:
                base["note"] = "아직 청산 없음"
            else:
                base["actual_exit_time"] = sell.get("timestamp") or ""
                exit_px = _num(sell.get("price"))
                base["actual_exit_price"] = exit_px if exit_px is not None else ""
                pr = _num(sell.get("profit_rate"))
                base["profit_rate"] = pr if pr is not None else ""
                if shadow_px and exit_px is not None and shadow_px > 0:
                    base["price_move_pct"] = (exit_px - shadow_px) / shadow_px * 100.0
                base["note"] = "섀도우 매도가 대비 이후 실제 청산가"
        elif outcome == "fail_open":
            base["note"] = "실적일 없음 — fail-open"
        elif mode == "live":
            base["note"] = "live 실행"
        rows.append(base)
    return rows


def _trigger_text(details: dict) -> str:
    triggers = details.get("triggers") or []
    parts = []
    for item in triggers:
        if not isinstance(item, dict):
            continue
        try:
            parts.append(f"{item.get('index')} {float(item.get('change_pct')):+.2f}%")
        except (TypeError, ValueError):
            parts.append(str(item.get("index") or ""))
    return ", ".join(parts)


def _fmt_pct(value: Any) -> str:
    num = _num(value)
    if num is None:
        return "-"
    return f"{num:+.2f}%"


def _fmt_px(value: Any) -> str:
    num = _num(value)
    if num is None:
        return "-"
    if abs(num) >= 1000:
        return f"{num:,.0f}"
    return f"{num:.4f}".rstrip("0").rstrip(".")


def format_report_ko(
    events: list[dict],
    rows: list[dict],
    *,
    events_path: str = "",
    history_path: str = "",
) -> str:
    lines = [
        "=== 섀도우 규칙 복기 ===",
        f"이벤트 파일: {events_path or '(메모리)'}",
        f"매매 내역: {history_path or '(메모리)'}",
        f"전체 이벤트: {len(events)}건",
        "",
    ]
    for rule, title in (
        (RULE_EARNINGS, "실적 진입 차단 (earnings_block)"),
        (RULE_KR_CRASH, "국장 지수 급락 청산 (kr_crash_exit)"),
    ):
        evs = [e for e in events if str(e.get("rule") or "") == rule]
        rr = [r for r in rows if r.get("rule") == rule]
        lines.append(f"[{title}]")
        lines.append(f"- 이벤트 {len(evs)}건")
        if not evs:
            lines.append("- 기록 없음")
            lines.append("")
            continue
        buckets: dict[str, int] = {}
        for e in evs:
            details = e.get("details") if isinstance(e.get("details"), dict) else {}
            key = f"{e.get('mode')}/{details.get('outcome')}"
            buckets[key] = buckets.get(key, 0) + 1
        bucket_txt = ", ".join(f"{k} {v}" for k, v in sorted(buckets.items()))
        lines.append(f"- 구성: {bucket_txt}")
        if rule == RULE_EARNINGS:
            hits = [r for r in rr if r.get("outcome") == "would_block" and r.get("mode") == "shadow"]
            closed = [r for r in hits if r.get("profit_rate") != ""]
            lines.append(f"- 섀도우로 통과시킨 차단 후보 {len(hits)}건, 그중 청산 확인 {len(closed)}건")
            rates = [float(r["profit_rate"]) for r in closed]
            if rates:
                avg = sum(rates) / len(rates)
                lines.append(f"- 청산된 건 평균 수익률: {avg:+.2f}%")
            for r in hits:
                extra = (
                    f"실제 청산 {_fmt_pct(r.get('profit_rate'))} @ {_fmt_px(r.get('actual_exit_price'))}"
                    if r.get("profit_rate") != ""
                    else r.get("note") or "청산 없음"
                )
                lines.append(
                    f"  · {r.get('ticker')} 실적 {r.get('earnings_date') or '-'} "
                    f"(거래일 {r.get('trading_days_until')}) → {extra}"
                )
        else:
            hits = [r for r in rr if r.get("outcome") == "would_sell" and r.get("mode") == "shadow"]
            lines.append(f"- 섀도우 매도 후보 {len(hits)}건")
            for r in hits:
                if r.get("actual_exit_price") != "":
                    move = _fmt_pct(r.get("price_move_pct"))
                    lines.append(
                        f"  · {r.get('ticker')} 섀도우가 {_fmt_px(r.get('shadow_price'))} "
                        f"→ 실제 청산 {_fmt_px(r.get('actual_exit_price'))} "
                        f"(가격변동 {move}, 청산수익률 {_fmt_pct(r.get('profit_rate'))}) "
                        f"trigger={r.get('trigger')}"
                    )
                else:
                    lines.append(
                        f"  · {r.get('ticker')} 섀도우가 {_fmt_px(r.get('shadow_price'))} "
                        f"→ {r.get('note') or '아직 청산 없음'} trigger={r.get('trigger')}"
                    )
        lines.append("")
    lines.append("해석: 실적은 막혔다면 놓쳤을(또는 피했을) 실제 청산 수익률입니다.")
    lines.append("국장 급락은 섀도우 시각의 가격과, 그 포지션이 나중에 실제로 나온 가격입니다.")
    return "\n".join(lines).rstrip() + "\n"


CSV_FIELDS = [
    "rule",
    "mode",
    "market",
    "ticker",
    "event_time",
    "outcome",
    "earnings_date",
    "trading_days_until",
    "shadow_price",
    "shadow_qty",
    "avg_price",
    "unrealized_pct",
    "trigger",
    "actual_exit_time",
    "actual_exit_price",
    "profit_rate",
    "price_move_pct",
    "note",
]


def write_csv(path: Path | str, rows: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def analyze_files(events_path: Path | str, history_path: Path | str) -> tuple[str, list[dict]]:
    events = load_jsonl(events_path)
    trades = load_trade_history(history_path)
    rows = build_rows(events, trades)
    text = format_report_ko(
        events,
        rows,
        events_path=str(events_path),
        history_path=str(history_path),
    )
    return text, rows
