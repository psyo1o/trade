"""KIS 강제 새로고침(force_kis_labels) 시 비장중 US 라벨도 실조회하는지."""
from __future__ import annotations

from services.account_snapshot import (
    _maybe_reject_off_hours_force_label_anomaly,
    build_account_snapshot_for_report,
)


def _minimal_deps(*, us_cash: float = 123.45, us_total_current: float = 500.0):
    calls: dict[str, int] = {"us_cash": 0, "us_positions": 0, "kr_balance": 0}

    def _get_us_cash(_broker):
        calls["us_cash"] += 1
        return us_cash

    def _us_positions():
        calls["us_positions"] += 1
        return {"output1": [{"pdno": "AAPL"}], "output2": []}

    def _kr_balance():
        calls["kr_balance"] += 1
        return {"output2": [{"dnca_tot_amt": "1000", "tot_evlu_amt": "2000"}]}

    deps = {
        "get_real_weather": lambda _kr, _us: "sunny",
        "broker_kr": object(),
        "broker_us": object(),
        "load_last_kis_display_snapshot": lambda: {
            "kr": {"cash": 1, "total": 2},
            "us": {"cash": 9.0, "total": 10.0},
        },
        "save_last_kis_display_snapshot": lambda *a, **k: None,
        "load_last_coin_display_snapshot": lambda: {},
        "save_last_coin_display_snapshot": lambda *a, **k: None,
        "is_weekend_suppress": lambda: False,
        "get_balance_with_retry": _kr_balance,
        "get_us_positions_with_retry": _us_positions,
        "get_us_cash_real": _get_us_cash,
        "to_float": lambda v, d=0.0: float(v) if v is not None else float(d),
        "safe_num": lambda v, d=0.0: float(v) if v is not None else float(d),
        "calc_kr_holdings_metrics": lambda _b: {"current": 0.0, "roi": None},
        "calc_us_holdings_metrics": lambda _b: {"current": us_total_current, "roi": 1.5},
        "calc_coin_holdings_metrics": lambda _b: {"current": 0.0, "roi": None},
        "upbit_get_balance": lambda _q: 0,
        "upbit_get_balances": lambda: [],
        "get_kr_holdings_with_roi": lambda: [],
        "get_us_holdings_with_roi": lambda: [],
        "get_coin_holdings_with_roi": lambda: [],
        "is_market_open": lambda m: m == "KR",
    }
    return deps, calls


def test_force_kis_labels_fetches_us_when_market_closed():
    deps, calls = _minimal_deps(us_cash=200.0, us_total_current=300.0)
    out = build_account_snapshot_for_report(
        deps=deps,
        allow_kis_fetch=lambda _m: True,
        force_kis_labels=True,
    )
    assert calls["us_cash"] == 1
    assert calls["us_positions"] == 1
    us = out["labels"]["us"]
    assert us["cash"] == 200.0
    assert us["total"] == 500.0


def test_off_hours_force_keeps_prev_when_us_decreases():
    deps, calls = _minimal_deps(us_cash=50.0, us_total_current=100.0)
    deps["load_last_kis_display_snapshot"] = lambda: {
        "kr": {"cash": 1, "total": 2},
        "us": {"cash": 200.0, "total": 1000.0, "roi": 2.0},
    }
    out = build_account_snapshot_for_report(
        deps=deps,
        allow_kis_fetch=lambda _m: True,
        force_kis_labels=True,
    )
    assert calls["us_cash"] == 1
    us = out["labels"]["us"]
    assert us["cash"] == 200.0
    assert us["total"] == 1000.0
    assert us["roi"] == 2.0


def test_off_hours_force_allows_us_increase():
    deps, _calls = _minimal_deps(us_cash=300.0, us_total_current=700.0)
    deps["load_last_kis_display_snapshot"] = lambda: {
        "kr": {"cash": 1, "total": 2},
        "us": {"cash": 50.0, "total": 100.0},
    }
    out = build_account_snapshot_for_report(
        deps=deps,
        allow_kis_fetch=lambda _m: True,
        force_kis_labels=True,
    )
    us = out["labels"]["us"]
    assert us["cash"] == 300.0
    assert us["total"] == 1000.0


