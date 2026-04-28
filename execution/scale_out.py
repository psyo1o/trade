# -*- coding: utf-8 -*-
"""
V7.1 조건부 50% 분할 익절(Scale-Out).

* 조건: 수익률 ≥ ``SCALE_OUT_PROFIT_PCT`` & 진입·평가 중 큰 값×수량(원화) ≥ ``SCALE_OUT_MIN_NOTIONAL_KRW``
  & ``scale_out_done`` 가 거짓.
* 국·미: ``sell_qty = total_qty // 2`` (0이면 스킵 = 1주만 보유).
* 코인: ``total_qty * 0.5`` 후 소수 버림(truncate).
* 최소 주문: 국·미는 ``sell_qty * 현재가 ≥ 1주 현재가``, 코인은 ``≥ 5000`` 원.
"""
from __future__ import annotations

import math
import time
from typing import Callable, List, Sequence

from execution.order_twap import plan_sell_qty_twap, run_qty_slice_sells

SCALE_OUT_PROFIT_PCT = 30.0
SCALE_OUT_MIN_NOTIONAL_KRW = 3_000_000.0
COIN_DECIMALS = 8
SCALE_OUT_ENTRY_ATR_MULT = 3.0


def position_scale_out_done(pos: dict) -> bool:
    return bool(isinstance(pos, dict) and pos.get("scale_out_done"))


def notional_krw_kr_us(buy_p: float, curr_p: float, qty: float, is_us: bool, usdkrw: float) -> float:
    """진입·평가 중 큰 쪽 기준 명목(원화)."""
    bp = float(buy_p or 0)
    cp = float(curr_p or 0)
    q = abs(float(qty or 0))
    notion = max(abs(bp * q), abs(cp * q))
    if is_us:
        return notion * float(usdkrw)
    return notion


def compute_stock_scale_out_qty(total_qty: int) -> int | None:
    if int(total_qty) <= 0:
        return None
    sq = int(int(total_qty) // 2)
    return sq if sq > 0 else None


def truncate_coin_qty(qty: float, decimals: int = COIN_DECIMALS) -> float:
    if qty <= 0:
        return 0.0
    m = 10**decimals
    return math.floor(float(qty) * m) / m


def compute_coin_scale_out_qty(total_qty: float, curr_p: float) -> float | None:
    if total_qty <= 0 or curr_p <= 0:
        return None
    t = truncate_coin_qty(float(total_qty) * 0.5)
    return t if t > 0 else None


def stock_scale_out_min_notional_ok(sell_qty: int, curr_p: float) -> bool:
    """국·미: 매도 명목(해당 시장 통화)이 1주 현재가 이상."""
    if sell_qty <= 0 or curr_p <= 0:
        return False
    return float(sell_qty) * float(curr_p) >= float(curr_p)


def coin_scale_out_min_notional_ok(sell_qty: float, curr_p: float, min_krw: float) -> bool:
    return float(sell_qty) * float(curr_p) >= float(min_krw)


def scale_out_trigger_ok(pos: dict, profit_rate: float, notional_krw: float) -> bool:
    if position_scale_out_done(pos):
        return False
    if float(profit_rate) < SCALE_OUT_PROFIT_PCT:
        return False
    if float(notional_krw) < SCALE_OUT_MIN_NOTIONAL_KRW:
        return False
    return True


def scale_out_price_target_hit(
    buy_price: float,
    curr_price: float,
    entry_atr: float | None,
    *,
    fallback_profit_pct: float = SCALE_OUT_PROFIT_PCT,
    atr_mult: float = SCALE_OUT_ENTRY_ATR_MULT,
) -> tuple[bool, str, float]:
    """
    V8.0 분할 익절 목표가 판정.

    Returns
        (hit, mode, target_price)
        - mode: ``entry_atr`` 또는 ``fallback_profit``
    """
    bp = float(buy_price or 0)
    cp = float(curr_price or 0)
    ea = float(entry_atr or 0)
    if bp <= 0 or cp <= 0:
        return False, "fallback_profit", 0.0
    if ea > 0:
        target = bp + (ea * float(atr_mult))
        return cp >= target, "entry_atr", float(target)
    target = bp * (1.0 + float(fallback_profit_pct) / 100.0)
    return cp >= target, "fallback_profit", float(target)


def post_partial_ledger(
    pos: dict,
    sell_qty: float,
    exec_px: float,
    qty_before: float,
    *,
    set_scale_out_done: bool = True,
) -> dict:
    """부분 매도 후 ``buy_p``·``qty``·``max_p``·``sl_p`` 보정. 수동 부분 매도 등에서는 ``scale_out_done`` 을 건드리지 않도록 할 수 있다."""
    out = dict(pos) if isinstance(pos, dict) else {}
    bp = float(out.get("buy_p") or 0)
    px = float(exec_px or 0)
    q0 = float(qty_before)
    sold = float(sell_qty)
    rem = max(0.0, q0 - sold)
    if rem <= 1e-12:
        return out
    inv = bp * q0
    cost_out = px * sold
    new_bp = (inv - cost_out) / rem if rem > 1e-12 else bp
    if not math.isfinite(new_bp) or new_bp <= 0:
        new_bp = bp
    out["buy_p"] = float(new_bp)
    if abs(rem - round(rem)) < 1e-9:
        out["qty"] = int(round(rem))
    else:
        out["qty"] = float(rem)
    old_sl = float(out.get("sl_p") or 0)
    if bp > 0 and old_sl > 0 and new_bp > 0:
        out["sl_p"] = float(new_bp * (old_sl / bp))
    old_max = float(out.get("max_p") or px)
    out["max_p"] = max(old_max, px)
    if set_scale_out_done:
        out["scale_out_done"] = True
    return out


def plan_coin_sell_chunks(
    qty: float,
    price: float,
    *,
    threshold_krw: float,
    max_parts: int = 5,
) -> List[float]:
    """코인 수량을 TWAP 기준 원화 명목으로 쪼갬."""
    notion = float(qty) * float(price)
    q = truncate_coin_qty(qty)
    if q <= 0 or notion <= 0 or notion < float(threshold_krw):
        return [q] if q > 0 else []
    n = min(int(max_parts), max(2, math.ceil(notion / float(threshold_krw))))
    base = truncate_coin_qty(q / n)
    if base <= 0:
        return [q]
    out: List[float] = []
    acc = 0.0
    for _ in range(n - 1):
        out.append(base)
        acc += base
    last = truncate_coin_qty(q - acc)
    if last <= 0:
        last = base
    out.append(last)
    return [x for x in out if x > 0]


def run_stock_scale_out_slices(
    sell_qty: int,
    notional_krw: float,
    threshold_krw: float,
    execute_qty: Callable[[int], bool],
    delay_sec: float,
) -> bool:
    chunks = plan_sell_qty_twap(int(sell_qty), float(notional_krw), threshold_krw=float(threshold_krw))
    return run_qty_slice_sells(chunks, execute_qty, delay_sec=float(delay_sec))


def run_coin_scale_out_chunks(
    chunks: Sequence[float],
    execute_vol: Callable[[float], bool],
    delay_sec: float,
) -> bool:
    """코인 분할 시장가 매도. 각 덩어리는 ``truncate_coin_qty`` 적용 후 양수만 실행."""
    flist = [truncate_coin_qty(float(c)) for c in chunks]
    flist = [x for x in flist if x > 0]
    if not flist:
        return False
    for i, v in enumerate(flist):
        if not execute_vol(float(v)):
            return False
        if delay_sec > 0 and i < len(flist) - 1:
            time.sleep(float(delay_sec))
    return True
