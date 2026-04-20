# -*- coding: utf-8 -*-
"""
수동 입·출금 반영 → Phase 5 합산 MDD용 고점(`peak_equity_total_krw`) 보정.

메인 봇은 `execution.guard.update_peak_equity_total_krw` 로 고점을 올리며,
현금만 입출금하면 평가금이 변해 고점 대비 드로다운이 왜곡될 수 있다.
이 스크립트는 **기록된 합산 고점**에 입금액을 더하거나 출금액을 빼서
다음 루프부터 동일 기준으로 MDD(-15% 등)가 계산되도록 한다.

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
    PEAK_EQUITY_TOTAL_KRW_KEY,
    load_state,
    save_state,
)

STATE_PATH = ROOT / "bot_state.json"
CAPITAL_ADJUSTMENTS_KEY = "capital_adjustments"


def portfolio_total_krw_estimated(state: dict) -> float:
    """봇이 마지막으로 장부에 남긴 시장별 스냅샷으로 합산 평가(원화) 추정."""
    rate = estimate_usdkrw()
    kr = float(state.get("circuit_aux_last_kr_krw", 0) or 0)
    coin = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    usd = float(state.get("circuit_aux_last_usd_total", 0) or 0)
    return kr + coin + usd * rate


def _parse_amount_krw(raw: str) -> float:
    s = raw.replace(",", "").strip()
    if not s:
        raise ValueError("금액이 비었습니다.")
    v = float(s)
    if v <= 0:
        raise ValueError("금액은 0보다 커야 합니다.")
    return v


def _refresh_aux_snapshot() -> None:
    """브로커·업비트 기준으로 circuit_aux_* 갱신 (실패 시 경고만)."""
    print("📡 실계좌 기준으로 합산 스냅샷(`circuit_aux_*`) 갱신 중…")
    try:
        import run_bot as rb

        st = load_state(STATE_PATH)
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


def main() -> int:
    print("=== 합산 자산 고점(Phase 5 MDD) 수동 보정 ===\n")

    _refresh_aux_snapshot()

    print("\n조작 종류를 선택하세요.")
    print("  1 — 입금 (고점에 금액만큼 가산)")
    print("  2 — 출금 (고점에서 금액만큼 감산)\n")

    choice = input("선택 (1 또는 2): ").strip()
    if choice not in ("1", "2"):
        print("오류: 1 또는 2만 입력 가능합니다.", file=sys.stderr)
        return 1

    try:
        amount_raw = input("금액 (원, 콤마 가능): ").strip()
        amount = _parse_amount_krw(amount_raw)
    except ValueError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    state = load_state(STATE_PATH)
    current_total = portfolio_total_krw_estimated(state)
    old_peak = float(state.get(PEAK_EQUITY_TOTAL_KRW_KEY, 0.0) or 0.0)

    if old_peak <= 0.0:
        print(
            f"ℹ️  장부에 합산 고점({PEAK_EQUITY_TOTAL_KRW_KEY})이 없거나 0입니다.\n"
            f"   직전 루프 기준 추정 총자산 {current_total:,.0f}원으로 고점을 간주하고 보정합니다.\n"
        )
        old_peak = current_total

    if choice == "2":
        if amount > current_total:
            print(
                f"오류: 출금액 {amount:,.0f}원이 현재 추정 총자산 {current_total:,.0f}원보다 큽니다.",
                file=sys.stderr,
            )
            return 1
        new_peak = old_peak - amount
        kind = "withdraw"
    else:
        new_peak = old_peak + amount
        kind = "deposit"

    if new_peak < 0.0:
        print(
            f"오류: 보정 후 고점이 음수가 됩니다 ({new_peak:,.0f}). 출금액 또는 장부 상태를 확인하세요.",
            file=sys.stderr,
        )
        return 1

    state[PEAK_EQUITY_TOTAL_KRW_KEY] = float(new_peak)

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "amount_krw": float(amount),
        "peak_before_krw": float(old_peak),
        "peak_after_krw": float(new_peak),
        "estimated_total_krw_at_adjust": float(current_total),
        "circuit_aux_after_refresh": {
            "kr_krw": float(state.get("circuit_aux_last_kr_krw", 0) or 0),
            "usd_total": float(state.get("circuit_aux_last_usd_total", 0) or 0),
            "coin_krw": float(state.get("circuit_aux_last_coin_krw", 0) or 0),
        },
        "source": "adjust_capital.py",
    }
    log = state.setdefault(CAPITAL_ADJUSTMENTS_KEY, [])
    if isinstance(log, list):
        log.append(entry)
    else:
        state[CAPITAL_ADJUSTMENTS_KEY] = [entry]

    save_state(STATE_PATH, state)

    print("\n--- 결과 ---")
    print(f"이전 합산 고점(보정 전): {old_peak:,.0f} 원")
    print(f"보정 후 합산 고점:     {new_peak:,.0f} 원")
    print(f"(참고) 추정 현재 총자산: {current_total:,.0f} 원")
    print(f"\n저장: {STATE_PATH}")
    print("메인 봇 다음 루프부터 위 고점 기준으로 Phase 5 MDD가 계산됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
