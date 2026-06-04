"""
account_snapshot — heartbeat·GUI가 공유하는 **계좌 라벨·보유 스냅샷** 조립.

* ``build_account_snapshot_for_report``: 국·미 현금/총액/ROI 라벨, 코인 **KRW 환산** 합산(바이낸스 포함),
  그리고 각 시장 ``holdings``/raw ``balances`` 를 한 번에 채운다. 코인 라벨은 성공 시 ``last_coin_display_snapshot`` 에 저장하고(업비트·바이낸스 동일), 실패 시 폴백·``display_fallback``.
  바이낸스 상단 USDT: 폴백이면 라벨만 환산, 아니면 ``binance_display_cash_and_total_usdt()`` .
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
            try:
                from api import coin_broker

                cb = coin_broker.get_current_price(str(ticker))
                if cb and float(cb) > 0:
                    cp = float(cb)
                else:
                    cp = float(pyupbit.get_current_price(str(ticker)) or bp)
            except Exception:
                cp = float(pyupbit.get_current_price(str(ticker)) or bp)
    except Exception:
        cp = float(bp)
    return float(cp if cp > 0 else bp)


# 비장중 강제 새로고침: KIS 응답 누락·이중 합산 시 총평이 직전과 크게 어긋날 수 있음
# (예수는 총평에 포함되므로 방어 기준은 총평만 — 매수 직후 예수↓·총평 유지 오탐 방지)
_OFF_HOURS_FORCE_MIN_PREV: dict[str, float] = {"KR": 10_000.0, "US": 10.0}
_OFF_HOURS_FORCE_TOL: dict[str, float] = {"KR": 1.0, "US": 0.01}
# 직전 대비 허용 초과 비율(초과 시 급변으로 보고 직전 라벨 유지) — US 장중 급락 45%와 대칭
_OFF_HOURS_FORCE_MAX_TOTAL_SPIKE = 1.12


def _maybe_reject_off_hours_force_label_anomaly(
    *,
    market: str,
    force_kis_labels: bool,
    is_market_open_now: bool,
    prev_part: dict | None,
    new_cash: float,
    new_total: float,
    new_roi,
    safe_num: Callable[[Any, float], float],
    trust_live_labels: bool = False,
) -> tuple[float, float, Any]:
    """비장중 ``force_kis_labels`` 조회 결과가 직전 **총평** 대비 비정상이면 덮어쓰지 않는다.

    예수 단독 변동(매수 직후 등)은 총평이 안정이면 허용한다.
    ``trust_live_labels`` — 입출금(고점 보정) 직후 1회 등 의도적 실조회는 방어 생략.
    """
    if trust_live_labels or not force_kis_labels or is_market_open_now:
        return new_cash, new_total, new_roi

    part = prev_part if isinstance(prev_part, dict) else {}
    prev_cash = float(safe_num(part.get("cash", 0), 0.0))
    prev_total = float(safe_num(part.get("total", prev_cash), prev_cash))
    prev_roi = part.get("roi")

    m = str(market or "").strip().upper()
    min_prev = float(_OFF_HOURS_FORCE_MIN_PREV.get(m, 10.0))
    tol = float(_OFF_HOURS_FORCE_TOL.get(m, 0.01))
    max_total_spike = float(_OFF_HOURS_FORCE_MAX_TOTAL_SPIKE)

    if prev_total < min_prev:
        return new_cash, new_total, new_roi

    nt = float(new_total)
    nc = float(new_cash)
    total_dropped = prev_total >= min_prev and nt < prev_total - tol
    total_spiked = prev_total >= min_prev and nt > prev_total * max_total_spike + tol
    if not (total_dropped or total_spiked):
        return new_cash, new_total, new_roi

    reason = (
        "총평 직전보다 급증(이중 합산·API 이상 추정)"
        if total_spiked
        else "총평 직전보다 감소(누락 가능)"
    )
    print(
        f"  ⚠️ [KIS 강제 새로고침·{m}] 비장중 — {reason} — "
        f"직전 라벨 유지 (new cash={nc}, total={nt} / "
        f"prev cash={prev_cash}, total={prev_total})"
    )
    return prev_cash, prev_total, prev_roi


# 하위 호환(테스트·import)
_maybe_reject_off_hours_force_label_decrease = _maybe_reject_off_hours_force_label_anomaly


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
        2. ``allow_kis_fetch`` 가 참이고 (장중 또는 ``force_kis_labels``) 인 시장만 KIS 재조회.
           강제 새로고침은 비장중에도 실조회하되, **직전 라벨 대비 총평 급감·급증** 만 덮어쓰지 않는다(입출금 직후 1회 제외).
        3. 코인은 항상 업비트 잔고로 라벨·metrics 계산.

    ``deps`` 키는 ``run_bot`` / ``run_gui`` 가 주입하는 콜백·상태 로더 집합이다.
    """
    if allow_kis_fetch is None:
        allow_kis_fetch = lambda _m: True
    if with_backoff is None:
        with_backoff = lambda fn, _label: fn()

    trust_live_labels = bool(deps.get("trust_off_hours_live_labels"))

    weather = deps["get_real_weather"](deps["broker_kr"], deps["broker_us"])
    snap = deps["load_last_kis_display_snapshot"]()
    is_market_open_fn = deps.get("is_market_open")

    def _is_open(market: str) -> bool:
        try:
            if callable(is_market_open_fn):
                return bool(is_market_open_fn(market))
        except Exception:
            pass
        return True

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
            print("  📌 [KIS 강제 새로고침] 주말·점검 창 — 억제 무시하고 KIS 국·미 라벨 재조회")
        elif force_kis_labels:
            print("  🔁 [KIS 강제 새로고침] 국·미 예수·총평 라벨 KIS 조회 시작")

        kr_part = snap.get("kr") or {}
        us_part = snap.get("us") or {}

        if allow_kis_fetch("KR") and (_is_open("KR") or force_kis_labels):
            try:
                kr_bal = with_backoff(deps["get_balance_with_retry"], "KR 잔고") or {}
                out2 = kr_bal.get("output2", []) if isinstance(kr_bal, dict) else []
                kr_cash, _ = parse_kr_cash_total(out2, deps["to_float"])
                kr_m = deps["calc_kr_holdings_metrics"](kr_bal)
                kr_total = int(kr_cash + float(kr_m.get("current", 0.0)))
                kr_roi = kr_m.get("roi")
                kr_cash_f, kr_total_f, kr_roi = _maybe_reject_off_hours_force_label_anomaly(
                    market="KR",
                    force_kis_labels=force_kis_labels,
                    is_market_open_now=_is_open("KR"),
                    prev_part=kr_part,
                    new_cash=float(kr_cash),
                    new_total=float(kr_total),
                    new_roi=kr_roi,
                    safe_num=deps["safe_num"],
                    trust_live_labels=trust_live_labels,
                )
                kr_cash = int(kr_cash_f)
                kr_total = int(kr_total_f)
            except Exception as e:
                print(
                    f"  ⚠️ [snapshot KR] 라벨용 잔고 조회 실패 — 직전 스냅샷으로 폴백: {type(e).__name__}: {e}"
                )
                if isinstance(kr_part, dict):
                    kr_cash = int(deps["safe_num"](kr_part.get("cash", 0), 0.0))
                    kr_total = int(deps["safe_num"](kr_part.get("total", 0), 0.0))
                    kr_roi = kr_part.get("roi")
        else:
            if not allow_kis_fetch("KR"):
                print("  📌 [snapshot KR] allow_kis_fetch=False — KIS 재조회 생략, 직전 스냅샷 라벨 사용")
            elif not _is_open("KR") and not force_kis_labels:
                print("  📌 [snapshot KR] 비장중 — KIS 재조회 생략, 직전 스냅샷 라벨 사용")
            else:
                print("  📌 [snapshot KR] KIS 재조회 생략, 직전 스냅샷 라벨 사용")
            if isinstance(kr_part, dict):
                kr_cash = int(deps["safe_num"](kr_part.get("cash", 0), 0.0))
                kr_total = int(deps["safe_num"](kr_part.get("total", 0), 0.0))
                kr_roi = kr_part.get("roi")

        if allow_kis_fetch("US") and (_is_open("US") or force_kis_labels):
            try:
                us_cash = deps["safe_num"](deps["get_us_cash_real"](deps["broker_us"]), 0.0)
                us_bal = with_backoff(deps["get_us_positions_with_retry"], "US 잔고") or {}
                us_m = deps["calc_us_holdings_metrics"](us_bal)
                if us_cash <= 0 and isinstance(us_bal, dict):
                    out2 = us_bal.get("output2", [])
                    us_cash = deps["safe_num"](parse_us_cash_fallback(out2, deps["to_float"]), 0.0)
                us_total = float(us_cash + float(us_m.get("current", 0.0) or 0.0))
                us_roi = us_m.get("roi")

                # KIS 해외 예수금/총평이 간헐적으로 튀는 구간 방어(표시·텔레그램 안정화)
                prev_us_total = float(deps["safe_num"]((us_part or {}).get("total", 0.0), 0.0))
                prev_us_cash = float(deps["safe_num"]((us_part or {}).get("cash", 0.0), 0.0))
                us_rows = us_bal.get("output1", []) if isinstance(us_bal, dict) else []
                has_us_rows = isinstance(us_rows, list) and len(us_rows) > 0
                suspicious_zero = us_total <= 0.0 and (prev_us_total > 0.0 or has_us_rows)
                suspicious_drop = (
                    prev_us_total > 0.0 and has_us_rows and us_total > 0.0 and us_total < prev_us_total * 0.45
                )
                suspicious_cash_glitch = prev_us_cash > 50.0 and us_cash <= 0.0 and has_us_rows
                suspicious_spike = (
                    not trust_live_labels
                    and prev_us_total > 0.0
                    and us_total > prev_us_total * _OFF_HOURS_FORCE_MAX_TOTAL_SPIKE
                )
                if suspicious_zero or suspicious_drop or suspicious_cash_glitch or suspicious_spike:
                    print(
                        "  ⚠️ [snapshot US] 미장 라벨 값 급변 감지(일시 API 이상 추정) — "
                        f"직전 스냅샷 유지 (new cash=${us_cash:.2f}, total=${us_total:.2f} / "
                        f"prev cash=${prev_us_cash:.2f}, total=${prev_us_total:.2f})"
                    )
                    if isinstance(us_part, dict):
                        us_cash = float(deps["safe_num"](us_part.get("cash", 0.0), 0.0))
                        us_total = float(deps["safe_num"](us_part.get("total", us_cash), us_cash))
                        us_roi = us_part.get("roi")
                else:
                    us_cash, us_total, us_roi = _maybe_reject_off_hours_force_label_anomaly(
                        market="US",
                        force_kis_labels=force_kis_labels,
                        is_market_open_now=_is_open("US"),
                        prev_part=us_part,
                        new_cash=float(us_cash),
                        new_total=float(us_total),
                        new_roi=us_roi,
                        safe_num=deps["safe_num"],
                        trust_live_labels=trust_live_labels,
                    )
            except Exception as e:
                print(
                    f"  ⚠️ [snapshot US] 라벨용 잔고 조회 실패 — 직전 스냅샷으로 폴백: {type(e).__name__}: {e}"
                )
                if isinstance(us_part, dict):
                    us_cash = float(deps["safe_num"](us_part.get("cash", 0.0), 0.0))
                    us_total = float(deps["safe_num"](us_part.get("total", us_cash), us_cash))
                    us_roi = us_part.get("roi")
        else:
            if not allow_kis_fetch("US"):
                print("  📌 [snapshot US] allow_kis_fetch=False — KIS 재조회 생략, 직전 스냅샷 라벨 사용")
            elif not _is_open("US") and not force_kis_labels:
                print("  📌 [snapshot US] 비장중 — KIS 재조회 생략, 직전 스냅샷 라벨 사용")
            else:
                print("  📌 [snapshot US] KIS 재조회 생략, 직전 스냅샷 라벨 사용")
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
            if force_kis_labels:
                print(
                    "  ✅ [KIS 강제 새로고침] last_kis_display_snapshot 저장 — "
                    f"KR 예수 {int(kr_cash):,}원 · 총평 {int(kr_total):,}원 / "
                    f"US 예수 ${float(us_cash):,.2f} · 총평 ${float(us_total):,.2f}"
                )
        except Exception as e:
            print(f"  ⚠️ [snapshot] last_kis_display_snapshot 저장 실패(라벨은 메모리 값 유지): {type(e).__name__}: {e}")

    _load_coin = deps.get("load_last_coin_display_snapshot")
    _save_coin = deps.get("save_last_coin_display_snapshot")
    coin_prev = _load_coin() if callable(_load_coin) else {}

    krw_bal = 0
    coin_total = 0
    coin_roi = None
    upbit_bals: list = []
    coin_label_from_fallback = False
    try:
        krw_bal = int(deps["safe_num"](deps["upbit_get_balance"]("KRW"), 0.0))
        upbit_bals = deps["upbit_get_balances"]() or []
        coin_m = deps["calc_coin_holdings_metrics"](upbit_bals)
        coin_total = int(krw_bal + float(coin_m.get("current", 0.0) or 0.0))
        coin_roi = coin_m.get("roi")
        if callable(_save_coin):
            try:
                _save_coin(int(krw_bal), int(coin_total), coin_roi)
            except Exception as se:
                print(
                    f"  ⚠️ [snapshot COIN] last_coin_display_snapshot 저장 실패: "
                    f"{type(se).__name__}: {se}"
                )
    except Exception as e:
        coin_label_from_fallback = True
        print(
            f"  ⚠️ [snapshot COIN] 라벨용 잔고 조회 실패 — 직전 스냅샷으로 폴백: "
            f"{type(e).__name__}: {e}"
        )
        fb = coin_prev if isinstance(coin_prev, dict) else {}
        krw_bal = int(deps["safe_num"](fb.get("cash", 0), 0.0))
        coin_total = int(deps["safe_num"](fb.get("total", 0), 0.0))
        coin_roi = fb.get("roi")
        if isinstance(fb.get("saved_at"), str) and fb["saved_at"].strip():
            print(f"     (직전 코인 스냅샷 시각: {fb['saved_at'].strip()})")

    coin_label: dict = {
        "cash": int(krw_bal),
        "total": int(coin_total),
        "roi": coin_roi,
    }
    # 바이낸스 GUI·텔레: 라이브 USDT 조회가 (0,0)으로 나와도 직전 라벨을 덮어쓰지 않도록 표시
    if coin_label_from_fallback:
        coin_label["display_fallback"] = True

    return {
        "weather": weather,
        "labels": {
            "kr": {"cash": int(kr_cash), "total": int(kr_total), "roi": kr_roi},
            "us": {"cash": float(us_cash), "total": float(us_total), "roi": us_roi},
            "coin": coin_label,
        },
        "holdings": {
            "kr": deps["get_kr_holdings_with_roi"](),
            "us": deps["get_us_holdings_with_roi"](),
            "coin": deps["get_coin_holdings_with_roi"](),
        },
        "balances": {"kr": kr_bal, "us": us_bal, "coin": upbit_bals},
        "snapshot_saved_at": str((snap.get("saved_at") or "")).strip(),
    }

