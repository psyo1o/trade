# -*- coding: utf-8 -*-
"""Phase5 청산 직전 AI 종합 판단 — ``strategy.ai_filter.evaluate_llm_json_prompt`` 공통 경로."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from strategy.ai_filter import (
    _gemini_model_from_config,
    evaluate_llm_json_prompt,
    summarize_ai_rationale,
)


def _build_liquidation_prompt(context_text: str) -> str:
    return (
        "당신은 자동매매 봇의 Phase5(시장 전량 청산) 최종 승인 심사관입니다.\n"
        "1차 서킷은 **해당 시장 잔고(예수+보유)가 고점 대비 ~15% 하락**했을 때만 발동합니다.\n"
        "**지수·종목 고점 하락만으로는 청산하지 않습니다.** 지수는 교차검증용 참고입니다.\n\n"
        "핵심 임무: **진짜 자산 붕괴**인지, **잔고/스냅샷/정산 지연 오류**인지 구분하십시오.\n\n"
        "【진짜 폭락 → 높은 점수】\n"
        "- KIS/잔고 재조회 후에도 risk 총평이 고점 대비 크게 하락\n"
        "- 보유 평가도 같이 빠지거나, 현금+보유 합이 실제로 줄었음\n"
        "- (참고) 시장 지수도 동반 약세면 설득력 증가 — 필수 아님\n\n"
        "【데이터 오류 → 낮은 점수(청산 금지)】\n"
        "- risk vs snap 괴리가 크거나, data_error_suspect=true\n"
        "- 매도 직후 예수 미반영·정산 지연 의심\n"
        "- 보유 종목 평가·수량은 멀쩡한데 총평만 급감\n"
        "- 과거 sanitize/스냅샷 버그 패턴(총평만 반토막)\n\n"
        "【점수】\n"
        "- 80~100: 실폭락 확실 → 청산\n"
        "- 60~79: 청산 쪽이나 일부 불확실\n"
        "- 40~59: 보류\n"
        "- 0~39: 오류·정산 지연 의심 → 청산 금지\n\n"
        '출력은 JSON만: {"liquidation_score": <int>, "rationale": "한국어 2~4문장"}\n\n'
        f"{context_text}\n"
    )


def _recent_sell_suspect(state: dict, market: str) -> bool:
    """최근 매도/동기화로 정산 지연이 의심되면 True."""
    mk = str(market or "").strip().upper()
    flags = (
        f"last_sell_ts_{mk.lower()}",
        "last_sell_ts",
        f"phase5_last_sell_{mk}",
    )
    now = time.time()
    for k in flags:
        try:
            ts = float(state.get(k) or 0)
        except (TypeError, ValueError):
            continue
        if ts > 0 and (now - ts) < 3600:
            return True
    # orphaned-cash / settle hints already logged into state sometimes
    hint = state.get("_risk_settle_hint")
    if isinstance(hint, dict) and hint.get(mk):
        return True
    return False


def build_phase5_liquidation_context(
    state: dict,
    market: str,
    circuit_ev: dict[str, Any],
) -> str:
    """LLM 입력 — 잔고 MDD 근거 + 오류 판별용 교차검증 필드."""
    from services.ledger_valuation import (
        display_cash_from_state,
        equity_divergence_metrics,
        kis_display_total,
        ledger_holdings_value_native,
        market_equity_for_risk,
        normalize_ticker,
    )
    from utils.helpers import is_coin_ticker

    mk = str(market or "").strip().upper()
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    lines = [
        f"datetime_kst={now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"market={mk}",
        "circuit_basis=account_equity_peak_mdd",
        "note=index_and_ticker_peaks_are_NOT_liquidation_triggers",
        f"circuit_reason={circuit_ev.get('reason', '')}",
        f"account_peak={circuit_ev.get('peak', 0)}",
        f"account_current={circuit_ev.get('current', 0)}",
        f"account_drawdown_pct={circuit_ev.get('drawdown_pct', 0)}",
        f"trigger_drawdown_pct={circuit_ev.get('trigger_drawdown_pct', 15)}",
    ]

    # 지수 = 참고만 (매수 급락 게이트와 동일 벤치)
    try:
        from strategy.market_benchmark import benchmark_ticker, daily_change_pct

        lines.append(f"index_benchmark_ref={benchmark_ticker(mk)}")
        lines.append(f"index_daily_change_pct_ref={daily_change_pct(mk):+.2f}")
    except Exception:
        lines.append("index_benchmark_ref=unavailable")

    positions = state.get("positions")
    holding_n = 0
    holdings_sum = 0.0
    if isinstance(positions, dict):
        for code, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            ticker = normalize_ticker(str(code))
            is_kr = ticker.isdigit() and len(ticker) == 6
            is_coin = is_coin_ticker(ticker)
            if mk == "KR" and not is_kr:
                continue
            if mk == "US" and (is_kr or is_coin):
                continue
            if mk == "COIN" and not is_coin:
                continue
            try:
                qty = float(pos.get("qty") or 0)
                buy_p = float(pos.get("buy_p") or 0)
                curr_p = float(pos.get("curr_p") or buy_p or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            holding_n += 1
            mv = qty * curr_p
            holdings_sum += mv
            pnl = ((curr_p - buy_p) / buy_p * 100.0) if buy_p > 0 else 0.0
            lines.append(
                f"holding ticker={ticker} qty={qty:g} buy={buy_p:g} curr={curr_p:g} "
                f"mv={mv:g} unrealized_pnl_pct={pnl:.2f}"
            )
    lines.append(f"holding_count={holding_n}")

    if mk in ("KR", "US"):
        cash = display_cash_from_state(state, mk)
        stock = ledger_holdings_value_native(state, mk)
        risk = market_equity_for_risk(state, mk)
        snap = kis_display_total(state, mk)
        risk2, snap2, div_pct = equity_divergence_metrics(state, mk)
        unit = "KRW" if mk == "KR" else "USD"
        peak = float(circuit_ev.get("peak", 0) or 0)
        cash_plus_stock = float(cash) + float(stock)
        settle = _recent_sell_suspect(state, mk)
        # 총평만 급감·분해 합은 고점 근처 → 오류 의심
        data_error = False
        if peak > 0 and float(risk) < peak * 0.90:
            if cash_plus_stock >= peak * 0.92:
                data_error = True
            if div_pct >= 10.0:
                data_error = True
            if settle and div_pct >= 5.0:
                data_error = True
        lines.extend(
            [
                f"account_cash_{unit}={cash:,.2f}",
                f"account_holdings_{unit}={stock:,.2f}",
                f"account_cash_plus_holdings_{unit}={cash_plus_stock:,.2f}",
                f"account_risk_total_{unit}={risk:,.2f}",
                f"account_snap_total_{unit}={snap:,.2f}",
                f"risk_vs_snap_divergence_pct={div_pct:.2f}",
                f"recent_sell_or_settle_suspect={str(settle).lower()}",
                f"data_error_suspect={str(data_error).lower()}",
                "circuit_uses=account_equity_peak_mdd_not_index",
            ]
        )
    elif mk == "COIN":
        from api import coin_broker as _cb

        coin_native = float(_cb.circuit_aux_coin_native(state) or 0)
        coin_krw = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
        unit = _cb.coin_equity_quote_unit()
        lines.append(f"account_coin_total_native={coin_native:,.4f}")
        lines.append(f"account_coin_unit={unit}")
        lines.append(f"account_coin_total_krw={coin_krw:,.0f}")
        lines.append(f"holdings_mark_to_market_sum={holdings_sum:,.4f}")
        lines.append("circuit_uses=account_equity_peak_mdd_not_index")
        lines.append(
            "note=Phase5_COIN_MDD_uses_native_quote_currency_not_FX"
        )

    summary = circuit_ev.get("reconfirm_summary")
    if summary:
        lines.append(f"reconfirm_before_after={summary}")
    trail = circuit_ev.get("reconfirm_trail")
    if isinstance(trail, list):
        for i, row in enumerate(trail):
            lines.append(f"reconfirm_step_{i}={row}")

    return "\n".join(lines)


def _llm_failure_non_retryable(rationale: str) -> bool:
    """404·429 등 동일 호출을 반복해도 성공 불가."""
    r = str(rationale or "").lower()
    return any(
        tok in r
        for tok in ("429", "spending cap", "404", "google_api_key 없음", "api_key 없음")
    )


def evaluate_phase5_ai_liquidation(
    state: dict,
    market: str,
    circuit_ev: dict[str, Any],
    *,
    threshold: int = 70,
    provider: str = "gemini",
    config: dict | None = None,
    max_retries: int = 1,
    retry_delay_sec: float = 2.0,
    gemini_max_model_attempts: int = 1,
) -> dict[str, Any]:
    """Phase5 청산 AI 심사 — 매수 AI와 동일 LLM 경로."""
    mk = str(market or "").strip().upper()
    context = build_phase5_liquidation_context(state, mk, circuit_ev)
    prompt = _build_liquidation_prompt(context)
    provider_in = str(provider or "gemini").strip().lower()
    gemini_model = _gemini_model_from_config(config, "")

    score = 0
    rationale = ""
    llm_success = False
    attempts = 0
    engine = "skip"

    for attempt in range(1, max(1, int(max_retries)) + 1):
        attempts = attempt
        score, rationale, llm_success, engine = evaluate_llm_json_prompt(
            prompt,
            config,
            provider=provider_in,
            gemini_model=gemini_model,
            gemini_max_model_attempts=int(gemini_max_model_attempts),
        )
        if llm_success:
            break
        if _llm_failure_non_retryable(rationale):
            break
        if attempt < max_retries:
            print(
                f"  ⏳ [Phase5·AI·{mk}] LLM 응답 없음({engine}: {rationale}) — "
                f"{retry_delay_sec:.0f}s 후 재시도 ({attempt}/{max_retries})"
            )
            time.sleep(float(retry_delay_sec))

    # 하드 가드: 컨텍스트가 오류 의심이면 점수와 무관하게 보류하지는 않되,
    # data_error_suspect 는 프롬프트에 명시 — 추가 로컬 컷은 과도할 수 있어 AI에 맡김.
    proceed = bool(llm_success and int(score) >= int(threshold))
    short = summarize_ai_rationale(rationale, max_chars=200)

    return {
        "market": mk,
        "liquidation_score": int(score),
        "threshold": int(threshold),
        "proceed": proceed,
        "rationale": rationale,
        "rationale_short": short,
        "attempts": attempts,
        "llm_success": llm_success,
        "evaluation_engine": engine,
        "provider": provider_in,
    }
