# -*- coding: utf-8 -*-
"""
수동 입·출금 반영 → Phase 5 합산 MDD용 고점(`peak_total_equity`) 보정.

메인 봇은 `execution.guard.apply_phase5_trailing_week_and_cooldown` 등으로 주차 고점을 관리하며,
현금만 입출금하면 평가금이 변해 고점 대비 드로다운이 왜곡될 수 있다.
이 스크립트는 **기록된 합산 고점**에 입금액을 더하거나 출금액을 빼서
다음 루프부터 동일 기준으로 MDD(-15% 등)가 계산되도록 한다.

입력 단위는 시장 고점과 같다: 국장 **원**, 미장 **USD**, 코인 **USDT**(바이낸스)/원(업비트).
합산 고점(원화)에는 환율로 환산한 금액을 가감한다.

시작 시 ``run_bot.refresh_circuit_aux_from_brokers`` 로 ``circuit_aux_*`` 잔고 스냅샷을
먼저 갱신해, 출금 한도·합산 총액이 실계좌와 맞도록 한다 (직후 봇 기동 시 Phase 5 오발동 완화).

사용: 프로젝트 루트에서 `py -3.11 adjust_capital.py` (`config.json` 필요)
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.circuit_break import estimate_usdkrw  # noqa: E402
from execution.guard import (  # noqa: E402
    PEAK_TOTAL_EQUITY_KEY,
    adjust_peak_equity_for_capital,
    apply_phase5_share_anchor,
    get_phase5_peak_market_equity,
    load_state,
    save_state,
)

STATE_PATH = ROOT / "bot_state.json"
CAPITAL_ADJUSTMENTS_KEY = "capital_adjustments"


def portfolio_total_krw_estimated(state: dict) -> float:
    """봇이 마지막으로 저장한 스냅샷으로 합산 평가(원화) 추정."""
    from services.ledger_valuation import kis_display_total

    rate = estimate_usdkrw()
    kr = kis_display_total(state, "KR")
    coin = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    usd = kis_display_total(state, "US")
    return kr + coin + usd * rate


def _parse_amount_krw(raw: str) -> float:
    s = raw.replace(",", "").strip()
    if not s:
        raise ValueError("금액이 비었습니다.")
    v = float(s)
    if v <= 0:
        raise ValueError("금액은 0보다 커야 합니다.")
    return v


# GUI 등에서 재사용 — 단위는 ``capital_amount_unit`` 기준(원·USD·USDT)
parse_capital_amount = _parse_amount_krw
parse_capital_amount_krw = _parse_amount_krw

_MARKET_LABELS = {"KR": "국장", "US": "미장", "COIN": "코인", "ALL": "합산"}
_UNIT_LABELS = {"KRW": "원", "USD": "USD", "USDT": "USDT"}


def capital_amount_unit(market: str | None) -> str:
    """입·출금 입력 단위 — Phase5 시장 고점과 같은 통화 (국장 KRW, 미장 USD, 코인 USDT/KRW)."""
    mk = normalize_capital_market(market)
    if mk == "US":
        return "USD"
    if mk == "COIN":
        try:
            from api import coin_broker as _cb

            return "USDT" if _cb.coin_equity_quote_unit() == "USDT" else "KRW"
        except Exception:
            return "KRW"
    return "KRW"


def capital_unit_label(unit: str) -> str:
    return _UNIT_LABELS.get(str(unit or "KRW").upper(), str(unit))


def _fmt_amount(amount: float, unit: str) -> str:
    u = str(unit or "KRW").upper()
    if u == "KRW":
        return f"{amount:,.0f}원"
    return f"{amount:,.2f} {u}"


def _krw_per_unit(unit: str) -> float:
    """입력 단위 1당 원화 — 합산 고점(원화) 가감용."""
    u = str(unit or "KRW").upper()
    if u == "USD":
        return float(estimate_usdkrw() or 0)
    if u == "USDT":
        try:
            from api import coin_broker as _cb

            return float(_cb.get_krw_per_usdt() or 0) or float(estimate_usdkrw() or 0)
        except Exception:
            return float(estimate_usdkrw() or 0)
    return 1.0


def normalize_capital_market(raw: str | None) -> str:
    s = str(raw or "KR").strip().upper()
    aliases = {
        "KR": "KR",
        "국장": "KR",
        "US": "US",
        "미장": "US",
        "COIN": "COIN",
        "코인": "COIN",
        "ALL": "ALL",
        "합산": "ALL",
        "TOTAL": "ALL",
    }
    return aliases.get(s, "KR") if s in aliases else "KR"


def _refresh_aux_snapshot() -> None:
    """브로커·업비트 기준으로 circuit_aux_* 갱신 (실패 시 경고만)."""
    print("📡 실계좌 기준으로 합산 스냅샷(`circuit_aux_*`) 갱신 중…")
    try:
        import run_bot as rb

        st = load_state(STATE_PATH)
        from execution.balance_policy import mark_balance_live_sync

        mark_balance_live_sync(st, STATE_PATH)
        info = rb.refresh_circuit_aux_from_brokers(st, STATE_PATH)
        t = info.get("totals") or {}
        kr = float(t.get("kr_krw", 0) or 0)
        usd = float(t.get("usd_total", 0) or 0)
        ck = float(t.get("coin_krw", 0) or 0)
        rate = estimate_usdkrw()
        approx = kr + ck + usd * rate
        print(
            f"   국·코인(원): {kr:,.0f} + {ck:,.0f} | 미장(USD): ${usd:,.2f} "
            f"→ 원화환산 합계 ~{approx:,.0f}원"
        )
        if info.get("weekend_kis_skip"):
            print(
                "   ℹ️  KIS 주말 점검 창: 국·미는 저장 스냅샷(`last_kis_display_snapshot`), 코인만 실조회."
            )
        ok_kr = info.get("kr_ok")
        ok_us = info.get("us_ok")
        ok_c = info.get("coin_ok")
        if not ok_c:
            print("   ⚠️ 코인 스냅샷 갱신 실패 — 장부의 이전 값이 남았을 수 있습니다.")
        if not info.get("weekend_kis_skip") and not (ok_kr and ok_us):
            print("   ⚠️ 국·미 일부 조회 실패 가능 — 장부 값 확인을 권장합니다.")
    except Exception as e:
        print(f"⚠️ 스냅샷 갱신 실패 ({type(e).__name__}: {e}). 장부의 기존 circuit_aux 로 진행합니다.")
        print("   (프로젝트 루트에 config.json 이 있고 네트워크·API가 정상인지 확인하세요.)")


def apply_capital_peak_adjustment(
    *,
    withdraw: bool,
    amount: float | None = None,
    amount_krw: float | None = None,
    state_path: Path | None = None,
    source_label: str = "adjust_capital.py",
    market: str = "KR",
) -> tuple[bool, str]:
    """
    ``circuit_aux_*`` 갱신 후 고점 입·출금 보정 및 ``capital_adjustments`` 기록.

    ``market``: ``KR`` / ``US`` / ``COIN`` / ``ALL``.
    ``amount`` 는 **시장 기준 통화**(``capital_amount_unit``: 국장 원, 미장 USD, 바이낸스 USDT).
    ``amount_krw`` 는 레거시 원화 입력 — 미장·USDT 코인이면 환율로 나눠 반영한다.
    시장 ``peak_equity_*`` 는 입력 통화 그대로, 합산 고점은 원화 환산액으로 가감하고
    비중 앵커를 현재 스냅샷으로 다시 잡는다.

    Returns
        ``(True, 요약 메시지)`` 또는 ``(False, 오류 메시지)``.
    """
    path = state_path or STATE_PATH
    mk = normalize_capital_market(market)
    unit = capital_amount_unit(mk)
    raw_native = float(amount) if amount is not None else None
    raw_krw = float(amount_krw) if amount_krw is not None else None
    if (raw_native is None or raw_native <= 0) and (raw_krw is None or raw_krw <= 0):
        return False, "금액은 0보다 커야 합니다."

    _refresh_aux_snapshot()

    fx = _krw_per_unit(unit)
    if fx <= 0:
        return False, f"{unit} 환율을 가져오지 못했습니다. 잠시 후 다시 시도하세요."
    if raw_native is not None and raw_native > 0:
        native_amt = raw_native
        krw_amt = raw_native * fx
    else:
        krw_amt = float(raw_krw or 0)
        native_amt = krw_amt / fx

    state = load_state(path)
    current_total = portfolio_total_krw_estimated(state)
    old_peak = float(state.get(PEAK_TOTAL_EQUITY_KEY, 0.0) or 0.0)
    peak_was_missing = old_peak <= 0.0

    if old_peak <= 0.0:
        old_peak = current_total

    if withdraw:
        if krw_amt > current_total:
            return (
                False,
                f"출금액 {_fmt_amount(native_amt, unit)}(≈{krw_amt:,.0f}원)이 "
                f"현재 추정 총자산 {current_total:,.0f}원보다 큽니다.",
            )
        new_peak = old_peak - krw_amt
        kind = "withdraw"
        delta_native = -native_amt
    else:
        new_peak = old_peak + krw_amt
        kind = "deposit"
        delta_native = native_amt

    if new_peak < 0.0:
        return (
            False,
            f"보정 후 고점이 음수가 됩니다 ({new_peak:,.0f}). 출금액 또는 장부를 확인하세요.",
        )

    state[PEAK_TOTAL_EQUITY_KEY] = float(new_peak)
    state.pop("peak_equity_total_krw", None)

    from services import ledger_valuation as lv

    rate = estimate_usdkrw()
    unit_lbl = capital_unit_label(unit)
    market_peak_note = ""
    if mk in ("KR", "US", "COIN"):
        old_m = get_phase5_peak_market_equity(state, mk)
        if old_m <= 0.0:
            if mk == "KR":
                old_m = float(lv.kis_display_total(state, "KR") or 0)
            elif mk == "US":
                old_m = float(lv.kis_display_total(state, "US") or 0)
            else:
                from api import coin_broker as _cb2

                old_m = float(_cb2.circuit_aux_coin_native(state) or 0)
                if old_m <= 0:
                    old_m = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
            state[f"peak_equity_{mk}"] = float(old_m)
        new_m = adjust_peak_equity_for_capital(state, mk, delta_native)
        market_peak_note = (
            f"{_MARKET_LABELS.get(mk, mk)} 고점: {old_m:,.2f} {unit_lbl} → {new_m:,.2f} {unit_lbl}"
        )
        try:
            apply_phase5_share_anchor(
                state,
                kr_krw=float(lv.kis_display_total(state, "KR") or 0),
                us_krw=float(lv.kis_display_total(state, "US") or 0) * rate,
                coin_krw=float(state.get("circuit_aux_last_coin_krw", 0) or 0),
                path=path,
                market_ok={"KR": True, "US": True, "COIN": True},
            )
        except Exception:
            pass
    elif mk == "ALL":
        try:
            apply_phase5_share_anchor(
                state,
                kr_krw=float(lv.kis_display_total(state, "KR") or 0),
                us_krw=float(lv.kis_display_total(state, "US") or 0) * rate,
                coin_krw=float(state.get("circuit_aux_last_coin_krw", 0) or 0),
                path=path,
                market_ok={"KR": True, "US": True, "COIN": True},
            )
        except Exception:
            pass

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "market": mk,
        "amount_native": float(native_amt),
        "amount_unit": unit,
        "krw_per_unit": float(fx),
        "amount_krw": float(krw_amt),
        "peak_before_krw": float(old_peak),
        "peak_after_krw": float(new_peak),
        "estimated_total_krw_at_adjust": float(current_total),
        "circuit_aux_after_refresh": {
            "kr_krw": float(lv.kis_display_total(state, "KR")),
            "usd_total": float(lv.kis_display_total(state, "US")),
            "coin_krw": float(state.get("circuit_aux_last_coin_krw", 0) or 0),
        },
        "source": str(source_label),
    }
    log = state.setdefault(CAPITAL_ADJUSTMENTS_KEY, [])
    if isinstance(log, list):
        log.append(entry)
    else:
        state[CAPITAL_ADJUSTMENTS_KEY] = [entry]

    save_state(path, state)

    try:
        from execution.balance_policy import mark_capital_label_refresh, mark_balance_live_sync

        mark_balance_live_sync(state, path)
        mark_capital_label_refresh(state, path)
    except Exception:
        pass

    lines = []
    if peak_was_missing:
        lines.append(
            f"ℹ️ 장부에 합산 고점이 없거나 0이라, 추정 총자산 {current_total:,.0f}원을 고점으로 간주했습니다."
        )
    lines.append(f"대상 시장: {_MARKET_LABELS.get(mk, mk)} ({mk})")
    if unit == "KRW":
        lines.append(f"반영 금액: {_fmt_amount(native_amt, unit)}")
    else:
        lines.append(
            f"반영 금액: {_fmt_amount(native_amt, unit)} "
            f"(합산 고점용 ≈{krw_amt:,.0f}원, 1 {unit}={fx:,.2f}원)"
        )
    if market_peak_note:
        lines.append(market_peak_note)
    lines.extend(
        [
            f"이전 합산 고점: {old_peak:,.0f} 원 → 보정 후: {new_peak:,.0f} 원",
            f"(참고) 추정 현재 총자산: {current_total:,.0f} 원",
            f"저장: {path}",
        ]
    )
    return True, "\n".join(lines)


def main() -> int:
    print("=== Phase 5 고점 수동 보정 (시장별 입·출금) ===\n")

    print("조작 종류를 선택하세요.")
    print("  1 — 입금 (고점에 금액만큼 가산)")
    print("  2 — 출금 (고점에서 금액만큼 감산)\n")

    choice = input("선택 (1 또는 2): ").strip()
    if choice not in ("1", "2"):
        print("오류: 1 또는 2만 입력 가능합니다.", file=sys.stderr)
        return 1

    print("\n어느 시장 계좌로 입·출금했습니까?")
    print("  1 — 국장 (KR)")
    print("  2 — 미장 (US)")
    print("  3 — 코인 (COIN)")
    print("  4 — 합산만 (레거시 peak_total_equity)\n")
    mk_choice = input("선택 (1~4): ").strip()
    mk_map = {"1": "KR", "2": "US", "3": "COIN", "4": "ALL"}
    if mk_choice not in mk_map:
        print("오류: 1~4만 입력 가능합니다.", file=sys.stderr)
        return 1
    market = mk_map[mk_choice]

    unit = capital_amount_unit(market)
    try:
        amount_raw = input(f"금액 ({capital_unit_label(unit)}, 콤마 가능): ").strip()
        amount = parse_capital_amount(amount_raw)
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    withdraw = choice == "2"
    ok, msg = apply_capital_peak_adjustment(
        withdraw=withdraw,
        amount=amount,
        state_path=STATE_PATH,
        market=market,
    )
    if not ok:
        print(msg, file=sys.stderr)
        return 1

    print("\n--- 결과 ---")
    print(msg)
    print("메인 봇 다음 루프부터 위 고점 기준으로 Phase 5 MDD가 계산됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
