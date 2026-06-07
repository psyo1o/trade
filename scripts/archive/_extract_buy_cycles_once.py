# -*- coding: utf-8 -*-
"""1회성: run_bot 매수 루프 → execution/market_cycles/*_buy_cycle.py"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
text = (ROOT / "run_bot.py").read_text(encoding="utf-8")

RB_NAMES = """
_phase4_hedge_only_active _apply_phase4_hedge_buy_targets _merge_hedge_into_buy_targets
get_market_index_change INDEX_CRASH_KR INDEX_CRASH_US WEATHER_LABEL_BEAR
get_kr_company_name get_us_company_name in_ticker_cooldown ticker_cooldown_human in_cooldown
order_idem allow_kr_sector_entry allow_us_sector_entry normalize_ticker MAX_POSITIONS_KR MAX_POSITIONS_US
get_cached_ohlcv kis_api decide_entry_signals _v8_trend_buy_allowed_in_weather _is_hedge_ticker
swing_entry_sl_p _position_ratio_with_vol_target atr_pct_from_ohlcv _portfolio_heat_snapshot
_log_portfolio_heat_block _can_open_new_respecting_hedge_bypass _ai_false_breakout_buy_gate
AI_FALSE_BREAKOUT_THRESHOLD get_safe_atr _execute_kr_market_buy_twap _execute_us_market_buy_twap
_register_swing_risk_after_buy get_ohlcv_yfinance get_top_market_cap_tickers _sort_buy_targets_by_rs
""".split()


def extract_func(name: str) -> str:
    pat = rf"^def {name}\("
    m = re.search(pat, text, re.M)
    if not m:
        raise SystemExit(f"{name} not found")
    rest = text[m.end() :]
    m2 = re.search(r"^def \w+\(", rest, re.M)
    end = m.end() + m2.start() if m2 else len(text)
    return text[m.start() : end].rstrip() + "\n"


def rb_prefix(src: str) -> str:
    for name in RB_NAMES:
        src = re.sub(rf"(?<!rb\.)\b{re.escape(name)}\b", f"rb.{name}", src)
    return src.replace("rb.rb.", "rb.")


def build_kr(src: str) -> str:
    src = src.replace("def _run_kr_buy_cycle(", "def run_kr_buy_cycle(", 1)
    src = re.sub(
        r"def run_kr_buy_cycle\(\n    ctx,\n    \*,\n    state: dict,\n"
        r"    weather: dict,\n    macro_mult: float,\n    macro_snap: dict,\n"
        r"    held_kr,\n    kr_cash: int,\n    total_kr_equity: float,\n"
        r"    buy_cycle_tag: str,\n    alpha_target_vol: float,\n\) -> int:",
        "def run_kr_buy_cycle(\n"
        "    ctx: TradingCycleContext,\n    *,\n    held_kr: Any,\n"
        "    kr_cash: int,\n    total_kr_equity: float,\n    alpha_target_vol: float,\n) -> int:",
        src,
        count=1,
    )
    doc_end = src.find('"""', src.find('"""국장')) + 3
    insert = (
        "\n    rb = _rb()\n"
        "    state = ctx.state\n"
        "    weather = ctx.weather\n"
        "    macro_mult = ctx.macro_mult\n"
        "    macro_snap = ctx.macro_snap\n"
        "    buy_cycle_tag = ctx.buy_cycle_tag\n"
    )
    src = src[:doc_end] + insert + src[doc_end:]
    return rb_prefix(src)


def build_us(src: str) -> str:
    src = src.replace("def _run_us_buy_cycle(", "def run_us_buy_cycle(", 1)
    src = re.sub(
        r"def run_us_buy_cycle\(\n    ctx,\n    \*,\n    state: dict,\n"
        r"    weather: dict,\n    macro_mult: float,\n    macro_snap: dict,\n"
        r"    held_us,\n    us_cash: float,\n    total_us_equity: float,\n"
        r"    buy_cycle_tag: str,\n    alpha_target_vol: float,\n\) -> float:",
        "def run_us_buy_cycle(\n"
        "    ctx: TradingCycleContext,\n    *,\n    held_us: Any,\n"
        "    us_cash: float,\n    total_us_equity: float,\n    alpha_target_vol: float,\n) -> float:",
        src,
        count=1,
    )
    doc_end = src.find('"""', src.find('"""미장')) + 3
    insert = (
        "\n    rb = _rb()\n"
        "    state = ctx.state\n"
        "    weather = ctx.weather\n"
        "    macro_mult = ctx.macro_mult\n"
        "    macro_snap = ctx.macro_snap\n"
        "    buy_cycle_tag = ctx.buy_cycle_tag\n"
    )
    src = src[:doc_end] + insert + src[doc_end:]
    return rb_prefix(src)


HEADER = '''# -*- coding: utf-8 -*-
"""{market} 매수 루프 — ``run_bot._run_{m}_buy_cycle`` 에서 분리 (로직 동일)."""
from __future__ import annotations

import traceback
from typing import Any

from execution.market_cycles.context import TradingCycleContext


def _rb():
    import run_bot as rb

    return rb


'''

kr_body = build_kr(extract_func("_run_kr_buy_cycle"))
us_body = build_us(extract_func("_run_us_buy_cycle"))
(ROOT / "execution/market_cycles/kr_buy_cycle.py").write_text(
    HEADER.format(market="KR", m="kr") + kr_body, encoding="utf-8"
)
(ROOT / "execution/market_cycles/us_buy_cycle.py").write_text(
    HEADER.format(market="US", m="us") + us_body, encoding="utf-8"
)
print("OK", "kr", len(kr_body.splitlines()), "us", len(us_body.splitlines()))
