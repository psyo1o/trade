# -*- coding: utf-8 -*-
"""일봉 OHLCV 디스크 캐시 — yfinance 401 시에도 최근 200봉 재사용."""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "ohlcv_cache"
_DEFAULT_MAX_AGE_SEC = 72 * 3600  # 3일

# 매매·지표별 최소 일봉 수 (run_bot / strategy.rules 와 동기화)
OHLCV_MIN_BARS = {
    "sell_loop": 14,       # 매도 루프·ATR 등 최소
    "v8_exit": 20,         # V8 get_final_exit_price
    "swing": 60,           # 스윙 진입·피보·구름·매도선
    "v8_entry": 120,       # V8 calculate_pro_signals (120MA)
    "cache_target": 200,   # get_cached_ohlcv 목표·ma200 여유
}


def ohlcv_len_ok(ohlcv: list | None, purpose: str) -> bool:
    """``purpose`` 키에 대해 봉 수가 충분한지."""
    need = int(OHLCV_MIN_BARS.get(purpose, 0))
    if need <= 0:
        return bool(ohlcv)
    return bool(ohlcv) and len(ohlcv) >= need


def _cache_path(ticker: str) -> Path:
    key = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(ticker).strip().upper())
    return _CACHE_DIR / f"{key}.json"


def bar_date_key(bar: dict) -> str:
    """일봉 ``d`` / ``date`` / ``xymd`` → ``YYYYMMDD``."""
    if not isinstance(bar, dict):
        return ""
    for key in ("d", "date", "xymd", "stck_bsop_date"):
        raw = str(bar.get(key) or "").strip()
        if len(raw) >= 8 and raw[:8].isdigit():
            return raw[:8]
    return ""


def normalize_ohlcv_series(rows: list | None) -> list:
    """날짜 오름차순 정렬·중복 제거(동일 일자는 마지막 봉 유지)."""
    if not isinstance(rows, list) or not rows:
        return []
    cleaned: list[dict] = []
    for bar in rows:
        if not isinstance(bar, dict):
            continue
        try:
            c = float(bar.get("c", 0) or 0)
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        out = dict(bar)
        dk = bar_date_key(out)
        if dk:
            out["d"] = dk
        cleaned.append(out)
    if not cleaned:
        return []
    if any(bar_date_key(b) for b in cleaned):
        cleaned.sort(key=lambda b: bar_date_key(b) or "00000000")
        deduped: list[dict] = []
        for bar in cleaned:
            dk = bar_date_key(bar)
            if deduped and dk and bar_date_key(deduped[-1]) == dk:
                deduped[-1] = bar
            else:
                deduped.append(bar)
        return deduped
    return cleaned


def ohlcv_series_valid(
    rows: list | None,
    *,
    max_stale_calendar_days: int = 14,
) -> bool:
    """
    매도선·5MA용 일봉이 최신 순서인지 검사.

    * 날짜가 있으면: 마지막 봉 일자가 ``max_stale_calendar_days`` 이내, 꼬리 5봉 비감소.
    * 날짜가 없으면: 마지막 종가가 최근 20봉 고가의 75% 미만이면 비정상(꼬리 봉).
    """
    series = normalize_ohlcv_series(rows)
    if len(series) < int(OHLCV_MIN_BARS.get("sell_loop", 14)):
        return False
    tail_dates = [bar_date_key(b) for b in series[-5:]]
    if all(tail_dates):
        if tail_dates != sorted(tail_dates):
            return False
        try:
            last_d = datetime.strptime(tail_dates[-1], "%Y%m%d").date()
            today = date.today()
            age_days = (today - last_d).days
            if age_days > int(max_stale_calendar_days):
                return False
            if age_days < -3:
                return False
        except ValueError:
            return False
        return True
    try:
        closes = [float(b.get("c", 0) or 0) for b in series[-60:]]
    except (TypeError, ValueError):
        return False
    if not closes or closes[-1] <= 0:
        return False
    peak60 = max(closes)
    peak5 = max(closes[-5:])
    if peak60 <= 0 or peak5 <= 0:
        return False
    return closes[-1] >= peak5 * 0.95 and closes[-1] >= peak60 * 0.85


