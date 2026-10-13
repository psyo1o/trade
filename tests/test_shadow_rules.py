# -*- coding: utf-8 -*-
"""섀도우 규칙 — 거래일 카운트, fail-open, 마감 창 멱등, 지수 선택, 모드별 주문."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from strategy.market_benchmark import pair_change_pct
from strategy.shadow_report import analyze_files, write_csv
from strategy.shadow_rules import (
    EarningsLookup,
    KrHolding,
    apply_kr_crash_exit,
    collect_earnings_dates,
    crash_triggers,
    earnings_would_block,
    execute_us_entry_respecting_earnings,
    in_kr_crash_window,
    indexes_for_listing,
    kr_crash_already_evaluated,
    normalize_mode,
    trading_days_until,
)

KST = ZoneInfo("Asia/Seoul")


def _kst(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 13, hour, minute, second, tzinfo=KST)


def _weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def test_pair_change_pct_matches_buy_filter_formula():
    assert pair_change_pct(100, 97) == pytest.approx(-3.0)
    assert pair_change_pct(0, 10) is None


def test_trading_days_include_buy_day_and_skip_holiday():
    # 2026-10-12 월요일 = day 0
    sessions = _weekdays(date(2026, 10, 12), 12)
    buy = sessions[0]
    assert trading_days_until(buy, buy, sessions) == 0
    assert earnings_would_block(0) is True
    assert trading_days_until(buy, sessions[9], sessions) == 9
    assert earnings_would_block(9) is True
    assert trading_days_until(buy, sessions[10], sessions) == 10
    assert earnings_would_block(10) is False
    # 금요일 매수 → 다음 월요일 = 1거래일
    friday = date(2026, 10, 16)
    monday = date(2026, 10, 19)
    assert trading_days_until(friday, monday, sessions) == 1
    # 목요일 휴장: 월~수, 금. 월→금 은 3 (월 0, 화 1, 수 2, 금 3)
    holiday_sessions = [
        date(2026, 10, 12),
        date(2026, 10, 13),
        date(2026, 10, 14),
        date(2026, 10, 16),
    ]
    assert trading_days_until(date(2026, 10, 12), date(2026, 10, 16), holiday_sessions) == 3
    assert trading_days_until(buy, date(2026, 10, 1), sessions) is None


def test_earnings_payload_parser_without_network():
    cal = {"Earnings Date": [date(2026, 10, 20), date(2026, 10, 21)]}
    assert collect_earnings_dates(cal, None)[0] == date(2026, 10, 20)
    assert collect_earnings_dates(None, None) == []


def test_earnings_fail_open_and_modes(tmp_path, capsys):
    log = tmp_path / "shadow_events.jsonl"
    buy = date(2026, 10, 13)
    sessions = _weekdays(buy, 12)
    calls: list[str] = []

    def place_order():
        calls.append("buy")
        return True

    # 데이터 없음 → 주문은 나간다
    calls.clear()
    ok = execute_us_entry_respecting_earnings(
        mode="live",
        ticker="AAPL",
        price=190.5,
        qty=3,
        place_order=place_order,
        buy_day=buy,
        lookup=EarningsLookup("unavailable"),
        sessions=sessions,
        log_path=log,
    )
    assert ok
    assert calls == ["buy"]
    fail_rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert fail_rows[-1]["details"]["outcome"] == "fail_open"
    assert "fail-open" in capsys.readouterr().out

    # day 0 live: 주문 함수를 호출하지 않음
    calls.clear()
    ok = execute_us_entry_respecting_earnings(
        mode="live",
        ticker="AAPL",
        price=190.5,
        qty=3,
        place_order=place_order,
        buy_day=buy,
        lookup=EarningsLookup("ok", buy),
        sessions=sessions,
        log_path=log,
    )
    assert not ok
    assert ok.blocked is True
    assert calls == []

    # shadow: 주문은 그대로, 태그는 SHADOW-EARNINGS
    calls.clear()
    ok = execute_us_entry_respecting_earnings(
        mode="shadow",
        ticker="MSFT",
        price=400,
        qty=1,
        place_order=place_order,
        buy_day=buy,
        lookup=EarningsLookup("ok", sessions[9]),
        sessions=sessions,
        log_path=log,
    )
    assert ok
    assert calls == ["buy"]
    assert "[SHADOW-EARNINGS]" in capsys.readouterr().out
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    shadow = [r for r in rows if r["ticker"] == "MSFT"][0]
    assert shadow["rule"] == "earnings_block"
    assert shadow["mode"] == "shadow"
    assert shadow["market"] == "US"
    assert shadow["details"]["trading_days_until"] == 9
    assert shadow["timestamp"].endswith("+09:00")

    # day 10 은 차단 후보가 아님 — 추가 로그 없이 주문
    before = len(rows)
    calls.clear()
    ok = execute_us_entry_respecting_earnings(
        mode="live",
        ticker="NVDA",
        price=100,
        qty=2,
        place_order=place_order,
        buy_day=buy,
        lookup=EarningsLookup("ok", sessions[10]),
        sessions=sessions,
        log_path=log,
    )
    assert ok
    assert calls == ["buy"]
    after = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(after) == before

    # off: 실적이 당일이어도 로그·차단 없음
    off_log = tmp_path / "off.jsonl"
    calls.clear()
    ok = execute_us_entry_respecting_earnings(
        mode="off",
        ticker="AAPL",
        price=1,
        qty=1,
        place_order=place_order,
        buy_day=buy,
        lookup=EarningsLookup("ok", buy),
        sessions=sessions,
        log_path=off_log,
    )
    assert ok
    assert calls == ["buy"]
    assert not off_log.exists()

    # 국장·코인에는 적용하지 않음
    calls.clear()
    ok = execute_us_entry_respecting_earnings(
        mode="live",
        market="KR",
        ticker="005930",
        price=70000,
        qty=1,
        place_order=place_order,
        buy_day=buy,
        lookup=EarningsLookup("ok", buy),
        sessions=sessions,
        log_path=off_log,
    )
    assert ok and calls == ["buy"]
    calls.clear()
    ok = execute_us_entry_respecting_earnings(
        mode="live",
        market="COIN",
        ticker="KRW-BTC",
        price=1,
        qty=1,
        place_order=place_order,
        buy_day=buy,
        lookup=EarningsLookup("ok", buy),
        sessions=sessions,
        log_path=off_log,
    )
    assert ok and calls == ["buy"]
    assert not off_log.exists()


def test_close_window_edges_and_idempotency(tmp_path):
    assert in_kr_crash_window(_kst(15, 14, 59)) is False
    assert in_kr_crash_window(_kst(15, 15, 0)) is True
    assert in_kr_crash_window(_kst(15, 19, 59)) is True
    assert in_kr_crash_window(_kst(15, 20, 0)) is False

    holding = KrHolding("005930", 10, 70000, 68000, "KOSPI")
    changes = {"KODEX200": -3.2, "KOSPI": -1.0, "KOSDAQ": -0.5}
    state: dict = {}
    sells: list[str] = []
    log = tmp_path / "shadow_events.jsonl"

    def place_sell(action):
        sells.append(action.ticker)
        return True

    first = apply_kr_crash_exit(
        mode="live",
        now=_kst(15, 16),
        state=state,
        holdings=[holding],
        index_changes=changes,
        place_sell=place_sell,
        log_path=log,
    )
    assert first.status == "evaluated"
    assert first.marked is True
    assert sells == ["005930"]
    assert kr_crash_already_evaluated(state, "2026-10-13")

    second = apply_kr_crash_exit(
        mode="live",
        now=_kst(15, 18),
        state=state,
        holdings=[holding],
        index_changes=changes,
        place_sell=place_sell,
        log_path=log,
    )
    assert second.status == "already_evaluated"
    assert sells == ["005930"]
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    # 창 밖은 일자를 소모하지 않는다
    fresh: dict = {}
    outside = apply_kr_crash_exit(
        mode="live",
        now=_kst(15, 14),
        state=fresh,
        holdings=[holding],
        index_changes=changes,
        place_sell=place_sell,
        log_path=log,
    )
    assert outside.status == "outside_window"
    assert not kr_crash_already_evaluated(fresh, "2026-10-13")
    assert sells == ["005930"]


def test_index_selection_per_listing_market(tmp_path):
    assert indexes_for_listing("KOSPI") == ["KODEX200", "KOSPI"]
    assert indexes_for_listing("KOSDAQ") == ["KODEX200", "KOSDAQ"]
    assert indexes_for_listing(None) == ["KODEX200"]
    assert crash_triggers("KOSPI", {"KODEX200": -1, "KOSPI": -1, "KOSDAQ": -5}, -3) == []
    kosdaq_only = crash_triggers("KOSDAQ", {"KODEX200": -1, "KOSPI": -1, "KOSDAQ": -3.0}, -3)
    assert [t["index"] for t in kosdaq_only] == ["KOSDAQ"]
    # 조회 실패(None)는 급락으로 보지 않는다
    assert crash_triggers("KOSPI", {"KODEX200": None, "KOSPI": None}, -3) == []

    log = tmp_path / "e.jsonl"
    changes = {"KODEX200": -1.0, "KOSPI": -0.5, "KOSDAQ": -4.0}
    sold: list[str] = []

    def place_sell(action):
        sold.append(action.ticker)
        return True

    result = apply_kr_crash_exit(
        mode="live",
        now=_kst(15, 15),
        state={},
        holdings=[
            KrHolding("005930", 1, 70000, 80000, "KOSPI"),  # 손실
            KrHolding("035720", 2, 50000, 40000, "KOSDAQ"),  # 수익
        ],
        index_changes=changes,
        place_sell=place_sell,
        log_path=log,
    )
    assert sold == ["035720"]
    assert result.actions[0].unrealized_pct == pytest.approx(25.0)

    both = apply_kr_crash_exit(
        mode="shadow",
        now=_kst(15, 15),
        state={},
        holdings=[
            KrHolding("005930", 1, 70000, 68000, "KOSPI"),
            KrHolding("035720", 2, 50000, 40000, "KOSDAQ"),
        ],
        index_changes={"KODEX200": -3.0, "KOSPI": -0.2, "KOSDAQ": -0.2},
        place_sell=place_sell,
        log_path=log,
    )
    assert [a.ticker for a in both.actions] == ["005930", "035720"]
    assert sold == ["035720"]  # shadow 는 주문을 호출하지 않음
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    shadow_rows = [r for r in rows if r["mode"] == "shadow"]
    assert len(shadow_rows) == 2
    assert shadow_rows[0]["details"]["triggers"][0]["index"] == "KODEX200"


def test_kr_crash_modes_do_not_order_unless_live(tmp_path, capsys):
    holding = KrHolding("005930", 5, 70000, 71000, "KOSPI")
    changes = {"KODEX200": -3.5, "KOSPI": -3.5, "KOSDAQ": 0.0}
    log = tmp_path / "m.jsonl"
    calls: list[str] = []

    def place_sell(action):
        calls.append(action.ticker)
        return True

    off = apply_kr_crash_exit(
        mode="off",
        now=_kst(15, 15),
        state={},
        holdings=[holding],
        index_changes=changes,
        place_sell=place_sell,
        log_path=log,
    )
    assert off.status == "off"
    assert calls == []
    assert not log.exists()

    state: dict = {}
    shadow = apply_kr_crash_exit(
        mode="shadow",
        now=_kst(15, 17),
        state=state,
        holdings=[holding],
        index_changes=changes,
        place_sell=place_sell,
        log_path=log,
    )
    assert shadow.status == "evaluated"
    assert calls == []
    out = capsys.readouterr().out
    assert "[SHADOW-KRCRASH]" in out
    assert "005930" in out and "unrealized=" in out
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["rule"] == "kr_crash_exit"
    assert row["details"]["outcome"] == "would_sell"
    assert row["details"]["qty"] == 5

    calls.clear()
    live_state: dict = {}
    live = apply_kr_crash_exit(
        mode="live",
        now=_kst(15, 17),
        state=live_state,
        holdings=[holding],
        index_changes=changes,
        place_sell=place_sell,
        log_path=log,
    )
    assert live.actions[0].sold is True
    assert calls == ["005930"]
    assert "[LIVE-KRCRASH]" in capsys.readouterr().out


def test_cycle_helper_orders_only_in_live(monkeypatch, tmp_path):
    """kr_cycle 헬퍼가 live 에서만 기존 매도 주문 함수를 호출한다. 네트워크 없음."""
    import strategy.shadow_rules as rules
    from execution.market_cycles.context import TradingCycleContext
    from execution.market_cycles.kr_cycle import _apply_kr_crash_exit

    monkeypatch.setattr(
        rules,
        "fetch_kr_crash_index_changes",
        lambda: {"KODEX200": -4.0, "KOSPI": -0.5, "KOSDAQ": -0.5},
    )
    monkeypatch.setattr(
        rules,
        "fetch_kr_listing_market",
        lambda _t: (_ for _ in ()).throw(AssertionError("listing network")),
    )
    monkeypatch.setattr(rules, "_LOG_PATH", tmp_path / "shadow_events.jsonl")

    ctx = TradingCycleContext(
        state={"positions": {"005930": {"buy_p": 68000, "strategy_type": "TREND_V8"}}},
        weather={"KR": "BULL"},
        macro_mult=1.0,
        macro_reason="",
        macro_snap={},
        buy_cycle_tag="t",
        cycle_started_kst=_kst(15, 16),
    )
    stock = {
        "pdno": "005930",
        "hldg_qty": "3",
        "prpr": "70000",
        "pchs_avg_prc": "68000",
        "rprs_mrkt_kor_name": "KOSPI",
    }

    def _rb(mode: str):
        rb = MagicMock()
        rb.KR_CRASH_EXIT_MODE = mode
        rb.INDEX_CRASH_KR = -3.0
        rb.normalize_ticker = lambda raw: str(raw)
        rb._to_float = lambda value, default=0.0: float(value if value not in (None, "") else default)
        rb._resolve_curr_price_with_gui_override = lambda _pos, px: px
        rb._resolve_sell_loop_strategy_type = lambda _pos: "TREND_V8"
        rb.get_kr_company_name = lambda _t: "삼성전자"
        rb.order_idem.LANE_KR_CRASH = "kr_crash"
        order = MagicMock(return_value={"rt_cd": "0"})
        rb.create_market_sell_order_kis = order

        def _idem(_state, **kwargs):
            kwargs["place_order"]()
            return SimpleNamespace(ok=True, note="")

        rb._idempotent_kis_sell.side_effect = _idem
        return rb, order

    rb_off, order_off = _rb("off")
    assert _apply_kr_crash_exit(rb_off, ctx, ctx.state, [stock], {"005930"}) == set()
    order_off.assert_not_called()

    rb_shadow, order_shadow = _rb("shadow")
    fresh = {"positions": {"005930": {"buy_p": 68000}}}
    assert _apply_kr_crash_exit(rb_shadow, ctx, fresh, [stock], {"005930"}) == set()
    order_shadow.assert_not_called()
    assert (tmp_path / "shadow_events.jsonl").is_file()

    rb_live, order_live = _rb("live")
    live_state = {"positions": {"005930": {"buy_p": 68000, "strategy_type": "TREND_V8"}}}
    sold = _apply_kr_crash_exit(rb_live, ctx, live_state, [stock], {"005930"})
    assert sold == {"005930"}
    order_live.assert_called_once()
    # 같은 날짜 재진입은 주문을 다시 넣지 않는다
    order_live.reset_mock()
    again = _apply_kr_crash_exit(rb_live, ctx, live_state, [stock], {"005930"})
    assert again == set()
    order_live.assert_not_called()


def test_shadow_report_counterfactual(tmp_path):
    events = tmp_path / "shadow_events.jsonl"
    history = tmp_path / "trade_history.json"
    events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-10-13T22:00:00+09:00",
                        "rule": "earnings_block",
                        "mode": "shadow",
                        "market": "US",
                        "ticker": "AAPL",
                        "details": {
                            "price": 190.5,
                            "qty": 3,
                            "earnings_date": "2026-10-20",
                            "trading_days_until": 5,
                            "outcome": "would_block",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-10-13T15:16:00+09:00",
                        "rule": "kr_crash_exit",
                        "mode": "shadow",
                        "market": "KR",
                        "ticker": "005930",
                        "details": {
                            "qty": 10,
                            "price": 70000,
                            "avg_price": 68000,
                            "unrealized_pct": 2.94,
                            "outcome": "would_sell",
                            "triggers": [{"index": "KODEX200", "change_pct": -3.2}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    history.write_text(
        json.dumps(
            [
                {
                    "timestamp": "2026-10-13 22:00:05",
                    "market": "US",
                    "ticker": "AAPL",
                    "side": "BUY",
                    "qty": 3,
                    "price": 190.5,
                },
                {
                    "timestamp": "2026-10-16 05:00:00",
                    "market": "US",
                    "ticker": "AAPL",
                    "side": "SELL",
                    "qty": 3,
                    "price": 200,
                    "profit_rate": 4.5,
                },
                {
                    "timestamp": "2026-10-20 15:10:00",
                    "market": "KR",
                    "ticker": "005930",
                    "side": "SELL",
                    "qty": 10,
                    "price": 72000,
                    "profit_rate": 5.88,
                },
            ]
        ),
        encoding="utf-8",
    )
    text, rows = analyze_files(events, history)
    assert "실적 진입 차단" in text
    assert "국장 지수 급락 청산" in text
    assert "+4.50%" in text
    assert "72,000" in text or "72000" in text
    assert "70,000" in text or "70000" in text
    earn = [r for r in rows if r["rule"] == "earnings_block"][0]
    assert earn["profit_rate"] == pytest.approx(4.5)
    crash = [r for r in rows if r["rule"] == "kr_crash_exit"][0]
    assert crash["actual_exit_price"] == pytest.approx(72000)
    assert crash["price_move_pct"] == pytest.approx((72000 - 70000) / 70000 * 100)

    csv_path = tmp_path / "out.csv"
    write_csv(csv_path, rows)
    body = csv_path.read_text(encoding="utf-8-sig")
    assert "profit_rate" in body and "AAPL" in body


def test_analyze_script_stdout(tmp_path):
    events = tmp_path / "shadow_events.jsonl"
    history = tmp_path / "trade_history.json"
    events.write_text("", encoding="utf-8")
    history.write_text("[]", encoding="utf-8")
    import subprocess
    import sys
    from pathlib import Path

    proc = subprocess.run(
        [
            sys.executable,
            "tools/analyze_shadow.py",
            "--events",
            str(events),
            "--history",
            str(history),
            "--csv",
            str(tmp_path / "r.csv"),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "섀도우 규칙 복기" in proc.stdout
    assert "기록 없음" in proc.stdout
    assert (tmp_path / "r.csv").is_file()


def test_normalize_mode_defaults_shadow():
    assert normalize_mode(None) == "shadow"
    assert normalize_mode(" LIVE ") == "live"
    assert normalize_mode("nope") == "shadow"
