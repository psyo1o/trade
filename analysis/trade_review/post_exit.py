# -*- coding: utf-8 -*-
"""매도 이후 주가 움직임 — 조기청산 vs 적절청산 지표."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import POST_EXIT_DAYS
from .load_trades import RoundTrip
from .price_fetch import fetch_daily_ohlcv, ohlcv_unit_matches_sell

_RET_CAP = 150.0


def _cap_pct(v: float) -> float:
    return max(-_RET_CAP, min(_RET_CAP, v))


def _parse_day(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19] if " " in s or "T" in s else s[:10], fmt)
        except ValueError:
            continue
    return None


def _index_from_date(rows: list[dict[str, Any]], day: datetime) -> int | None:
    target = day.strftime("%Y-%m-%d")
    for i, r in enumerate(rows):
        if str(r.get("date", ""))[:10] >= target:
            return i
    return None


def compute_post_exit(trip: RoundTrip, *, use_cache: bool = True) -> dict[str, Any]:
    sell_dt = _parse_day(trip.sell_time)
    sell_px = float(trip.sell_price or 0)
    out: dict[str, Any] = {
        "post_exit_ok": False,
        "post_exit_note": "",
    }
    if not sell_dt or sell_px <= 0:
        out["post_exit_note"] = "매도 시각/가격 없음"
        return out

    ohlcv = fetch_daily_ohlcv(trip.market, trip.ticker, use_cache=use_cache)
    if len(ohlcv) < 5:
        out["post_exit_note"] = "OHLCV 부족"
        return out

    if not ohlcv_unit_matches_sell(sell_px, ohlcv):
        if trip.market == "COIN" and trip.ticker.startswith("USDT-"):
            from .price_fetch import _cache_path

            cp = _cache_path(trip.market, trip.ticker)
            if cp.is_file():
                cp.unlink(missing_ok=True)
            ohlcv = fetch_daily_ohlcv(trip.market, trip.ticker, use_cache=False)
        if not ohlcv_unit_matches_sell(sell_px, ohlcv):
            out["post_exit_note"] = "가격 단위 불일치(USDT vs KRW 등)"
            return out

    idx = _index_from_date(ohlcv, sell_dt)
    if idx is None:
        out["post_exit_note"] = "매도일 이후 데이터 없음"
        return out

    window = ohlcv[idx : idx + max(POST_EXIT_DAYS) + 5]
    if len(window) < 2:
        out["post_exit_note"] = "매도일 포함 구간 짧음"
        return out

    highs = [float(r.get("high") or r.get("close") or 0) for r in window[1:]]
    lows = [float(r.get("low") or r.get("close") or 0) for r in window[1:]]
    closes = [float(r.get("close") or 0) for r in window]

    max_up = max(((h - sell_px) / sell_px * 100.0 for h in highs if h > 0), default=0.0)
    max_dn = min(((l - sell_px) / sell_px * 100.0 for l in lows if l > 0), default=0.0)

    for d in POST_EXIT_DAYS:
        j = min(d, len(closes) - 1)
        if j <= 0:
            continue
        c = closes[j]
        if c > 0:
            out[f"ret_{d}d"] = round(_cap_pct((c - sell_px) / sell_px * 100.0), 2)

    out["max_up_after_pct"] = round(_cap_pct(max_up), 2)
    out["max_down_after_pct"] = round(_cap_pct(max_dn), 2)
    ret20 = float(out.get("ret_20d", 0) or 0)
    if ret20 >= 5.0:
        verdict = "early_exit"
        out["post_exit_verdict"] = verdict
        out["post_exit_verdict_ko"] = "조기청산(매도 후 상승)"
    elif ret20 <= -5.0:
        verdict = "good_exit"
        out["post_exit_verdict"] = verdict
        out["post_exit_verdict_ko"] = "적절청산(매도 후 하락)"
    else:
        out["post_exit_verdict"] = "neutral"
        out["post_exit_verdict_ko"] = "중립"
    out["post_exit_ok"] = True
    return out


def enrich_trips(trips: list[RoundTrip], *, use_cache: bool = True) -> None:
    seen: set[tuple[str, str]] = set()
    for trip in trips:
        key = (trip.market, trip.ticker)
        if key in seen:
            # 동일 종목 재매매 — 캐시 재사용
            pass
        else:
            seen.add(key)
        trip.post_exit = compute_post_exit(trip, use_cache=use_cache)