def ohlcv_last_close(rows: list | None) -> float:
    series = normalize_ohlcv_series(rows)
    if not series:
        return 0.0
    try:
        return float(series[-1].get("c", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def ohlcv_last_close_rel_diff(primary: list | None, reference: list | None) -> float | None:
    """``primary`` 최종 종가가 ``reference`` 대비 얼마나 다른지 (비율). 둘 다 유효할 때만."""
    p = ohlcv_last_close(primary)
    r = ohlcv_last_close(reference)
    if p <= 0 or r <= 0:
        return None
    return abs(p - r) / r


def _fmt_ohlcv_close_label(price: float, *, market_us: bool) -> str:
    px = float(price)
    if market_us:
        return f"${px:,.2f}"
    return f"{int(px):,}원"


def finalize_ohlcv_daily(
    rows: list | None,
    *,
    ticker: str = "",
    source: str = "",
    max_bars: int = 260,
) -> list:
    """날짜 정렬·꼬리 검증 후 일봉 반환. 비정상이면 ``[]``."""
    series = normalize_ohlcv_series(rows)
    if not series:
        return []
    if len(series) > int(max_bars):
        series = series[-int(max_bars) :]
    if not ohlcv_series_valid(series):
        tk = str(ticker or "").strip()
        src = str(source or "").strip()
        tag = f"[{tk}] " if tk else ""
        if src:
            tag += f"{src} "
        print(f"     [OHLCV] {tag}일봉 비정상(정렬·최신성) — 캐시 저장 생략")
        return []
    return series


def select_validated_ohlcv(
    kis_rows: list | None,
    ref_rows: list | None,
    *,
    ticker: str = "",
    reference_name: str = "yfinance",
    market_us: bool = True,
    max_last_close_rel_diff: float = 0.06,
) -> list:
    """
    KIS vs 기준 시계열(pykrx·yfinance 등) 교차검증 후 채택.

    * 기준源 비정상 / KIS 꼬림 / 종가 괴리 > 한도 → 기준源 채택.
    * 일치 시 KIS 채택(브로커·주문과 동일).
    """
    tk = str(ticker or "").strip() or "?"
    ref_label = str(reference_name or "ref").strip()
    kis = normalize_ohlcv_series(kis_rows or [])
    ref = normalize_ohlcv_series(ref_rows or [])
    k_ok = ohlcv_series_valid(kis)
    r_ok = ohlcv_series_valid(ref)

    if r_ok and not k_ok:
        last = ref[-1] if ref else {}
        print(
            f"     [OHLCV] [{tk}] KIS 비정상, {ref_label} 채택 "
            f"(최종 {bar_date_key(last) or '?'} "
            f"{_fmt_ohlcv_close_label(ohlcv_last_close(ref), market_us=market_us)})"
        )
        return ref
    if k_ok and not r_ok:
        print(f"     [OHLCV] [{tk}] {ref_label} 부족, KIS {len(kis)}봉 채택")
        return kis
    if not k_ok and not r_ok:
        return ref if len(ref) >= len(kis) else kis

    rel = ohlcv_last_close_rel_diff(kis, ref)
    cap = float(max_last_close_rel_diff)
    if rel is not None and rel > cap:
        print(
            f"     [OHLCV] [{tk}] KIS vs {ref_label} 종가 괴리 {rel * 100:.1f}% "
            f"(KIS {_fmt_ohlcv_close_label(ohlcv_last_close(kis), market_us=market_us)}, "
            f"{ref_label.upper()} {_fmt_ohlcv_close_label(ohlcv_last_close(ref), market_us=market_us)}), "
            f"{ref_label} 채택"
        )
        return ref

    print(
        f"     [OHLCV] [{tk}] KIS/{ref_label} 일치 "
        f"({bar_date_key(kis[-1]) or '?'} "
        f"{_fmt_ohlcv_close_label(ohlcv_last_close(kis), market_us=market_us)}, {len(kis)}봉)"
    )
    return kis


def select_validated_equity_ohlcv(
    kis_rows: list | None,
    yf_rows: list | None,
    *,
    ticker: str = "",
    max_last_close_rel_diff: float = 0.06,
) -> list:
    """미장 — KIS vs yfinance."""
    return select_validated_ohlcv(
        kis_rows,
        yf_rows,
        ticker=ticker,
        reference_name="yfinance",
        market_us=True,
        max_last_close_rel_diff=max_last_close_rel_diff,
    )


def select_validated_kr_ohlcv(
    kis_rows: list | None,
    ref_rows: list | None,
    *,
    ticker: str = "",
    reference_name: str = "pykrx",
    max_last_close_rel_diff: float = 0.06,
) -> list:
    """국장 — KIS vs pykrx(없으면 yfinance)."""
    return select_validated_ohlcv(
        kis_rows,
        ref_rows,
        ticker=ticker,
        reference_name=reference_name,
        market_us=False,
        max_last_close_rel_diff=max_last_close_rel_diff,
    )


def invalidate_disk_ohlcv(ticker: str) -> None:
    """깨진·역순 디스크 캐시 삭제 — 다음 ``get_cached_ohlcv`` 가 재조회."""
    try:
        _cache_path(ticker).unlink(missing_ok=True)
    except Exception:
        pass


def load_disk_ohlcv(ticker: str, *, max_age_sec: float = _DEFAULT_MAX_AGE_SEC) -> list | None:
    path = _cache_path(ticker)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        saved = float(raw.get("saved_at", 0))
        if max_age_sec > 0 and (time.time() - saved) > max_age_sec:
            return None
        rows = raw.get("ohlcv")
        if not isinstance(rows, list) or not rows:
            return None
        series = normalize_ohlcv_series(rows)
        if not ohlcv_series_valid(series):
            invalidate_disk_ohlcv(ticker)
            return None
        return series
    except Exception:
        return None


def save_disk_ohlcv(ticker: str, ohlcv: list) -> None:
    series = normalize_ohlcv_series(ohlcv)
    if not ohlcv_series_valid(series):
        return
    if len(series) < 14:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": time.time(), "ohlcv": series}
        _cache_path(ticker).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
