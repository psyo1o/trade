# -*- coding: utf-8 -*-
"""실적 차단·국장 급락 청산 — 창 계산, 임계, shadow/live/off."""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from execution.guard import compute_ticker_cooldown_hours
from execution.kr_crash_exit import apply_kr_crash_exit_for_holding
from strategy.earnings_gate import (
    block_sessions,
    decide_earnings_action,
    earnings_in_window,
    format_shadow_earnings_log,
    next_earnings_on_or_after,
    to_us_eastern_date,
    us_earnings_entry_blocked,
    us_session_date,
    weekday_sessions,
)
from strategy.kr_crash_exit import (
    change_hits_threshold,
    crash_trigger_detail,
    format_shadow_krcrash_log,
    in_kr_close_crash_window,
    normalize_listing,
    parse_listing_payload,
    plan_kr_crash,
)
from strategy.market_benchmark import index_daily_change_pct
from strategy.rule_modes import normalize_rule_mode


_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")


def _sessions_from(start: date, n: int = 12, skip: set[date] | None = None) -> list[date]:
    skip = skip or set()
    out: list[date] = []
    cursor = start
    while len(out) < n:
        if cursor.weekday() < 5 and cursor not in skip:
            out.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return out


def test_mode_normalize_defaults_and_typos():
    assert normalize_rule_mode(None) == "shadow"
    assert normalize_rule_mode("") == "shadow"
    assert normalize_rule_mode("OFF") == "off"
    assert normalize_rule_mode("Live") == "live"
    assert normalize_rule_mode("SHADOW") == "shadow"
    assert normalize_rule_mode("liv") == "shadow"
    assert normalize_rule_mode(None, default="off") == "off"


def test_earnings_window_includes_same_day_and_tenth_session():
    as_of = date(2026, 10, 13)  # Tuesday
    holiday = date(2026, 10, 15)
    calendar = _sessions_from(as_of, n=12, skip={holiday})
    window = block_sessions(as_of, calendar, 10)
    assert window[0] == as_of
    assert holiday not in window
    assert len(window) == 10
    assert earnings_in_window(as_of, window)  # 당일·장후 포함
    assert earnings_in_window(window[-1], window)
    assert not earnings_in_window(date.fromordinal(window[-1].toordinal() + 1), window)
    assert not earnings_in_window(date(2026, 10, 12), window)
    assert next_earnings_on_or_after([date(2026, 10, 1), as_of, window[-1]], as_of) == as_of


def test_same_day_after_close_timestamp_is_buy_day():
    # 2026-10-13 20:05 UTC = 16:05 EDT (장후)
    stamp = pd.Timestamp("2026-10-13 20:05", tz="UTC")
    assert to_us_eastern_date(stamp) == date(2026, 10, 13)
    naive = datetime(2026, 10, 13, 16, 5)
    assert to_us_eastern_date(naive) == date(2026, 10, 13)


def test_us_session_date_uses_eastern_not_kst_morning():
    # KST 2026-10-14 05:00 = EDT 2026-10-13 16:00 (아직 미국 13일)
    kst = datetime(2026, 10, 14, 5, 0, tzinfo=_KST)
    assert us_session_date(kst) == date(2026, 10, 13)
    naive_kst = datetime(2026, 10, 14, 5, 0)
    assert us_session_date(naive_kst) == date(2026, 10, 13)


def test_weekday_fallback_skips_weekend():
    # Friday + 10 weekdays includes the following week's days, not Sat/Sun
    days = weekday_sessions(date(2026, 10, 16), 10)  # Friday
    assert days[0] == date(2026, 10, 16)
    assert date(2026, 10, 17) not in days
    assert date(2026, 10, 18) not in days
    assert len(days) == 10
    assert days[1] == date(2026, 10, 19)


def test_earnings_action_off_shadow_live_fail_open():
    assert decide_earnings_action("off", in_window=True, data_ok=True) == "noop"
    assert decide_earnings_action("shadow", in_window=False, data_ok=True) == "allow"
    assert decide_earnings_action("shadow", in_window=True, data_ok=False) == "fail_open"
    assert decide_earnings_action("live", in_window=True, data_ok=False) == "fail_open"
    assert decide_earnings_action("shadow", in_window=True, data_ok=True) == "shadow"
    assert decide_earnings_action("live", in_window=True, data_ok=True) == "block"
    assert decide_earnings_action("nope", in_window=True, data_ok=True) == "shadow"


