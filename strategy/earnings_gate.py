# -*- coding: utf-8 -*-
"""미장 실적 발표 진입 차단.

매수일(미국 동부 세션 날짜)을 포함해 앞으로 10거래일 안에 실적이 있으면 신규 매수를 막는다.
당일 장후(after-close) 발표도 같은 날짜로 포함된다.

* ``off``    — 조회·로그·기록 없음
* ``shadow`` — ``[SHADOW-EARNINGS]`` 로그만, 매수는 그대로
* ``live``   — 매수 스킵

실적일을 못 구하면 fail-open (매수 허용 + 경고).
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from strategy.rule_modes import normalize_rule_mode
from strategy.shadow_events import append_shadow_event

EARNINGS_BLOCK_TRADING_DAYS = 10
_EARNINGS_TTL_SEC = 12 * 3600
_EARNINGS_MISS_TTL_SEC = 30 * 60
_ET = ZoneInfo("America/New_York")
_CACHE_LOCK = threading.Lock()


def us_session_date(now: datetime | None = None) -> date:
    """미장 매수일. 봇 PC가 KST여도 미국 동부 달력 날짜를 쓴다."""
    if now is None:
        current = datetime.now(_ET)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=ZoneInfo("Asia/Seoul")).astimezone(_ET)
    else:
        current = now.astimezone(_ET)
    return current.date()


def to_us_eastern_date(value: Any) -> date | None:
    """실적 시각 → 미국 동부 날짜. naive 는 이미 그 날짜로 본다."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        try:
            return value.astimezone(_ET).date()
        except Exception:
            return value.date()
    if isinstance(value, date):
        return value
    try:
        import pandas as pd

        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return None
        if stamp.tzinfo is None:
            return stamp.date()
        return stamp.tz_convert("America/New_York").date()
    except Exception:
        text = str(value).strip()
        if len(text) >= 10:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def block_sessions(as_of: date, sessions: Sequence[date], n: int = EARNINGS_BLOCK_TRADING_DAYS) -> list[date]:
    """``as_of`` 이상인 거래일을 n개. ``as_of`` 가 거래일이면 그날이 1번째(당일 포함)."""
    picked = [d for d in sessions if d >= as_of]
    return picked[: max(0, int(n))]


def weekday_sessions(as_of: date, n: int = EARNINGS_BLOCK_TRADING_DAYS) -> list[date]:
    """NYSE 캘린더를 못 쓸 때 주말 제외 폴백(공휴일은 모름)."""
    out: list[date] = []
    cursor = as_of
    guard = 0
    while len(out) < n and guard < 80:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
        guard += 1
    return out


def nyse_block_sessions(as_of: date, n: int = EARNINGS_BLOCK_TRADING_DAYS) -> list[date]:
    """NYSE 거래일 n개(매수일 포함). 캘린더 실패 시 평일 폴백."""
    try:
        import pandas as pd
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar("NYSE")
        end = as_of + timedelta(days=max(21, int(n) * 3 + 14))
        valid = cal.valid_days(start_date=as_of, end_date=end)
        sessions = [pd.Timestamp(x).date() for x in valid]
        picked = block_sessions(as_of, sessions, n)
        if len(picked) >= min(n, 1):
            return picked
    except Exception:
        pass
    return weekday_sessions(as_of, n)


def earnings_in_window(earnings_day: date | None, sessions: Sequence[date]) -> bool:
    """실적일이 창(매수일 포함 10거래일) 안이면 True. 과거 실적은 False."""
    if earnings_day is None:
        return False
    return earnings_day in set(sessions)


def next_earnings_on_or_after(dates: Sequence[date], as_of: date) -> date | None:
    future = [d for d in dates if d >= as_of]
    return min(future) if future else None


def decide_earnings_action(mode: str, *, in_window: bool, data_ok: bool) -> str:
    """``noop`` | ``fail_open`` | ``allow`` | ``shadow`` | ``block``.

    ``off`` 는 창·데이터와 무관하게 ``noop`` (조회 자체를 하지 않을 때와 같은 결과).
    """
    normalized = normalize_rule_mode(mode)
    if normalized == "off":
        return "noop"
    if not data_ok:
        return "fail_open"
    if not in_window:
        return "allow"
    if normalized == "shadow":
        return "shadow"
    if normalized == "live":
        return "block"
    return "noop"


def _cache_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "earnings_cache.json"


def _load_cache() -> dict[str, Any]:
    path = _cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_cache(data: dict[str, Any]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"  ⚠️ [EARNINGS] 캐시 저장 실패: {exc}")


def _dates_from_frame(frame: Any) -> list[date]:
    if frame is None:
        return []
    try:
        empty = bool(getattr(frame, "empty", False))
    except Exception:
        empty = False
    if empty:
        return []
    index = getattr(frame, "index", None)
    if index is None:
        return []
    out: list[date] = []
    for item in list(index):
        parsed = to_us_eastern_date(item)
        if parsed is not None:
            out.append(parsed)
    return sorted(set(out))


def _dates_from_calendar(calendar: Any) -> list[date]:
    values: list[Any] = []
    if isinstance(calendar, dict):
        for key in ("Earnings Date", "EarningsDate", "earningsDate", "earnings_date"):
            if key in calendar:
                raw = calendar.get(key)
                if isinstance(raw, (list, tuple)):
                    values.extend(raw)
                else:
                    values.append(raw)
                break
    out: list[date] = []
    for item in values:
        parsed = to_us_eastern_date(item)
        if parsed is not None:
            out.append(parsed)
    return sorted(set(out))


