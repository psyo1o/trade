# -*- coding: utf-8 -*-
"""국장 급락 청산 실행 — 판단은 ``strategy.kr_crash_exit``, 주문은 기존 KIS 매도 경로."""
from __future__ import annotations

from typing import Any

from strategy.kr_crash_exit import (
    format_shadow_krcrash_log,
    kr_listing_market,
    plan_kr_crash,
)
from strategy.shadow_events import append_shadow_event, now_kst_iso


def _position_buy_price(rb: Any, stock: dict, pos_info: dict) -> float:
    buy = rb._to_float(pos_info.get("buy_p", 0), 0.0) if isinstance(pos_info, dict) else 0.0
    if buy > 0:
        return float(buy)
    avg = rb._to_float(
        stock.get("pchs_avg_prc", stock.get("pchs_avg_pric", stock.get("prpr", 0))),
        0.0,
    )
    return float(avg or 0.0)


def _holding_price(rb: Any, stock: dict, ohlcv: list | None) -> float:
    try:
        from api.kis_parsers import parse_kr_live_price

        live = float(parse_kr_live_price(stock, rb._to_float) or 0.0)
    except Exception:
        live = 0.0
    if live > 0:
        return live
    if ohlcv and isinstance(ohlcv[-1], dict):
        try:
            close = float(ohlcv[-1].get("c") or 0.0)
        except (TypeError, ValueError):
            close = 0.0
        if close > 0:
            return close
    return 0.0


def _pnl_pct(price: float, buy_p: float) -> float | None:
    if buy_p <= 0 or price <= 0:
        return None
    return (float(price) - float(buy_p)) / float(buy_p) * 100.0


def execute_live_kr_crash_sell(
    rb: Any,
    state: dict,
    *,
    ticker: str,
    qty: int,
    price: float,
    buy_p: float,
    reason: str,
    strategy_type: str,
    cycle_tag: str,
    name: str,
) -> bool:
    """전량 시장가. 성공 시 장부 제거·24h 쿨다운(KR-CRASH). 실패 시 False."""

    def _place():
        return rb.create_market_sell_order_kis(
            ticker, int(qty), is_us=False, curr_price=float(price)
        )

    fill = rb._idempotent_kis_sell(
        state,
        market="KR",
        ticker=ticker,
        lane=rb.order_idem.LANE_KR_CRASH,
        qty=int(qty),
        fallback_price=float(price),
        place_order=_place,
        cycle_tag=cycle_tag,
    )
    tag = "♻️" if getattr(fill, "reused", False) else "🧾"
    print(f"  {tag} [KR-CRASH] {ticker} ok={fill.ok} qty={int(fill.qty)} note={fill.note}")
    if not fill.ok:
        print(f"  ❌ [KR-CRASH] {name}({ticker}) 매도 실패: {fill.note or '응답 없음'}")
        return False

    profit_rate = _pnl_pct(float(price), float(buy_p))
    profit_rate_f = float(profit_rate or 0.0)
    stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
    if profit_rate_f > 0:
        stats["wins"] = int(stats.get("wins", 0) or 0) + 1
    else:
        stats["losses"] = int(stats.get("losses", 0) or 0) + 1
    stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + profit_rate_f
    rb._record_trade_event(
        "KR",
        ticker,
        "SELL",
        int(qty),
        price=float(price),
        profit_rate=profit_rate_f,
        reason=reason,
    )
    print(f"  ✅ [국장 급락 청산] {name}({ticker}) | 수익률: {profit_rate_f:+.2f}% | 사유: {reason}")
    try:
        rb.send_telegram(
            f"🚨 [국장 급락 청산] {ticker}({name})\n사유: {reason}\n최종 수익률: {profit_rate_f:+.2f}%"
        )
    except Exception as exc:
        print(f"  ⚠️ [KR-CRASH] 텔레그램 실패: {type(exc).__name__}")

    def _mut(st: dict) -> None:
        rb.set_cooldown(st, ticker)
        rb.set_ticker_cooldown_after_sell(
            st,
            ticker,
            reason,
            profit_rate=profit_rate_f,
            strategy_type=strategy_type,
            market="KR",
            remaining_qty=0.0,
        )

    rb.ledger_apply.persist_position_remove(
        state, ticker, context="KR CRASH EXIT", state_path=rb.STATE_PATH, mutate_fn=_mut
    )
    return True