def test_off_hours_force_allows_cash_drop_when_total_stable():
    """매수 직후 예수↓·총평 유지/소폭↑ — 비장중에도 라벨 반영."""
    cash, total, roi = _maybe_reject_off_hours_force_label_anomaly(
        market="US",
        force_kis_labels=True,
        is_market_open_now=False,
        prev_part={"cash": 1128.07, "total": 1474.37, "roi": 1.0},
        new_cash=34.42,
        new_total=1480.41,
        new_roi=1.1,
        safe_num=lambda v, d=0.0: float(v) if v is not None else float(d),
    )
    assert cash == 34.42
    assert total == 1480.41
    assert roi == 1.1


def test_off_hours_force_rejects_us_spike():
    prev = {"cash": 100.0, "total": 1000.0, "roi": 1.0}
    cash, total, roi = _maybe_reject_off_hours_force_label_anomaly(
        market="US",
        force_kis_labels=True,
        is_market_open_now=False,
        prev_part=prev,
        new_cash=200.0,
        new_total=2500.0,
        new_roi=2.0,
        safe_num=lambda v, d=0.0: float(v) if v is not None else float(d),
    )
    assert cash == 100.0
    assert total == 1000.0
    assert roi == 1.0


def test_force_kis_prompt_accepts_us_spike():
    """GUI 강제 새로고침 — 사용자가 새 값 적용을 선택하면 급증도 반영."""
    deps, calls = _minimal_deps(us_cash=895.08, us_total_current=539.45)
    deps["load_last_kis_display_snapshot"] = lambda: {
        "kr": {"cash": 1, "total": 2},
        "us": {"cash": 37.91, "total": 576.69, "roi": 1.0},
    }
    deps["is_market_open"] = lambda _m: False

    def _accept(**_kw):
        return True

    out = build_account_snapshot_for_report(
        deps=deps,
        allow_kis_fetch=lambda _m: True,
        force_kis_labels=True,
        kis_label_anomaly_prompt=_accept,
    )
    us = out["labels"]["us"]
    assert us["cash"] == 895.08
    assert abs(us["total"] - 1434.53) < 0.02


def test_force_kis_prompt_rejects_us_spike():
    deps, _calls = _minimal_deps(us_cash=895.08, us_total_current=539.45)
    deps["load_last_kis_display_snapshot"] = lambda: {
        "kr": {"cash": 1, "total": 2},
        "us": {"cash": 37.91, "total": 576.69, "roi": 2.0},
    }
    deps["is_market_open"] = lambda _m: False

    def _reject(**_kw):
        return False

    out = build_account_snapshot_for_report(
        deps=deps,
        allow_kis_fetch=lambda _m: True,
        force_kis_labels=True,
        kis_label_anomaly_prompt=_reject,
    )
    us = out["labels"]["us"]
    assert us["cash"] == 37.91
    assert us["total"] == 576.69
    assert us["roi"] == 2.0


def test_trust_live_labels_allows_off_hours_spike():
    prev = {"cash": 100.0, "total": 1000.0}
    cash, total, _roi = _maybe_reject_off_hours_force_label_anomaly(
        market="US",
        force_kis_labels=True,
        is_market_open_now=False,
        prev_part=prev,
        new_cash=500.0,
        new_total=2500.0,
        new_roi=None,
        safe_num=lambda v, d=0.0: float(v) if v is not None else float(d),
        trust_live_labels=True,
    )
    assert cash == 500.0
    assert total == 2500.0


def test_without_force_kis_skips_us_when_market_closed():
    deps, calls = _minimal_deps()
    out = build_account_snapshot_for_report(
        deps=deps,
        allow_kis_fetch=lambda _m: True,
        force_kis_labels=False,
    )
    assert calls["us_cash"] == 0
    assert calls["us_positions"] == 0
    us = out["labels"]["us"]
    assert us["cash"] == 9.0
    assert us["total"] == 10.0