def test_earnings_entry_blocked_branches(tmp_path, capsys):
    as_of = date(2026, 10, 13)
    window = block_sessions(as_of, _sessions_from(as_of, 12), 10)
    calls: list[str] = []

    def fetcher(ticker: str):
        calls.append(ticker)
        return [as_of], True

    events = tmp_path / "shadow_events.jsonl"
    assert (
        us_earnings_entry_blocked(
            "off",
            "AAPL",
            100.0,
            "TREND_V8",
            as_of=as_of,
            sessions=window,
            fetcher=fetcher,
            events_path=events,
            use_cache=False,
        )
        is False
    )
    assert calls == []
    assert not events.exists()

    calls.clear()
    assert (
        us_earnings_entry_blocked(
            "shadow",
            "AAPL",
            180.5,
            "SWING_FIB",
            as_of=as_of,
            sessions=window,
            fetcher=fetcher,
            events_path=events,
            use_cache=False,
        )
        is False
    )
    assert calls == ["AAPL"]
    out = capsys.readouterr().out
    assert format_shadow_earnings_log("AAPL", as_of) in out
    row = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    assert row["action"] == "would_block"
    assert row["rule"] == "earnings_block"
    assert row["mode"] == "shadow"
    assert row["market"] == "US"
    assert row["earnings_date"] == "2026-10-13"

    assert (
        us_earnings_entry_blocked(
            "live",
            "MSFT",
            400.0,
            "TREND_V8",
            as_of=as_of,
            sessions=window,
            fetcher=fetcher,
            events_path=events,
            use_cache=False,
        )
        is True
    )
    live = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    assert live["action"] == "block"
    assert live["ticker"] == "MSFT"
    assert "EARNINGS-LIVE" in capsys.readouterr().out

    def broken(_ticker: str):
        return None, False

    assert (
        us_earnings_entry_blocked(
            "live",
            "NVDA",
            10.0,
            "TREND_V8",
            as_of=as_of,
            sessions=window,
            fetcher=broken,
            events_path=events,
            use_cache=False,
        )
        is False
    )
    warn = capsys.readouterr().out
    assert "fail-open" in warn
    fail = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    assert fail["action"] == "fail_open"


def test_crash_threshold_and_listing_scope():
    assert change_hits_threshold(-3.0) is True
    assert change_hits_threshold(-3.01) is True
    assert change_hits_threshold(-2.99) is False
    assert change_hits_threshold(0.0) is False
    assert change_hits_threshold(None) is False

    hit, label, chg = crash_trigger_detail("KOSPI", -3.0, -1.0, -1.0)
    assert hit and "KODEX200" in label and chg == -3.0
    hit_q, _, _ = crash_trigger_detail("KOSDAQ", -1.0, -4.0, -3.2)
    assert hit_q
    hit_p, _, _ = crash_trigger_detail("KOSPI", -1.0, -4.0, -3.2)
    assert hit_p
    miss, _, _ = crash_trigger_detail("KOSPI", -1.0, -1.0, -5.0)
    assert not miss
    unknown, _, _ = crash_trigger_detail("", -1.0, -4.0, -4.0)
    assert not unknown
    unknown_kodex, _, _ = crash_trigger_detail("", -3.5, None, None)
    assert unknown_kodex

    assert plan_kr_crash("off", listing="KOSPI", kodex=-5, kospi=-5, kosdaq=-5).action == "noop"
    assert plan_kr_crash("shadow", listing="KOSDAQ", kodex=-1, kospi=-1, kosdaq=-3).action == "shadow"
    assert plan_kr_crash("live", listing="KOSPI", kodex=-3, kospi=-1, kosdaq=-1).action == "live"
    assert plan_kr_crash("shadow", listing="KOSPI", kodex=-2.9, kospi=-2.9, kosdaq=-2.9).action == "noop"


def test_close_window_bounds():
    assert in_kr_close_crash_window(datetime(2026, 10, 13, 15, 14, 59, tzinfo=_KST)) is False
    assert in_kr_close_crash_window(datetime(2026, 10, 13, 15, 15, 0, tzinfo=_KST)) is True
    assert in_kr_close_crash_window(datetime(2026, 10, 13, 15, 20, 59, tzinfo=_KST)) is True
    assert in_kr_close_crash_window(datetime(2026, 10, 13, 15, 21, 0, tzinfo=_KST)) is False
    assert in_kr_close_crash_window(datetime(2026, 10, 13, 15, 16)) is True
    # ET 02:16 = KST 15:16 during KST (EDT is UTC-4, KST UTC+9 → 13h ahead)
    et = datetime(2026, 10, 13, 2, 16, tzinfo=_ET)
    assert in_kr_close_crash_window(et) is True


def test_listing_parser_and_log_line():
    assert parse_listing_payload({"stockExchangeType": {"code": "KS", "name": "KOSPI"}}) == "KOSPI"
    assert parse_listing_payload({"stockExchangeType": {"nameKor": "코스닥", "code": "KQ"}}) == "KOSDAQ"
    assert parse_listing_payload({"stockExchangeType": "KOSDAQ"}) == "KOSDAQ"
    assert parse_listing_payload({}) == ""
    assert normalize_listing("코스피") == "KOSPI"
    line = format_shadow_krcrash_log("005930", 10, 70000, -1.2)
    assert line == "[SHADOW-KRCRASH] would sell 005930 10 at 70000, -1.20%"


