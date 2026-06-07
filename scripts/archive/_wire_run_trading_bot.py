# -*- coding: utf-8 -*-
"""run_trading_bot 시장 블록 → market_cycles 호출로 교체."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_BOT = ROOT / "run_bot.py"

START = 4882  # 1-based: comment before KR block
END = 6828  # 1-based: COIN else branch last line

REPLACEMENT = '''    # -------------------------------------------------------------------------
    # 시장별 엔진 — execution/market_cycles (B-1 분리, 로직·순서 동일)
    # -------------------------------------------------------------------------
    from execution.market_cycles import TradingCycleContext, run_coin_cycle, run_kr_cycle, run_us_cycle

    _cycle_ctx = TradingCycleContext(
        state=state,
        weather=weather,
        macro_mult=macro_mult,
        macro_reason=macro_reason,
        macro_snap=macro_snap,
        buy_cycle_tag=_buy_cycle_tag,
        final_targets_kr=final_targets,
    )
    run_kr_cycle(_cycle_ctx)
    run_us_cycle(_cycle_ctx)
    run_coin_cycle(_cycle_ctx)
    _cycle_buy_fills = _cycle_ctx.buy_fills
    _cycle_buy_zone_kr = _cycle_ctx.buy_zone_kr
    _cycle_buy_zone_us = _cycle_ctx.buy_zone_us
    _cycle_buy_zone_coin = _cycle_ctx.buy_zone_coin

'''


def main() -> None:
    lines = RUN_BOT.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines = lines[: START - 1] + [REPLACEMENT] + lines[END:]
    RUN_BOT.write_text("".join(new_lines), encoding="utf-8")
    removed = END - START + 1
    print(f"removed {removed} lines, inserted wire block")


if __name__ == "__main__":
    main()
