# -*- coding: utf-8 -*-
"""trade_history.json → 라운드트립(매수-매도 쌍) 추출."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .eras import era_label, load_eras
from .config import ERAS_PATH


def _parse_ts(ts: str | None) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _norm_strategy(row: dict[str, Any]) -> str:
    st = str(row.get("strategy_type") or "").strip()
    if st:
        return st.upper()
    reason = str(row.get("reason") or "").strip()
    ru = reason.upper()
    if "SWING" in ru:
        return "SWING_FIB"
    if "V8" in ru or "TREND_V8" in ru:
        return "TREND_V8"
    if "V6" in ru or "스나이퍼" in reason:
        return "V6_SNIPER"
    if "V5" in ru:
        return "V5_LEGACY"
    if "HEDGE" in ru or "PHASE4" in ru:
        return "HEDGE_PHASE4"
    return reason[:40] or "UNKNOWN"


def _norm_sell_bucket(reason: str | None) -> str:
    r = str(reason or "").strip()
    ru = r.upper()
    if "PHASE5" in ru or "서킷" in r:
        return "phase5_circuit"
    if "MANUAL" in ru:
        return "manual"
    if "SWING" in ru and "SELL" in ru:
        return "swing_exit"
    if "타임스탑" in r or "TIME" in ru:
        return "time_stop"
    if "하드스탑" in r or "HARD" in ru:
        return "hard_stop"
    if "익절" in r or "PROFIT" in ru or "락" in r:
        return "profit_lock"
    if "V8" in ru:
        return "v8_exit"
    return "other"


@dataclass
class RoundTrip:
    market: str
    ticker: str
    name: str
    sector: str
    strategy: str
    buy_time: str
    sell_time: str
    hold_hours: float
    buy_price: float
    sell_price: float
    qty: float
    profit_rate: float | None
    sell_reason: str
    sell_bucket: str
    buy_era: str
    sell_era: str
    buy_reason: str = ""
    open_position: bool = False
    post_exit: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        flat = {k: v for k, v in row.items() if k != "post_exit"}
        for pk, pv in (self.post_exit or {}).items():
            flat[pk] = pv
        return flat


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("trade_history must be a JSON array")
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
    out.sort(key=lambda r: (_parse_ts(r.get("timestamp")) or datetime.min, str(r.get("ticker", ""))))
    return out


def build_round_trips(
    events: list[dict[str, Any]],
    *,
    eras_path: Path = ERAS_PATH,
) -> tuple[list[RoundTrip], list[dict[str, Any]]]:
    eras = load_eras(eras_path)
    # FIFO lots: (market, ticker) -> list of buy dicts with remaining qty
    lots: dict[tuple[str, str], list[dict[str, Any]]] = {}
    trips: list[RoundTrip] = []
    orphans: list[dict[str, Any]] = []

    for ev in events:
        mk = str(ev.get("market", "")).strip().upper()
        ticker = str(ev.get("ticker", "")).strip()
        side = str(ev.get("side", "")).strip().upper()
        if not mk or not ticker or side not in ("BUY", "SELL"):
            continue
        key = (mk, ticker)
        if side == "BUY":
            qty = float(ev.get("qty") or 0)
            if qty <= 0:
                continue
            lots.setdefault(key, []).append(
                {
                    "qty": qty,
                    "price": float(ev.get("price") or 0),
                    "time": ev.get("timestamp"),
                    "strategy": _norm_strategy(ev),
                    "reason": str(ev.get("reason") or ""),
                    "name": str(ev.get("name") or ticker),
                    "sector": str(ev.get("sector") or ""),
                }
            )
            continue

        sell_qty = float(ev.get("qty") or 0)
        sell_price = float(ev.get("price") or 0)
        sell_time = str(ev.get("timestamp") or "")
        sell_reason = str(ev.get("reason") or "")
        profit_rate = ev.get("profit_rate")
        if sell_qty <= 0:
            continue
        queue = lots.get(key) or []
        if not queue:
            orphans.append(ev)
            continue
        remaining = sell_qty
        while remaining > 1e-9 and queue:
            lot = queue[0]
            lot_qty = float(lot["qty"])
            take = min(remaining, lot_qty)
            buy_dt = _parse_ts(lot.get("time"))
            sell_dt = _parse_ts(sell_time)
            hold_h = 0.0
            if buy_dt and sell_dt:
                hold_h = max(0.0, (sell_dt - buy_dt).total_seconds() / 3600.0)
            pr = profit_rate
            if pr is None and lot["price"] > 0 and sell_price > 0:
                pr = (sell_price - lot["price"]) / lot["price"] * 100.0
            trips.append(
                RoundTrip(
                    market=mk,
                    ticker=ticker,
                    name=str(ev.get("name") or lot.get("name") or ticker),
                    sector=str(ev.get("sector") or lot.get("sector") or ""),
                    strategy=str(lot.get("strategy") or "UNKNOWN"),
                    buy_time=str(lot.get("time") or ""),
                    sell_time=sell_time,
                    hold_hours=round(hold_h, 2),
                    buy_price=float(lot["price"]),
                    sell_price=sell_price,
                    qty=take,
                    profit_rate=float(pr) if pr is not None else None,
                    sell_reason=sell_reason,
                    sell_bucket=_norm_sell_bucket(sell_reason),
                    buy_era=era_label(str(lot.get("time") or ""), eras),
                    sell_era=era_label(sell_time, eras),
                    buy_reason=str(lot.get("reason") or ""),
                )
            )
            lot["qty"] = lot_qty - take
            remaining -= take
            if lot["qty"] <= 1e-9:
                queue.pop(0)
        lots[key] = queue
        if remaining > 1e-9:
            orphans.append({**ev, "unmatched_qty": remaining})

    return trips, orphans
