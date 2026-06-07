# -*- coding: utf-8 -*-
"""1회성: coin_buy_cycle · order_executor 추출 (본문 이동, rb. 접두)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ORDER_RB = """
TWAP_ENABLED TWAP_KRW_THRESHOLD TWAP_USD_THRESHOLD TWAP_SLICE_DELAY_SEC TEST_MODE STATE_PATH
order_idem bal_read kis_api coin_config coin_broker upbit_api
create_market_buy_order_kis execute_us_order_direct refresh_brokers_if_needed
persist_position_registration ensure_position_registered _record_trade_event
send_telegram get_coin_name _coin_min_order_krw _coin_twap_filled_base_qty
_fmt_telegram_coin_unit_usdt
""".split()

COIN_RB = ORDER_RB + """
get_market_index_change INDEX_CRASH_COIN WEATHER_LABEL_BEAR
in_ticker_cooldown ticker_cooldown_human in_cooldown
decide_entry_signals swing_entry_sl_p get_safe_atr atr_pct_from_ohlcv
can_open_new _v8_trend_buy_allowed_in_weather _macro_market_buy_allowed
_sort_buy_targets_by_rs _position_ratio_with_vol_target _portfolio_heat_snapshot
_log_portfolio_heat_block _ai_false_breakout_buy_gate _coin_min_order_krw
_execute_coin_market_buy_twap _register_swing_risk_after_buy
AI_FALSE_BREAKOUT_THRESHOLD_COIN MAX_POSITIONS_COIN UPBIT_UNIVERSE_TOP BINANCE_UNIVERSE_TOP
requests coin_broker
""".split()


def rb_prefix(src: str, names: list[str]) -> str:
    for name in names:
        src = re.sub(rf"(?<!rb\.)\b{re.escape(name)}\b", f"rb.{name}", src)
    return src.replace("rb.rb.", "rb.")


def extract_coin_buy() -> None:
    text = (ROOT / "execution/market_cycles/coin_cycle.py").read_text(encoding="utf-8")
    start = text.index("                ctx.buy_zone_coin = True")
    end = text.index("\n    else:\n        print(\"💤 코인은 점검")
    body = text[start:end]
    # dedent 16 spaces -> 4
    lines = []
    for line in body.splitlines():
        if line.startswith("                "):
            lines.append("    " + line[16:])
        elif line.strip() == "":
            lines.append("")
        else:
            lines.append(line)
    body = "\n".join(lines).rstrip() + "\n"
    body = rb_prefix(body, COIN_RB)
    header = '''# -*- coding: utf-8 -*-
"""COIN 매수 루프 — ``coin_cycle`` 에서 분리 (로직 동일)."""
from __future__ import annotations

from typing import Any

from execution.market_cycles.context import TradingCycleContext


def _rb():
    import run_bot as rb

    return rb


def run_coin_buy_cycle(
    ctx: TradingCycleContext,
    *,
    coin_weather: str,
    held_coins: Any,
    krw_bal: float,
    total_coin_equity: float,
    alpha_target_vol: float,
) -> float:
    """코인 매수 — Phase4·지수·스캔·V8/스윙·TWAP (매수 창·MDD 통과 후 호출)."""
    rb = _rb()
    state = ctx.state
    macro_mult = ctx.macro_mult
    macro_snap = ctx.macro_snap
    macro_reason = ctx.macro_reason
    _buy_cycle_tag = ctx.buy_cycle_tag
'''
    out = header + body.replace("macro_mult", "ctx.macro_mult", 1)  # no - use local
    # fix: body uses macro_mult from outer - we set in header as macro_mult = ctx.macro_mult
    out = header + body
    (ROOT / "execution/market_cycles/coin_buy_cycle.py").write_text(out, encoding="utf-8")


def extract_order_executor() -> None:
    text = (ROOT / "run_bot.py").read_text(encoding="utf-8")
    m = re.search(r"^def _twap_krw_budget_slices", text, re.M)
    m_coin_helper = re.search(r"^def _coin_twap_filled_base_qty", text, re.M)
    m_coin_exec = re.search(r"^def _execute_coin_market_buy_twap", text, re.M)
    m_end = re.search(r"^def _holding_duration_human", text, re.M)
    chunk = (
        text[m.start() : m_coin_helper.start()]
        + text[m_coin_exec.start() : m_end.start()]
    )
    chunk = chunk.rstrip() + "\n"
    chunk = chunk.replace("def _twap_krw_budget_slices", "def twap_krw_budget_slices", 1)
    chunk = chunk.replace("def _twap_usd_budget_slices", "def twap_usd_budget_slices", 1)
    chunk = chunk.replace("def _execute_kr_market_buy_twap", "def execute_kr_market_buy_twap", 1)
    chunk = chunk.replace("def _execute_us_market_buy_twap", "def execute_us_market_buy_twap", 1)
    chunk = chunk.replace("def _execute_coin_market_buy_twap", "def execute_coin_market_buy_twap", 1)
    chunk = chunk.replace("_twap_krw_budget_slices(", "twap_krw_budget_slices(", 1)
    chunk = chunk.replace("_twap_usd_budget_slices(", "twap_usd_budget_slices(", 1)
    chunk = rb_prefix(chunk, ORDER_RB)
    chunk = chunk.replace("rb.rb.", "rb.")
    chunk = chunk.replace("rb.time.rb.time()", "time.time()")
    chunk = chunk.replace("time.time()", "__TIME_TIME__")
    chunk = re.sub(r"\btime\.sleep\b", "rb.time.sleep", chunk)
    chunk = chunk.replace("__TIME_TIME__", "time.time()")
    header = '''# -*- coding: utf-8 -*-
"""시장별 TWAP 매수 실행 — ``run_bot._execute_*_market_buy_twap`` 분리 (로직 동일)."""
from __future__ import annotations

import time

from execution.order_twap import plan_krw_slices, plan_usd_slices


def _rb():
    import run_bot as rb

    return rb


'''
    chunk = re.sub(
        r"(def twap_krw_budget_slices\([^\)]*\)[^\n]*\n)",
        r"\1    rb = _rb()\n",
        chunk,
        count=1,
    )
    chunk = re.sub(
        r"(def twap_usd_budget_slices\([^\)]*\)[^\n]*\n(?:    \"\"\".*?\"\"\"\n)?)",
        r"\1    rb = _rb()\n",
        chunk,
        count=1,
        flags=re.S,
    )
    for fn in ("execute_kr_market_buy_twap", "execute_us_market_buy_twap", "execute_coin_market_buy_twap"):
        chunk = re.sub(
            rf"(def {fn}\([^\)]*\)[^\n]*\n(?:    \"\"\".*?\"\"\"\n)?)",
            r"\1    rb = _rb()\n",
            chunk,
            count=1,
            flags=re.S,
        )
    (ROOT / "execution/order_executor.py").write_text(header + chunk, encoding="utf-8")


if __name__ == "__main__":
    extract_coin_buy()
    extract_order_executor()
    print("done")
