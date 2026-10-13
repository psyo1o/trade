# -*- coding: utf-8 -*-
"""섀도우 규칙 — 미장 실적 진입 차단 · 국장 지수 급락 청산.

모드 ``off`` | ``shadow`` | ``live`` (기본 shadow).

* shadow/off 는 실주문을 막거나 넣지 않는다. shadow 는 로그만 남긴다.
* live 만 기존 주문 콜백을 막거나(실적) 호출한다(국장 전량 매도).
* 실적 일자를 못 구하면 fail-open (매수는 진행, 로그만).
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
US_EAST = ZoneInfo("America/New_York")

RULE_EARNINGS = "earnings_block"
RULE_KR_CRASH = "kr_crash_exit"

# 매수일 = day 0. day 0..9 (10거래일) 안에 실적이 있으면 차단 후보.
EARNINGS_BLOCK_LAST_OFFSET = 9

# KST 15:15 포함 ~ 15:20 미만. 15분 스케줄(:00/:15/:30/:45) 중 15:15 회차만 들어온다.
KR_CRASH_WINDOW_START = (15, 15)
KR_CRASH_WINDOW_END = (15, 20)

_STATE_KEY = "shadow_rules"
_DATES_KEY = "kr_crash_evaluated_dates"
_LOG_LOCK = threading.Lock()
_LOG_PATH: Path | None = None
_LISTING_CACHE: dict[str, str] = {}

MODES = ("off", "shadow", "live")


def normalize_mode(value: Any, default: str = "shadow") -> str:
    """``off`` | ``shadow`` | ``live``. 그 외는 default."""
    raw = str(value if value is not None else default).strip().lower()
    if raw in MODES:
        return raw
    return default if default in MODES else "shadow"


def configure_shadow_events_path(path: Path | str) -> None:
    global _LOG_PATH
    _LOG_PATH = Path(path)


def shadow_events_path(override: Path | str | None = None) -> Path:
    if override is not None:
        return Path(override)
    if _LOG_PATH is not None:
        return _LOG_PATH
    return Path(__file__).resolve().parent.parent / "shadow_events.jsonl"


def to_kst(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now.astimezone(KST)


def kst_iso(now: datetime | None = None) -> str:
    return to_kst(now).isoformat(timespec="seconds")


def us_session_date(now: datetime | None = None) -> date:
    """미장 매수일의 거래일(미국 동부 달력 날짜)."""
    if now is None:
        return datetime.now(US_EAST).date()
    if now.tzinfo is None:
        return now.replace(tzinfo=US_EAST).date()
    return now.astimezone(US_EAST).date()


def in_kr_crash_window(now: datetime | None = None) -> bool:
    """KST 15:15:00 이상, 15:20:00 미만."""
    kst = to_kst(now)
    start_h, start_m = KR_CRASH_WINDOW_START
    end_h, end_m = KR_CRASH_WINDOW_END
    start = kst.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = kst.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start <= kst < end


def kr_crash_day(now: datetime | None = None) -> str:
    return to_kst(now).date().isoformat()


def kr_crash_already_evaluated(state: dict | None, day: str) -> bool:
    if not isinstance(state, dict):
        return False
    bucket = state.get(_STATE_KEY)
    if not isinstance(bucket, dict):
        return False
    dates = bucket.get(_DATES_KEY) or []
    return str(day) in dates


def mark_kr_crash_evaluated(state: dict, day: str) -> None:
    bucket = state.setdefault(_STATE_KEY, {})
    if not isinstance(bucket, dict):
        bucket = {}
        state[_STATE_KEY] = bucket
    dates = bucket.setdefault(_DATES_KEY, [])
    if not isinstance(dates, list):
        dates = []
        bucket[_DATES_KEY] = dates
    if day not in dates:
        dates.append(day)
    if len(dates) > 90:
        del dates[:-90]


def append_shadow_event(record: dict, path: Path | str | None = None) -> None:
    """append-only JSONL 한 줄."""
    target = shadow_events_path(path)
    line = json.dumps(record, ensure_ascii=False, default=str)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _event(
    *,
    rule: str,
    mode: str,
    market: str,
    ticker: str,
    details: dict,
    now: datetime | None = None,
) -> dict:
    return {
        "timestamp": kst_iso(now),
        "rule": rule,
        "mode": mode,
        "market": market,
        "ticker": ticker,
        "details": details,
    }


# ---------------------------------------------------------------------------
# 미장 실적 — 거래일 카운트
# ---------------------------------------------------------------------------

def nyse_trading_days(start: date, end: date) -> list[date]:
    """NYSE 세션 날짜. 캘린더 실패 시 월~금 근사."""
    if end < start:
        return []
    try:
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar("NYSE")
        sched = cal.schedule(start_date=start.isoformat(), end_date=end.isoformat())
        out: list[date] = []
        for ts in sched.index:
            if hasattr(ts, "date"):
                out.append(ts.date())
            else:
                out.append(date.fromisoformat(str(ts)[:10]))
        return out
    except Exception:
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out


def trading_days_until(buy_day: date, event_day: date, sessions: Sequence[date]) -> int | None:
    """매수일(day 0)부터 실적일까지 거래일 오프셋.

    매수일과 실적일이 같으면 0. 실적이 매수일보다 이전이면 None.
    ``sessions`` 에 없는 실적일(주말 등)은 그 날짜 이하의 마지막 세션까지로 센다.
    """
    if event_day < buy_day:
        return None
    sess = sorted({d for d in sessions if isinstance(d, date)})
    if buy_day in sess:
        span = [s for s in sess if buy_day <= s <= event_day]
        if span:
            return len(span) - 1
        # 실적일이 매수 세션과 다음 세션 사이(주말)면 day 0 으로 보지 않고,
        # 매수일 당일이 아니면 다음 세션 전이다. 사이 날짜는 아직 다음 거래일이 아님.
        return 0 if event_day == buy_day else None
    after = [s for s in sess if buy_day < s <= event_day]
    if event_day == buy_day:
        return 0
    if not after:
        return None
    return len(after)


def earnings_would_block(days_until: int | None) -> bool:
    return days_until is not None and 0 <= int(days_until) <= EARNINGS_BLOCK_LAST_OFFSET


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            try:
                return value.astimezone(US_EAST).date()
            except Exception:
                return value.date()
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return _as_date(value.to_pydatetime())
        except Exception:
            return None
    text = str(value).strip()
    if not text or text.lower() in ("nat", "none", "nan"):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def collect_earnings_dates(calendar: Any, earnings_frame: Any) -> list[date]:
    """yfinance calendar / earnings_dates 에서 날짜만 모은다. 네트워크 없음."""
    found: list[date] = []

    def _add(raw: Any) -> None:
        if isinstance(raw, (list, tuple)):
            for item in raw:
                d = _as_date(item)
                if d:
                    found.append(d)
            return
        d = _as_date(raw)
        if d:
            found.append(d)

    if isinstance(calendar, dict):
        raw = calendar.get("Earnings Date", calendar.get("EarningsDate"))
        if raw is not None:
            _add(raw)
    elif calendar is not None and hasattr(calendar, "columns"):
        cols = [str(c) for c in getattr(calendar, "columns", [])]
        if "Earnings Date" in cols:
            try:
                _add(list(calendar["Earnings Date"].tolist()))
            except Exception:
                pass
        else:
            try:
                idx = list(calendar.index)
                if "Earnings Date" in idx:
                    val = calendar.loc["Earnings Date"]
                    if hasattr(val, "tolist"):
                        _add(val.tolist())
                    else:
                        _add(val)
            except Exception:
                pass

    if earnings_frame is not None and hasattr(earnings_frame, "index"):
        try:
            for idx in list(earnings_frame.index):
                d = _as_date(idx)
                if d:
                    found.append(d)
        except Exception:
            pass
    return sorted(set(found))


def next_earnings_on_or_after(dates: Sequence[date], on_or_after: date) -> date | None:
    future = [d for d in dates if d >= on_or_after]
    return min(future) if future else None


@dataclass
class EarningsLookup:
    status: str  # ok | unavailable
    earnings_date: date | None = None


def lookup_earnings_from_payloads(
    calendar: Any,
    earnings_frame: Any,
    *,
    on_or_after: date,
    payloads_missing: bool = False,
) -> EarningsLookup:
    if payloads_missing or (calendar is None and earnings_frame is None):
        return EarningsLookup("unavailable")
    dates = collect_earnings_dates(calendar, earnings_frame)
    if not dates:
        # 응답은 왔으나 예정 실적이 없음 — 차단하지 않음 (fail-open 로그도 아님)
        return EarningsLookup("ok", None)
    return EarningsLookup("ok", next_earnings_on_or_after(dates, on_or_after))


def fetch_earnings_lookup(ticker: str, on_or_after: date) -> EarningsLookup:
    """yfinance 실적일. 예외·401·None 이면 unavailable (fail-open)."""
    sym = str(ticker or "").strip().upper()
    if not sym:
        return EarningsLookup("unavailable")
    try:
        from utils.yfinance_guard import yf_call

        def _load():
            import yfinance as yf

            tk = yf.Ticker(sym)
            cal = None
            ed = None
            try:
                cal = tk.calendar
            except Exception:
                cal = None
            try:
                ed = tk.earnings_dates
            except Exception:
                ed = None
            return cal, ed

        loaded = yf_call(_load, label=f"earnings_{sym}", ticker=sym)
        if loaded is None:
            return EarningsLookup("unavailable")
        cal, ed = loaded
        return lookup_earnings_from_payloads(cal, ed, on_or_after=on_or_after)
    except Exception:
        return EarningsLookup("unavailable")


@dataclass
class EarningsDecision:
    proceed: bool
    outcome: str
    mode: str
    earnings_date: date | None = None
    trading_days_until: int | None = None
    should_log: bool = False


@dataclass
class EntryResult:
    """``bool(result)`` 는 주문이 나갔는지. ``blocked`` 는 live 실적 차단."""

    ordered: bool
    blocked: bool = False

    def __bool__(self) -> bool:
        return bool(self.ordered)


def evaluate_earnings_entry(
    *,
    mode: str,
    market: str,
    lookup: EarningsLookup | None,
    buy_day: date,
    sessions: Sequence[date] | None,
) -> EarningsDecision:
    """주문 함수를 호출하지 않는 순수 판정."""
    m = normalize_mode(mode)
    mk = str(market or "").strip().upper()
    if m == "off" or mk != "US":
        return EarningsDecision(True, "off" if m == "off" else "not_us", m)
    if lookup is None or lookup.status != "ok":
        return EarningsDecision(True, "fail_open", m, should_log=True)
    if lookup.earnings_date is None:
        return EarningsDecision(True, "allow", m)
    sess = list(sessions) if sessions is not None else nyse_trading_days(
        buy_day, lookup.earnings_date + timedelta(days=3)
    )
    days = trading_days_until(buy_day, lookup.earnings_date, sess)
    if not earnings_would_block(days):
        return EarningsDecision(
            True,
            "allow",
            m,
            earnings_date=lookup.earnings_date,
            trading_days_until=days,
        )
    proceed = m != "live"
    outcome = "would_block" if m == "shadow" else "blocked"
    return EarningsDecision(
        proceed,
        outcome,
        m,
        earnings_date=lookup.earnings_date,
        trading_days_until=days,
        should_log=True,
    )


def _print_earnings(ticker: str, decision: EarningsDecision, price: float, qty: int) -> None:
    if decision.outcome == "fail_open":
        print(
            f"  ⚠️ [EARNINGS] {ticker} 실적일 조회 불가 — fail-open "
            f"(의도 가격 {price} 수량 {qty}, 매수 진행)"
        )
        return
    tag = "[SHADOW-EARNINGS]" if decision.mode == "shadow" else "[LIVE-EARNINGS]"
    ed = decision.earnings_date.isoformat() if decision.earnings_date else "-"
    days = decision.trading_days_until
    tail = "매수 진행" if decision.proceed else "매수 차단"
    print(
        f"  {tag} {ticker} price={price} qty={qty} "
        f"earnings={ed} trading_days_until={days} → {tail}"
    )


def execute_us_entry_respecting_earnings(
    *,
    mode: str,
    ticker: str,
    price: float,
    qty: int,
    place_order: Callable[[], Any],
    market: str = "US",
    buy_day: date | None = None,
    lookup: EarningsLookup | None = None,
    sessions: Sequence[date] | None = None,
    fetch_lookup: Callable[[str, date], EarningsLookup] | None = None,
    log_path: Path | str | None = None,
    now: datetime | None = None,
) -> EntryResult:
    """실적 규칙 후 ``place_order`` 를 호출할지 결정한다.

    반환값은 주문을 냈고 그 결과가 참일 때 True. live 차단이면 주문을 호출하지 않는다.
    """
    m = normalize_mode(mode)
    mk = str(market or "").strip().upper()
    day = buy_day or us_session_date(now)
    resolved = lookup
    if m != "off" and mk == "US" and resolved is None:
        getter = fetch_lookup or fetch_earnings_lookup
        try:
            resolved = getter(str(ticker), day)
        except Exception:
            resolved = EarningsLookup("unavailable")
    if m != "off" and mk == "US" and sessions is None and resolved and resolved.earnings_date:
        end = max(resolved.earnings_date, day) + timedelta(days=5)
        sessions = nyse_trading_days(day, end)
    decision = evaluate_earnings_entry(
        mode=m,
        market=mk,
        lookup=resolved,
        buy_day=day,
        sessions=sessions,
    )
    if decision.should_log:
        _print_earnings(str(ticker), decision, float(price), int(qty))
        details = {
            "price": float(price),
            "qty": int(qty),
            "earnings_date": (
                decision.earnings_date.isoformat() if decision.earnings_date else None
            ),
            "trading_days_until": decision.trading_days_until,
            "outcome": decision.outcome,
        }
        if decision.outcome == "fail_open":
            details["reason"] = "earnings_unavailable"
        append_shadow_event(
            _event(
                rule=RULE_EARNINGS,
                mode=m,
                market="US",
                ticker=str(ticker),
                details=details,
                now=now,
            ),
            log_path,
        )
    if not decision.proceed:
        return EntryResult(False, blocked=True)
    return EntryResult(bool(place_order()))


# ---------------------------------------------------------------------------
# 국장 지수 급락 청산
# ---------------------------------------------------------------------------

def normalize_listing_market(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    upper = text.upper()
    if "KOSDAQ" in upper or "코스닥" in text:
        return "KOSDAQ"
    if "KOSPI" in upper or "코스피" in text:
        return "KOSPI"
    return None


def infer_listing_market(*sources: Any) -> str | None:
    keys = ("listing_market", "rprs_mrkt_kor_name", "stockExchangeType", "mrkt_kor_name")
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in keys:
            found = normalize_listing_market(src.get(key))
            if found:
                return found
    return None


def fetch_kr_listing_market(ticker: str) -> str | None:
    """네이버 기본정보의 상장시장. 실패 시 None (KODEX200만 적용)."""
    code = "".join(ch for ch in str(ticker or "") if ch.isdigit()).zfill(6)
    if not code or code == "000000":
        return None
    cached = _LISTING_CACHE.get(code)
    if cached:
        return cached
    try:
        import requests

        res = requests.get(
            f"https://m.stock.naver.com/api/stock/{code}/basic",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6,
        )
        if res.status_code >= 400:
            return None
        data = res.json()
        found = normalize_listing_market((data or {}).get("stockExchangeType"))
        if found:
            _LISTING_CACHE[code] = found
        return found
    except Exception:
        return None


def indexes_for_listing(listing: str | None) -> list[str]:
    """KODEX200 은 항상, 상장시장 지수는 KOSPI/KOSDAQ 일 때만."""
    names = ["KODEX200"]
    market = normalize_listing_market(listing) or (
        str(listing).strip().upper() if listing else ""
    )
    if market == "KOSPI":
        names.append("KOSPI")
    elif market == "KOSDAQ":
        names.append("KOSDAQ")
    return names


def crash_triggers(
    listing: str | None,
    changes: dict[str, float | None],
    threshold: float,
) -> list[dict]:
    """등락률이 임계(기본 -3%) 이하인 지수. None(조회 실패)은 발동하지 않는다."""
    out: list[dict] = []
    for name in indexes_for_listing(listing):
        raw = changes.get(name) if isinstance(changes, dict) else None
        if raw is None:
            continue
        try:
            pct = float(raw)
        except (TypeError, ValueError):
            continue
        if pct <= float(threshold):
            out.append({"index": name, "change_pct": pct})
    return out


def unrealized_pct(price: float, avg: float) -> float | None:
    try:
        p = float(price)
        a = float(avg)
    except (TypeError, ValueError):
        return None
    if a <= 0:
        return None
    return (p - a) / a * 100.0


def fetch_kr_crash_index_changes() -> dict[str, float | None]:
    """KODEX200 은 매수 급락 필터와 같은 티커·공식. KOSPI/KOSDAQ 도 동일 공식."""
    from strategy.market_benchmark import (
        KR_LISTING_INDEX_YF,
        benchmark_ticker,
        symbol_daily_change_pct,
    )

    out: dict[str, float | None] = {
        "KODEX200": symbol_daily_change_pct(benchmark_ticker("KR")),
    }
    for name, symbol in KR_LISTING_INDEX_YF.items():
        out[name] = symbol_daily_change_pct(symbol)
    return out


@dataclass
class KrHolding:
    ticker: str
    qty: int
    price: float
    avg_price: float
    listing_market: str | None = None


@dataclass
class CrashAction:
    ticker: str
    qty: int
    price: float
    avg_price: float
    unrealized_pct: float | None
    listing_market: str | None
    triggers: list[dict] = field(default_factory=list)
    indexes: dict[str, float | None] = field(default_factory=dict)
    sold: bool = False


@dataclass
class CrashEvalResult:
    status: str
    actions: list[CrashAction] = field(default_factory=list)
    marked: bool = False


def _fmt_px(value: float) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _print_crash(action: CrashAction, mode: str) -> None:
    tag = "[SHADOW-KRCRASH]" if mode == "shadow" else "[LIVE-KRCRASH]"
    trig = ", ".join(
        f"{t['index']} {float(t['change_pct']):+.2f}%" for t in action.triggers
    )
    ur = action.unrealized_pct
    ur_s = f"{ur:+.2f}%" if ur is not None else "n/a"
    if mode == "shadow":
        tail = "매도 안 함"
    elif action.sold:
        tail = "시장가 전량 매도"
    else:
        tail = "매도 실패"
    print(
        f"  {tag} {action.ticker} qty={int(action.qty)} "
        f"price={_fmt_px(action.price)} avg={_fmt_px(action.avg_price)} "
        f"unrealized={ur_s} trigger={trig} → {tail}"
    )


def apply_kr_crash_exit(
    *,
    mode: str,
    now: datetime,
    state: dict,
    holdings: Sequence[KrHolding],
    index_changes: dict[str, float | None] | None = None,
    fetch_indexes: Callable[[], dict[str, float | None]] | None = None,
    resolve_listing: Callable[[str], str | None] | None = None,
    place_sell: Callable[[CrashAction], bool] | None = None,
    threshold: float = -3.0,
    log_path: Path | str | None = None,
) -> CrashEvalResult:
    """창·일자 멱등 후 보유별 발동. live 만 ``place_sell`` 호출.

    보유가 없으면 일자를 소모하지 않는다. 지수 조회 예외면 일자도 소모하지 않는다.
    """
    m = normalize_mode(mode)
    if m == "off":
        return CrashEvalResult("off")
    if not in_kr_crash_window(now):
        return CrashEvalResult("outside_window")
    day = kr_crash_day(now)
    if kr_crash_already_evaluated(state, day):
        print(f"  [KRCRASH] {day} 이미 평가함 — 스킵")
        return CrashEvalResult("already_evaluated")
    rows = [h for h in holdings if int(getattr(h, "qty", 0) or 0) > 0]
    if not rows:
        return CrashEvalResult("no_holdings")

    changes = index_changes
    if changes is None:
        getter = fetch_indexes or fetch_kr_crash_index_changes
        try:
            changes = getter()
        except Exception as exc:
            print(f"  ⚠️ [KRCRASH] 지수 조회 실패 — 청산 없음 ({type(exc).__name__}: {exc})")
            return CrashEvalResult("fetch_failed")
    if not isinstance(changes, dict):
        changes = {}

    mark_kr_crash_evaluated(state, day)
    actions: list[CrashAction] = []
    idx_txt = ", ".join(
        f"{name}={('조회실패' if changes.get(name) is None else f'{float(changes.get(name)):+.2f}%')}"
        for name in ("KODEX200", "KOSPI", "KOSDAQ")
    )
    print(f"  [KRCRASH] 평가 {kst_iso(now)} mode={m} {idx_txt}")

    for holding in rows:
        listing = normalize_listing_market(holding.listing_market) or holding.listing_market
        if not listing and resolve_listing is not None:
            try:
                listing = normalize_listing_market(resolve_listing(holding.ticker))
            except Exception:
                listing = None
        triggers = crash_triggers(listing, changes, threshold)
        if not triggers:
            continue
        ur = unrealized_pct(holding.price, holding.avg_price)
        action = CrashAction(
            ticker=str(holding.ticker),
            qty=int(holding.qty),
            price=float(holding.price),
            avg_price=float(holding.avg_price),
            unrealized_pct=ur,
            listing_market=listing if isinstance(listing, str) else None,
            triggers=triggers,
            indexes={k: changes.get(k) for k in ("KODEX200", "KOSPI", "KOSDAQ")},
        )
        outcome = "would_sell"
        if m == "live":
            ok = False
            if place_sell is not None and action.price > 0 and action.qty > 0:
                try:
                    ok = bool(place_sell(action))
                except Exception as exc:
                    print(
                        f"  ❌ [LIVE-KRCRASH] {action.ticker} 주문 예외: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    ok = False
            action.sold = ok
            outcome = "sold" if ok else "sell_failed"
        _print_crash(action, m)
        append_shadow_event(
            _event(
                rule=RULE_KR_CRASH,
                mode=m,
                market="KR",
                ticker=action.ticker,
                details={
                    "qty": action.qty,
                    "price": action.price,
                    "avg_price": action.avg_price,
                    "unrealized_pct": action.unrealized_pct,
                    "listing_market": action.listing_market,
                    "triggers": action.triggers,
                    "indexes": action.indexes,
                    "outcome": outcome,
                },
                now=now,
            ),
            log_path,
        )
        actions.append(action)
    return CrashEvalResult("evaluated", actions, marked=True)
