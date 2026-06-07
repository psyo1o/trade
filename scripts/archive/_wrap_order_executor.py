# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
p = ROOT / "run_bot.py"
text = p.read_text(encoding="utf-8")
m = re.search(r"^def _twap_krw_budget_slices", text, re.M)
m2 = re.search(r"^def _holding_duration_human", text, re.M)
assert m and m2
wrappers = '''
def _twap_krw_budget_slices(total_krw: float) -> list:
    from execution import order_executor as oe
    return oe.twap_krw_budget_slices(total_krw)


def _twap_usd_budget_slices(total_usd: float) -> list:
    from execution import order_executor as oe
    return oe.twap_usd_budget_slices(total_usd)


def _execute_kr_market_buy_twap(
    t: str,
    kr_name: str,
    target_budget: float,
    curr_p: float,
    sl_p: float,
    entry_atr: float,
    t_name: str,
    s_name: str,
    state: dict,
    kr_cash_holder: list,
    *,
    strategy_type: str = "TREND_V8",
    entry_fib_level: float = 0.0,
) -> bool:
    from execution import order_executor as oe
    return oe.execute_kr_market_buy_twap(
        t,
        kr_name,
        target_budget,
        curr_p,
        sl_p,
        entry_atr,
        t_name,
        s_name,
        state,
        kr_cash_holder,
        strategy_type=strategy_type,
        entry_fib_level=entry_fib_level,
    )


def _execute_us_market_buy_twap(
    t: str,
    us_name: str,
    target_budget_usd: float,
    curr_p: float,
    sl_p: float,
    entry_atr: float,
    t_name: str,
    s_name: str,
    state: dict,
    us_cash_holder: list,
    *,
    strategy_type: str = "TREND_V8",
    entry_fib_level: float = 0.0,
) -> bool:
    from execution import order_executor as oe
    return oe.execute_us_market_buy_twap(
        t,
        us_name,
        target_budget_usd,
        curr_p,
        sl_p,
        entry_atr,
        t_name,
        s_name,
        state,
        us_cash_holder,
        strategy_type=strategy_type,
        entry_fib_level=entry_fib_level,
    )


def _execute_coin_market_buy_twap(
    t: str,
    budget_krw: float,
    sl_p: float,
    entry_atr: float,
    s_name: str,
    state: dict,
    krw_bal_holder: list,
    held_coins_mut: list,
    *,
    strategy_type: str = "TREND_V8",
    entry_fib_level: float = 0.0,
) -> bool:
    from execution import order_executor as oe
    return oe.execute_coin_market_buy_twap(
        t,
        budget_krw,
        sl_p,
        entry_atr,
        s_name,
        state,
        krw_bal_holder,
        held_coins_mut,
        strategy_type=strategy_type,
        entry_fib_level=entry_fib_level,
    )


'''
p.write_text(text[: m.start()] + wrappers + text[m2.start() :], encoding="utf-8")
print("OK removed", m2.start() - m.start(), "bytes")
