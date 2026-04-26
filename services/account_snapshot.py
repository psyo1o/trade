"""
account_snapshot — heartbeat·GUI가 공유하는 **계좌 라벨·보유 스냅샷** 조립.

* ``build_account_snapshot_for_report``: 국·미 현금/총액/ROI 라벨, 코인 KRW 합산,
  그리고 각 시장 ``holdings``/raw ``balances`` 를 한 번에 채운다.
* ``resolve_display_current_price``: API 현재가가 없을 때 yfinance·업비트 등으로 보조.

로그 정책 (2026-04-22)
    KIS 조회 생략·예외·스냅샷 저장 실패는 **빈 값으로 넘어가지 않도록** 한 줄 남기고,
    가능하면 ``load_last_kis_display_snapshot`` 의 직전 값으로 라벨을 채운다.
"""
from __future__ import annotations

from typing import Any, Callable

import pyupbit
import yfinance as yf
from api.kis_parsers import parse_kr_cash_total, parse_us_cash_fallback


def resolve_display_current_price(
    market: str,
    ticker: str,
    buy_p: float,
    current_p_api=None,
    *,
    to_float: Callable[[Any, float], float],
    get_ohlcv_yfinance: Callable[[str], list | None],
) -> float:
    """티커별 표시용 현재가.

    우선순위: ``current_p_api``(브로커/스냅샷) → 시장별 외부 소스 → 실패 시 ``buy_p``.
    예외는 삼키지 않고 최종적으로 ``buy_p`` 로 수렴(호출부에서 과도한 로그를 피하기 위해
    여기서는 출력하지 않음; 필요 시 호출부에서 ticker 단위로 로깅).
    """
    bp = float(to_float(buy_p, 0.0))
    cp = float(bp)
    try:
        if market == "KR":
            if current_p_api is not None and float(to_float(current_p_api, 0.0)) > 0:
                cp = float(to_float(current_p_api, bp))
            else:
                oc = get_ohlcv_yfinance(str(ticker))
                if oc and len(oc) > 0:
                    cp = float(oc[-1]["c"])
                else:
                    cp = float(bp)
        elif market == "US":
            if current_p_api is not None and float(to_float(current_p_api, 0.0)) > 0:
                cp = float(to_float(current_p_api, bp))
            else:
                try:
                    t_info = yf.Ticker(str(ticker))
                    cp0 = t_info.info.get("currentPrice")
                    if cp0:
                        cp = float(cp0)
                    else:
                        oc = get_ohlcv_yfinance(str(ticker))
                        if oc and len(oc) > 0:
                            cp = float(oc[-1]["c"])
                except Exception:
                    oc = get_ohlcv_yfinance(str(ticker))
                    if oc and len(oc) > 0:
                        cp = float(oc[-1]["c"])
        elif market == "COIN":
            cp = float(pyupbit.get_current_price(str(ticker)) or bp)
    except Exception:
        cp = float(bp)
    return float(cp if cp > 0 else bp)


