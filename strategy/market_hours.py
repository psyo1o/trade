# -*- coding: utf-8 -*-
"""시장별 영업시간 누적 — 타임스탑·스윙 시간가중 손절용."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
US_EAST = ZoneInfo("America/New_York")

# KRX 정규장 / NYSE 정규장 (현지 시각)
KR_SESSION = (time(9, 0), time(15, 30))
US_SESSION = (time(9, 30), time(16, 0))


def _to_aware(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _session_overlap_seconds(day: datetime, tz: ZoneInfo, open_t: time, close_t: time, start: datetime, end: datetime) -> float:
    """하루 구간 [open, close] 와 [start, end] 겹치는 초."""
    local_day = day.astimezone(tz).date()
    open_dt = datetime.combine(local_day, open_t, tzinfo=tz)
    close_dt = datetime.combine(local_day, close_t, tzinfo=tz)
    seg_start = max(start, open_dt)
    seg_end = min(end, close_dt)
    if seg_end <= seg_start:
        return 0.0
    return (seg_end - seg_start).total_seconds()


def trading_hours_elapsed(
    market: str,
    start: datetime | float,
    end: datetime | float | None = None,
) -> float:
    """
    ``start``~``end`` 사이 **장이 열린 시간만** 누적(시간 단위).

    * KR — KST 09:00~15:30, 월~금
    * US — ET 09:30~16:00, 월~금
    * COIN — 24/7 (연속 시각과 동일)
    """
    m = str(market or "").strip().upper()
    if isinstance(start, (int, float)):
        start_dt = datetime.fromtimestamp(float(start), tz=KST)
    else:
        start_dt = start
    if end is None:
        end_dt = datetime.now(KST)
    elif isinstance(end, (int, float)):
        end_dt = datetime.fromtimestamp(float(end), tz=KST)
    else:
        end_dt = end

    if m == "COIN":
        return max(0.0, (end_dt - start_dt).total_seconds() / 3600.0)

    if m == "US":
        tz, session = US_EAST, US_SESSION
    else:
        tz, session = KST, KR_SESSION

    start_a = _to_aware(start_dt, tz)
    end_a = _to_aware(end_dt, tz)
    if end_a <= start_a:
        return 0.0

    open_t, close_t = session
    total_sec = 0.0
    day = start_a.date()
    last_day = end_a.date()
    while day <= last_day:
        if day.weekday() < 5:
            day_anchor = datetime.combine(day, time(12, 0), tzinfo=tz)
            total_sec += _session_overlap_seconds(
                day_anchor, tz, open_t, close_t, start_a, end_a
            )
        day += timedelta(days=1)

    return total_sec / 3600.0