def apply_kr_crash_exit_for_holding(
    rb: Any,
    *,
    state: dict,
    stock: dict,
    ticker: str,
    ohlcv: list | None,
    snap: Any,
    cycle_tag: str,
    listing_lookup: Any = None,
    sell_fn: Any = None,
    events_path: str | None = None,
) -> str:
    """``noop`` | ``shadow`` | ``sold`` | ``failed``.

    shadow 는 로그만 남기고 주문·장부를 바꾸지 않는다.
    off 는 호출 전에 걸러지지만, 여기서도 noop.
    """
    mode = str(getattr(rb, "KR_CRASH_EXIT_MODE", "off") or "off")
    if str(mode).strip().lower() == "off":
        return "noop"
    qty = int(rb._to_float(stock.get("hldg_qty", stock.get("t01", stock.get("q", 0)))))
    if qty <= 0:
        return "noop"
    price = _holding_price(rb, stock, ohlcv)
    if price <= 0:
        return "noop"

    threshold = float(getattr(rb, "INDEX_CRASH_KR", -3.0))
    # KODEX 가 이미 임계 이하면 상장시장 조회를 생략할 수 있지만,
    # 기록용으로 캐시된 상장시장은  cheap 하다. 네트워크는 캐시 미스일 때만.
    lookup = listing_lookup or kr_listing_market
    try:
        listing = str(lookup(ticker) or "")
    except Exception:
        listing = ""

    plan = plan_kr_crash(
        mode,
        listing=listing,
        kodex=getattr(snap, "kodex", None),
        kospi=getattr(snap, "kospi", None),
        kosdaq=getattr(snap, "kosdaq", None),
        threshold=threshold,
    )
    if plan.action == "noop":
        return "noop"

    pos_info = (state.get("positions") or {}).get(ticker, {})
    if not isinstance(pos_info, dict):
        pos_info = {}
    buy_p = _position_buy_price(rb, stock, pos_info)
    pnl = _pnl_pct(price, buy_p)
    strategy_type = ""
    try:
        strategy_type = str(rb._resolve_sell_loop_strategy_type(pos_info) or "")
    except Exception:
        strategy_type = str(pos_info.get("strategy_type") or "")
    try:
        name = rb.get_kr_company_name(ticker)
    except Exception:
        name = ticker
    reason = f"[KR-CRASH] 지수 급락 청산 {plan.trigger}"

    event = {
        "rule": "kr_crash_exit",
        "mode": "shadow" if plan.action == "shadow" else "live",
        "ticker": ticker,
        "market": "KR",
        "price": float(price),
        "qty": int(qty),
        "position_pnl_pct": None if pnl is None else round(float(pnl), 4),
        "index_change_pct": plan.index_change_pct,
        "listing": plan.listing,
        "trigger": plan.trigger,
        "kodex_pct": getattr(snap, "kodex", None),
        "kospi_pct": getattr(snap, "kospi", None),
        "kosdaq_pct": getattr(snap, "kosdaq", None),
        "strategy_type": strategy_type,
        "session_date": now_kst_iso()[:10],
    }

    if plan.action == "shadow":
        line = format_shadow_krcrash_log(ticker, qty, price, pnl)
        print(f"  {line} | {plan.trigger}")
        event["action"] = "would_sell"
        append_shadow_event(event, path=events_path or getattr(rb, "SHADOW_EVENTS_PATH", None))
        return "shadow"

    print(f"  🚨 [KR-CRASH-LIVE] sell {ticker} {qty} at {price:g}, {plan.trigger}")
    if sell_fn is not None:
        ok = bool(sell_fn())
    else:
        try:
            ok = execute_live_kr_crash_sell(
                rb,
                state,
                ticker=ticker,
                qty=qty,
                price=price,
                buy_p=buy_p,
                reason=reason,
                strategy_type=strategy_type,
                cycle_tag=cycle_tag,
                name=name,
            )
        except Exception as exc:
            print(f"  ❌ [KR-CRASH] {ticker} 매도 예외: {type(exc).__name__}: {exc}")
            ok = False
    event["action"] = "sell" if ok else "sell_failed"
    append_shadow_event(event, path=events_path or getattr(rb, "SHADOW_EVENTS_PATH", None))
    return "sold" if ok else "failed"