def build_account_snapshot_for_report(
    *,
    deps: dict,
    allow_kis_fetch: Callable[[str], bool] | None = None,
    with_backoff: Callable[[Callable[[], Any], str], Any] | None = None,
    force_kis_labels: bool = False,
) -> dict:
    """heartbeat / GUI 상단 라벨·보유 테이블 입력용 스냅샷.

    흐름
        1. ``is_weekend_suppress`` 이면 API 없이 ``last_kis_display_snapshot`` 만 사용.
           단, ``force_kis_labels=True`` (GUI KIS 강제 새로고침) 이면 억제를 무시하고 KIS 재조회.
        2. 평일이면 ``allow_kis_fetch`` 가 참인 시장만 KIS/브로커 재조회 후 스냅샷 갱신 시도.
        3. 코인은 항상 업비트 잔고로 라벨·metrics 계산.

    ``deps`` 키는 ``run_bot`` / ``run_gui`` 가 주입하는 콜백·상태 로더 집합이다.
    """
    if allow_kis_fetch is None:
        allow_kis_fetch = lambda _m: True
    if with_backoff is None:
        with_backoff = lambda fn, _label: fn()

    weather = deps["get_real_weather"](deps["broker_kr"], deps["broker_us"])
    snap = deps["load_last_kis_display_snapshot"]()

    kr_bal: dict = {}
    us_bal: dict = {}
    kr_cash = 0
    kr_total = 0
    kr_roi = None
    us_cash = 0.0
    us_total = 0.0
    us_roi = None

    if deps["is_weekend_suppress"]() and not force_kis_labels:
        kr_part = snap.get("kr") or {}
        us_part = snap.get("us") or {}
        print("  📌 [snapshot] 주말·점검 억제 — 국·미 라벨은 last_kis_display_snapshot 만 사용")
        if isinstance(kr_part, dict) and "total" in kr_part:
            kr_cash = int(kr_part.get("cash", 0))
            kr_total = int(kr_part["total"])
            kr_roi = kr_part.get("roi")
        else:
            print("  ⚠️ [snapshot KR] 주말 스냅샷에 total 없음 — 국장 라벨 0·ROI 없음")
        if isinstance(us_part, dict) and "total" in us_part:
            us_cash = float(us_part.get("cash", 0))
            us_total = float(us_part["total"])
            us_roi = us_part.get("roi")
        else:
            print("  ⚠️ [snapshot US] 주말 스냅샷에 total 없음 — 미장 라벨 0·ROI 없음")
    else:
        if deps["is_weekend_suppress"]() and force_kis_labels:
            print("  📌 [snapshot] force_kis_labels — 주말·점검 창에서도 KIS 국·미 라벨 재조회 후 스냅샷 저장 시도")

        kr_part = snap.get("kr") or {}
        us_part = snap.get("us") or {}

        if allow_kis_fetch("KR"):
            try:
                kr_bal = with_backoff(deps["get_balance_with_retry"], "KR 잔고") or {}
                out2 = kr_bal.get("output2", []) if isinstance(kr_bal, dict) else []
                kr_cash, _ = parse_kr_cash_total(out2, deps["to_float"])
                kr_m = deps["calc_kr_holdings_metrics"](kr_bal)
                kr_total = int(kr_cash + float(kr_m.get("current", 0.0)))
                kr_roi = kr_m.get("roi")
            except Exception as e:
                print(
                    f"  ⚠️ [snapshot KR] 라벨용 잔고 조회 실패 — 직전 스냅샷으로 폴백: {type(e).__name__}: {e}"
                )
                if isinstance(kr_part, dict):
                    kr_cash = int(deps["safe_num"](kr_part.get("cash", 0), 0.0))
                    kr_total = int(deps["safe_num"](kr_part.get("total", 0), 0.0))
                    kr_roi = kr_part.get("roi")
        else:
            print("  📌 [snapshot KR] allow_kis_fetch=False — KIS 재조회 생략, 직전 스냅샷 라벨 사용")
            if isinstance(kr_part, dict):
                kr_cash = int(deps["safe_num"](kr_part.get("cash", 0), 0.0))
                kr_total = int(deps["safe_num"](kr_part.get("total", 0), 0.0))
                kr_roi = kr_part.get("roi")

        if allow_kis_fetch("US"):
            try:
                us_cash = deps["safe_num"](deps["get_us_cash_real"](deps["broker_us"]), 0.0)
                us_bal = with_backoff(deps["get_us_positions_with_retry"], "US 잔고") or {}
                us_m = deps["calc_us_holdings_metrics"](us_bal)
                if us_cash <= 0 and isinstance(us_bal, dict):
                    out2 = us_bal.get("output2", [])
                    us_cash = deps["safe_num"](parse_us_cash_fallback(out2, deps["to_float"]), 0.0)
                us_total = float(us_cash + float(us_m.get("current", 0.0) or 0.0))
                us_roi = us_m.get("roi")
            except Exception as e:
                print(
                    f"  ⚠️ [snapshot US] 라벨용 잔고 조회 실패 — 직전 스냅샷으로 폴백: {type(e).__name__}: {e}"
                )
                if isinstance(us_part, dict):
                    us_cash = float(deps["safe_num"](us_part.get("cash", 0.0), 0.0))
                    us_total = float(deps["safe_num"](us_part.get("total", us_cash), us_cash))
                    us_roi = us_part.get("roi")
        else:
            print("  📌 [snapshot US] allow_kis_fetch=False — KIS 재조회 생략, 직전 스냅샷 라벨 사용")
            if isinstance(us_part, dict):
                us_cash = float(deps["safe_num"](us_part.get("cash", 0.0), 0.0))
                us_total = float(deps["safe_num"](us_part.get("total", us_cash), us_cash))
                us_roi = us_part.get("roi")

        try:
            deps["save_last_kis_display_snapshot"](
                int(kr_cash),
                int(kr_total),
                kr_roi,
                float(us_cash),
                float(us_total),
                us_roi,
                force=force_kis_labels,
            )
        except Exception as e:
            print(f"  ⚠️ [snapshot] last_kis_display_snapshot 저장 실패(라벨은 메모리 값 유지): {type(e).__name__}: {e}")

    krw_bal = int(deps["safe_num"](deps["upbit_get_balance"]("KRW"), 0.0))
    upbit_bals = deps["upbit_get_balances"]() or []
    coin_m = deps["calc_coin_holdings_metrics"](upbit_bals)
    coin_total = int(krw_bal + float(coin_m.get("current", 0.0) or 0.0))
    coin_roi = coin_m.get("roi")

    return {
        "weather": weather,
        "labels": {
            "kr": {"cash": int(kr_cash), "total": int(kr_total), "roi": kr_roi},
            "us": {"cash": float(us_cash), "total": float(us_total), "roi": us_roi},
            "coin": {"cash": int(krw_bal), "total": int(coin_total), "roi": coin_roi},
        },
        "holdings": {
            "kr": deps["get_kr_holdings_with_roi"](),
            "us": deps["get_us_holdings_with_roi"](),
            "coin": deps["get_coin_holdings_with_roi"](),
        },
        "balances": {"kr": kr_bal, "us": us_bal, "coin": upbit_bals},
        "snapshot_saved_at": str((snap.get("saved_at") or "")).strip(),
    }

