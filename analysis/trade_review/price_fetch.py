# -*- coding: utf-8 -*-
"""일봉 조회 — 운영 봇·KIS 토큰 미사용, 로컬 캐시만."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import CACHE_DIR


def _cache_path(market: str, ticker: str) -> Path:
    safe = ticker.replace("/", "_").replace("\\", "_")
    return CACHE_DIR / f"{market}_{safe}.json"


def _save_cache(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _load_cache(path: Path, max_age_hours: float = 24.0) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    age_h = (time.time() - path.stat().st_mtime) / 3600.0
    if age_h > max_age_hours:
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else None
    except (OSError, ValueError, TypeError):
        return None


def _rows_from_df(df) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if df is None or df.empty:
        return out
    try:
        import pandas as pd

        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
    except Exception:
        try:
            df = df.reset_index()
        except Exception:
            return out
    date_col = None
    for c in df.columns:
        cl = str(c).lower()
        if cl in ("date", "datetime", "index"):
            date_col = c
            break
    if date_col is None:
        date_col = df.columns[0]
    for _, row in df.iterrows():
        try:
            d = row[date_col]
            if hasattr(d, "strftime"):
                ds = d.strftime("%Y-%m-%d")
            else:
                ds = str(d)[:10]
            o = float(row.get("Open", row.get("open", 0)) or 0)
            h = float(row.get("High", row.get("high", 0)) or 0)
            l = float(row.get("Low", row.get("low", 0)) or 0)
            c = float(row.get("Close", row.get("close", 0)) or 0)
            if c > 0:
                out.append({"date": ds, "open": o, "high": h, "low": l, "close": c})
        except (TypeError, ValueError, KeyError):
            continue
    out.sort(key=lambda x: x["date"])
    return out


def _fetch_stooq(ticker: str, *, is_kr: bool) -> list[dict[str, Any]]:
    try:
        import pandas as pd

        sym = f"{str(ticker).zfill(6)}.kr" if is_kr else f"{str(ticker).lower()}.us"
        url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
        df = pd.read_csv(url)
        return _rows_from_df(df)
    except Exception:
        return []


def _fetch_yfinance(ticker: str, *, is_kr: bool) -> list[dict[str, Any]]:
    import yfinance as yf

    candidates = [f"{ticker}.KS", f"{ticker}.KQ"] if is_kr else [ticker]
    for sym in candidates:
        try:
            df = yf.download(sym, period="2y", interval="1d", progress=False, threads=False)
            rows = _rows_from_df(df)
            if rows:
                return rows
        except Exception:
            continue
    return []


def _fetch_pykrx(ticker: str) -> list[dict[str, Any]]:
    try:
        from pykrx import stock

        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=730)).strftime("%Y%m%d")
        df = stock.get_market_ohlcv_by_date(start, end, ticker)
        if df is None or df.empty:
            return []
        out: list[dict[str, Any]] = []
        for idx, row in df.iterrows():
            ds = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            c = float(row.get("종가", row.get("close", 0)) or 0)
            if c <= 0:
                continue
            out.append(
                {
                    "date": ds,
                    "open": float(row.get("시가", row.get("open", c)) or c),
                    "high": float(row.get("고가", row.get("high", c)) or c),
                    "low": float(row.get("저가", row.get("low", c)) or c),
                    "close": c,
                }
            )
        out.sort(key=lambda x: x["date"])
        return out
    except Exception:
        return []


def _fetch_coin_upbit(ticker: str) -> list[dict[str, Any]]:
    try:
        import pyupbit

        t = str(ticker or "").strip()
        markets: list[str] = []
        if t.startswith("USDT-"):
            markets.append(t)
            markets.append(f"KRW-{t.split('-', 1)[1]}")
        elif t.startswith("KRW-"):
            markets.append(t)
        else:
            markets.append(f"KRW-{t}")
        seen: set[str] = set()
        for m in markets:
            if m in seen:
                continue
            seen.add(m)
            try:
                df = pyupbit.get_ohlcv(m, interval="day", count=400)
                rows = _rows_from_df(df)
                if rows:
                    return rows
            except Exception:
                continue
    except ImportError:
        pass
    return []


def ohlcv_unit_matches_sell(sell_px: float, ohlcv: list[dict[str, Any]]) -> bool:
    """체결가(USDT/USD/원)와 일봉 스케일이 같은지 대략 검사."""
    if sell_px <= 0 or not ohlcv:
        return False
    ref = float(ohlcv[min(len(ohlcv) // 2, len(ohlcv) - 1)].get("close") or 0)
    if ref <= 0:
        return False
    ratio = ref / sell_px
    return 0.2 <= ratio <= 5.0 or (sell_px > 1000 and ref > 1000) or (sell_px < 1000 and ref < 1000)


def fetch_daily_ohlcv(market: str, ticker: str, *, use_cache: bool = True) -> list[dict[str, Any]]:
    mk = str(market or "").strip().upper()
    t = str(ticker or "").strip()
    if not t:
        return []
    cp = _cache_path(mk, t)
    if use_cache:
        cached = _load_cache(cp)
        if cached:
            return cached

    rows: list[dict[str, Any]] = []
    if mk == "KR":
        rows = _fetch_pykrx(t) or _fetch_yfinance(t, is_kr=True) or _fetch_stooq(t, is_kr=True)
    elif mk == "US":
        rows = _fetch_yfinance(t, is_kr=False) or _fetch_stooq(t, is_kr=False)
    elif mk == "COIN":
        rows = _fetch_coin_upbit(t)

    if rows:
        _save_cache(cp, rows)
    return rows
