# -*- coding: utf-8 -*-
"""``trade_history.json`` BUY 기록 → ``positions`` 장부 복구·보강."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.helpers import normalize_ticker

DEFAULT_TRADE_HISTORY_PATH = Path(__file__).resolve().parent.parent / "trade_history.json"

_LEDGER_BUY_KEYS = (
    "strategy_type",
    "entry_fib_level",
    "sl_p",
    "entry_atr",
    "buy_time",
)


def is_swing_trade_reason(reason: str | None) -> bool:
    r = str(reason or "").strip().upper()
    return r in ("SWING_FIB", "SWING") or "SWING" in r


def parse_history_timestamp(ts: str | None) -> float | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return float(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None


def find_last_buy_row(
    ticker: str,
    market: str,
    *,
    history_path: Path | None = None,
) -> dict[str, Any] | None:
    path = history_path if history_path is not None else DEFAULT_TRADE_HISTORY_PATH
    if not path.is_file():
        return None
    key = normalize_ticker(ticker)
    m = str(market or "").strip().upper()
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(rows, list):
        return None
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("side", "")).upper() != "BUY":
            continue
        if str(row.get("market", "")).strip().upper() != m:
            continue
        if normalize_ticker(str(row.get("ticker", "") or "")) != key:
            continue
        return dict(row)
    return None


def ledger_extra_from_buy_payload(payload: dict | None) -> dict[str, Any]:
    """매수 장부 payload → trade_history append 필드."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for k in _LEDGER_BUY_KEYS:
        if k not in payload:
            continue
        v = payload[k]
        if v is None:
            continue
        if k in ("entry_fib_level", "sl_p", "entry_atr", "buy_time"):
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if k == "buy_time" and fv <= 0:
                continue
            if k != "buy_time" and fv <= 0:
                continue
            out[k] = fv
        else:
            s = str(v).strip()
            if s:
                out[k] = s
    return out


def position_needs_history_enrich(pos: dict | None) -> bool:
    if not isinstance(pos, dict):
        return False
    tier = str(pos.get("tier") or "")
    st = str(pos.get("strategy_type") or "").strip().upper()
    if "자동복구" in tier or "자동등록" in tier:
        return True
    if st == "SWING_FIB" or is_swing_trade_reason(tier):
        if float(pos.get("entry_fib_level", 0) or 0) <= 0:
            return True
        if float(pos.get("buy_time", 0) or 0) <= 0:
            return True
        if float(pos.get("entry_initial_risk_1r", 0) or 0) <= 0:
            return True
    if float(pos.get("buy_time", 0) or 0) <= 0 and (
        isinstance(pos.get("buy_date"), str) and str(pos.get("buy_date")).strip()
    ):
        return True
    return False


def enrich_position_from_buy_history(
    pos: dict,
    ticker: str,
    market: str,
    *,
    ohlcv=None,
    live_qty: float | None = None,
    live_avg_p: float | None = None,
    history_path: Path | None = None,
) -> bool:
    """
    ``trade_history`` 최근 BUY 로 ``positions`` 행을 매수 당시 값으로 보강.

    Returns:
        장부 필드가 바뀌었으면 True.
    """
    row = find_last_buy_row(ticker, market, history_path=history_path)
    if not row:
        return False

    changed = False
    p = pos if isinstance(pos, dict) else {}

    def _set(key: str, val: Any) -> None:
        nonlocal changed
        if val is None:
            return
        if p.get(key) != val:
            p[key] = val
            changed = True

    hist_px = float(row.get("price") or 0)
    if hist_px > 0:
        _set("buy_p", float(live_avg_p if live_avg_p and live_avg_p > 0 else hist_px))
    elif live_avg_p and live_avg_p > 0:
        _set("buy_p", float(live_avg_p))

    hist_qty = float(row.get("qty") or 0)
    if live_qty is not None and live_qty > 0:
        _set("qty", float(live_qty))
    elif hist_qty > 0:
        _set("qty", hist_qty)

    bt = row.get("buy_time")
    try:
        bt_f = float(bt) if bt is not None else 0.0
    except (TypeError, ValueError):
        bt_f = 0.0
    if bt_f <= 0:
        bt_f = float(parse_history_timestamp(str(row.get("timestamp") or "")) or 0.0)
    if bt_f > 0:
        _set("buy_time", bt_f)

    reason = str(row.get("reason") or "").strip()
    st_hist = str(row.get("strategy_type") or "").strip().upper()
    if st_hist:
        _set("strategy_type", st_hist)
    elif is_swing_trade_reason(reason):
        _set("strategy_type", "SWING_FIB")

    if reason and (is_swing_trade_reason(reason) or reason.upper() not in ("", "MANUAL")):
        _set("tier", reason if is_swing_trade_reason(reason) else reason)

    for k in ("entry_fib_level", "sl_p", "entry_atr"):
        if k not in row:
            continue
        try:
            fv = float(row.get(k) or 0)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            _set(k, fv)

    buy_p = float(p.get("buy_p") or 0)
    if float(p.get("max_p") or 0) <= 0 and buy_p > 0:
        _set("max_p", buy_p)

    p.setdefault("scale_out_done", False)

    st = str(p.get("strategy_type") or "").strip().upper()
    tier = str(p.get("tier") or "")
    is_swing = st == "SWING_FIB" or is_swing_trade_reason(tier) or is_swing_trade_reason(reason)

    if is_swing and ohlcv and buy_p > 0:
        from strategy.rules import (
            infer_swing_entry_fib_from_ohlcv,
            register_swing_entry_risk_fields,
            swing_entry_sl_p,
        )

        fib = float(p.get("entry_fib_level", 0) or 0)
        if fib <= 0:
            inferred = infer_swing_entry_fib_from_ohlcv(ohlcv, buy_p)
            if inferred > 0:
                _set("entry_fib_level", float(inferred))

        if float(p.get("entry_initial_risk_1r", 0) or 0) <= 0:
            before_r = float(p.get("entry_initial_risk_1r", 0) or 0)
            register_swing_entry_risk_fields(
                p, buy_p, ohlcv, market=market, ticker=ticker
            )
            after_r = float(p.get("entry_initial_risk_1r", 0) or 0)
            if after_r != before_r:
                changed = True

        sl = float(p.get("sl_p", 0) or 0)
        fib_now = float(p.get("entry_fib_level", 0) or 0)
        hard = float(p.get("entry_initial_hard_floor", 0) or 0)
        raw_sl = fib_now if fib_now > 0 else hard
        if raw_sl > 0:
            new_sl = swing_entry_sl_p(buy_p, raw_sl)
            if sl <= 0 or abs(sl - new_sl) > 1e-6:
                _set("sl_p", float(new_sl))

    return changed


def build_recovered_position_from_history(
    ticker: str,
    market: str,
    *,
    live_avg_p: float,
    live_qty: float,
    ohlcv=None,
    history_path: Path | None = None,
) -> dict[str, Any] | None:
    """자동복구 대신 trade_history BUY 기반 신규 장부 행."""
    row = find_last_buy_row(ticker, market, history_path=history_path)
    if not row:
        return None
    pos: dict[str, Any] = {"scale_out_done": False}
    enrich_position_from_buy_history(
        pos,
        ticker,
        market,
        ohlcv=ohlcv,
        live_qty=live_qty,
        live_avg_p=live_avg_p,
        history_path=history_path,
    )
    if float(pos.get("buy_p") or 0) <= 0:
        return None
    return pos