def test_apply_crash_shadow_does_not_sell_live_does(tmp_path, capsys):
    events = tmp_path / "shadow_events.jsonl"
    sold = {"n": 0}

    def sell():
        sold["n"] += 1
        return True

    looked = {"n": 0}

    def listing(_code):
        looked["n"] += 1
        return "KOSDAQ"

    snap = SimpleNamespace(kodex=-1.0, kospi=-0.5, kosdaq=-3.4)
    stock = {"hldg_qty": "3", "prpr": "15000", "pchs_avg_prc": "14000"}
    state = {"positions": {"035720": {"buy_p": 14000, "strategy_type": "TREND_V8"}}}

    def rb_for(mode: str):
        return SimpleNamespace(
            KR_CRASH_EXIT_MODE=mode,
            INDEX_CRASH_KR=-3.0,
            SHADOW_EVENTS_PATH=str(events),
            _to_float=lambda v, default=0.0: float(v if v not in (None, "") else default),
            _resolve_sell_loop_strategy_type=lambda pos: pos.get("strategy_type") or "TREND_V8",
            get_kr_company_name=lambda _t: "카카오",
        )

    off = apply_kr_crash_exit_for_holding(
        rb_for("off"),
        state=state,
        stock=stock,
        ticker="035720",
        ohlcv=None,
        snap=snap,
        cycle_tag="t",
        listing_lookup=listing,
        sell_fn=sell,
        events_path=events,
    )
    assert off == "noop"
    assert looked["n"] == 0 and sold["n"] == 0
    assert not events.exists()

    shadow = apply_kr_crash_exit_for_holding(
        rb_for("shadow"),
        state=state,
        stock=stock,
        ticker="035720",
        ohlcv=None,
        snap=snap,
        cycle_tag="t",
        listing_lookup=listing,
        sell_fn=sell,
        events_path=events,
    )
    assert shadow == "shadow"
    assert sold["n"] == 0 and looked["n"] == 1
    row = json.loads(events.read_text(encoding="utf-8").splitlines()[-1])
    assert row["action"] == "would_sell"
    assert row["market"] == "KR"
    assert row["position_pnl_pct"] is not None
    assert "[SHADOW-KRCRASH] would sell 035720 3 at 15000" in capsys.readouterr().out

    live = apply_kr_crash_exit_for_holding(
        rb_for("live"),
        state=state,
        stock=stock,
        ticker="035720",
        ohlcv=None,
        snap=snap,
        cycle_tag="t",
        listing_lookup=listing,
        sell_fn=sell,
        events_path=events,
    )
    assert live == "sold"
    assert sold["n"] == 1
    # 수익이어도 급락 청산은 24h 쿨다운
    hours = compute_ticker_cooldown_hours(
        reason="[KR-CRASH] 지수 급락 청산 KODEX200 -3.00%",
        profit_rate=4.0,
        remaining_qty=0.0,
    )
    assert hours == 24.0


def test_index_change_none_on_short_series(monkeypatch):
    monkeypatch.setattr(
        "strategy.market_benchmark._fetch_yf_closes",
        lambda *_a, **_k: pd.Series([100.0]),
    )
    assert index_daily_change_pct("069500.KS") is None
    monkeypatch.setattr(
        "strategy.market_benchmark._fetch_yf_closes",
        lambda *_a, **_k: pd.Series([100.0, 97.0]),
    )
    assert index_daily_change_pct("^KS11") == -3.0


def test_forward_returns_vs_event_price():
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "analysis"
    sys.path.insert(0, str(root))
    from trade_review.shadow_outcome import forward_returns, interpret

    rows = [
        {"date": "2026-10-13", "close": 100},
        {"date": "2026-10-14", "close": 101},
        {"date": "2026-10-15", "close": 97},
        {"date": "2026-10-16", "close": 110},
        {"date": "2026-10-19", "close": 95},
        {"date": "2026-10-20", "close": 80},
    ]
    rets = forward_returns(rows, "2026-10-13", 100)
    assert rets["ret_1d"] == 1.0
    assert rets["ret_3d"] == 10.0
    assert rets["ret_5d"] == -20.0
    assert "하락" in interpret("kr_crash_exit", "would_sell", rets)
    assert "기회비용" in interpret(
        "earnings_block", "would_block", {"ok": True, "ret_5d": 10.0}
    )
    empty = forward_returns(rows[:1], "2026-10-13", 100)
    assert empty["ok"] is False