def fetch_us_earnings_dates(ticker: str) -> tuple[list[date] | None, bool]:
    """(날짜 목록, data_ok). 네트워크·파싱 실패는 ``(None, False)`` — fail-open."""
    sym = str(ticker or "").strip().upper().replace(".", "-")
    if not sym:
        return None, False
    try:
        import yfinance as yf

        from utils.yfinance_guard import yf_call

        def _pull() -> list[date] | None:
            handle = yf.Ticker(sym)
            frame = None
            calendar = None
            try:
                getter = getattr(handle, "get_earnings_dates", None)
                frame = getter(limit=16) if callable(getter) else getattr(handle, "earnings_dates", None)
            except Exception:
                frame = None
            dates = _dates_from_frame(frame)
            if dates:
                return dates
            try:
                calendar = handle.calendar
            except Exception:
                calendar = None
            extra = _dates_from_calendar(calendar)
            if frame is None and calendar is None:
                return None
            return extra

        pulled = yf_call(_pull, label=f"earnings_{sym}", ticker=sym)
    except Exception as exc:
        print(f"  ⚠️ [EARNINGS] {sym} 조회 예외: {type(exc).__name__}")
        return None, False
    if pulled is None:
        return None, False
    if isinstance(pulled, list):
        return pulled, True
    return None, False


def cached_us_earnings_dates(
    ticker: str,
    *,
    fetcher: Callable[[str], tuple[list[date] | None, bool]] | None = None,
    now_ts: float | None = None,
    use_cache: bool = True,
) -> tuple[list[date] | None, bool]:
    """디스크 캐시. 성공 12시간, 실패 30분. 캐시 깨짐은 무시하고 재조회."""
    sym = str(ticker or "").strip().upper().replace(".", "-")
    if not sym:
        return None, False
    fetch = fetcher or fetch_us_earnings_dates
    now = float(now_ts if now_ts is not None else datetime.now().timestamp())
    if not use_cache:
        try:
            return fetch(sym)
        except Exception:
            return None, False
    with _CACHE_LOCK:
        cache = _load_cache()
        row = cache.get(sym) if isinstance(cache.get(sym), dict) else None
        if row:
            try:
                age = now - float(row.get("fetched_at") or 0.0)
            except (TypeError, ValueError):
                age = 1e12
            ok = bool(row.get("ok"))
            ttl = _EARNINGS_TTL_SEC if ok else _EARNINGS_MISS_TTL_SEC
            if age >= 0 and age < ttl:
                if not ok:
                    return None, False
                dates: list[date] = []
                for text in row.get("dates") or []:
                    parsed = to_us_eastern_date(text)
                    if parsed is not None:
                        dates.append(parsed)
                return dates, True
        dates, ok = fetch(sym)
        cache[sym] = {
            "fetched_at": now,
            "ok": bool(ok),
            "dates": [d.isoformat() for d in (dates or [])],
        }
        _save_cache(cache)
        return dates, bool(ok)


def format_shadow_earnings_log(ticker: str, earnings_day: date) -> str:
    return f"[SHADOW-EARNINGS] would block {ticker}, earnings {earnings_day.isoformat()}"


def us_earnings_entry_blocked(
    mode: str,
    ticker: str,
    price: float,
    strategy_type: str,
    *,
    as_of: date | None = None,
    now: datetime | None = None,
    sessions: Sequence[date] | None = None,
    fetcher: Callable[[str], tuple[list[date] | None, bool]] | None = None,
    events_path: str | Path | None = None,
    use_cache: bool = True,
) -> bool:
    """True 이면 이번 매수를 건너뛴다. shadow·off·fail-open 은 False (매수 유지)."""
    normalized = normalize_rule_mode(mode)
    if normalized == "off":
        return False

    sym = str(ticker or "").strip().upper()
    session_day = as_of or us_session_date(now)
    try:
        dates, data_ok = cached_us_earnings_dates(sym, fetcher=fetcher, use_cache=use_cache)
    except Exception as exc:
        print(f"  ⚠️ [EARNINGS] {sym} 실적일 조회 실패 — 매수 허용(fail-open): {type(exc).__name__}")
        return False

    window = list(sessions) if sessions is not None else nyse_block_sessions(session_day)
    upcoming = next_earnings_on_or_after(dates or [], session_day) if data_ok else None
    in_window = bool(data_ok) and earnings_in_window(upcoming, window)
    action = decide_earnings_action(normalized, in_window=in_window, data_ok=data_ok)

    if action == "allow":
        return False
    if action == "fail_open":
        print(f"  ⚠️ [EARNINGS] {sym} 실적일 조회 실패 — 매수 허용(fail-open)")
        append_shadow_event(
            {
                "rule": "earnings_block",
                "mode": normalized,
                "action": "fail_open",
                "ticker": sym,
                "market": "US",
                "price": float(price or 0.0),
                "position_pnl_pct": None,
                "index_change_pct": None,
                "strategy_type": strategy_type,
                "session_date": session_day.isoformat(),
            },
            path=events_path,
        )
        return False
    if action not in ("shadow", "block") or upcoming is None:
        return False

    if action == "shadow":
        print(f"  {format_shadow_earnings_log(sym, upcoming)}")
        event_action = "would_block"
        skip = False
    else:
        print(f"  🚫 [EARNINGS-LIVE] block {sym}, earnings {upcoming.isoformat()}")
        event_action = "block"
        skip = True

    append_shadow_event(
        {
            "rule": "earnings_block",
            "mode": normalized,
            "action": event_action,
            "ticker": sym,
            "market": "US",
            "price": float(price or 0.0),
            "position_pnl_pct": None,
            "index_change_pct": None,
            "strategy_type": strategy_type,
            "earnings_date": upcoming.isoformat(),
            "window_end": window[-1].isoformat() if window else "",
            "session_date": session_day.isoformat(),
        },
        path=events_path,
    )
    return skip
