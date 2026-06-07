# -*- coding: utf-8 -*-
"""run_bot.py 시장별 블록 → execution/market_cycles/*.py (1회성)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_BOT = ROOT / "run_bot.py"
OUT_DIR = ROOT / "execution" / "market_cycles"

# 1-based inclusive line numbers from run_bot.py (if/else 전체 분기)
RANGES = {
    "kr": (4886, 5533),
    "us": (5539, 6185),
    "coin": (6191, 6828),
}


def _transform_body(name: str, body: str) -> str:
    body = body.replace("_cycle_buy_fills += 1", "ctx.buy_fills += 1")
    body = body.replace("_cycle_buy_zone_kr = True", "ctx.buy_zone_kr = True")
    body = body.replace("_cycle_buy_zone_us = True", "ctx.buy_zone_us = True")
    body = body.replace("_cycle_buy_zone_coin = True", "ctx.buy_zone_coin = True")
    if name == "kr":
        body = body.replace("final_targets", "ctx.final_targets_kr")
    return body


def _cycle_preamble(name: str) -> str:
    lines = [
        "    state = ctx.state",
        "    weather = ctx.weather",
        "    macro_mult = ctx.macro_mult",
        "    macro_reason = ctx.macro_reason",
        "    macro_snap = ctx.macro_snap",
        "    _buy_cycle_tag = ctx.buy_cycle_tag",
    ]
    if name == "kr":
        lines.append("    final_targets = ctx.final_targets_kr")
    return "\n".join(lines) + "\n"


def _indent_body(body: str) -> str:
    out = []
    for line in body.splitlines(keepends=True):
        if line.strip():
            out.append("    " + line)
        else:
            out.append(line)
    return "".join(out)


def main() -> None:
    lines = RUN_BOT.read_text(encoding="utf-8").splitlines(keepends=True)
    for name, (start, end) in RANGES.items():
        chunk = lines[start - 1 : end]
        body_lines = []
        for line in chunk:
            body_lines.append(line[4:] if line.startswith("    ") else line)
        body = _indent_body(_transform_body(name, "".join(body_lines)))
        header = (
            "# -*- coding: utf-8 -*-\n"
            f'"""{name.upper()} 시장 매매 사이클 — ``run_trading_bot`` 에서 분리 (로직 동일)."""\n'
            "from __future__ import annotations\n\n"
            "from execution.market_cycles.context import TradingCycleContext\n\n\n"
            "def _rb():\n"
            "    import run_bot as rb\n"
            "    return rb\n\n\n"
            f"def run_{name}_cycle(ctx: TradingCycleContext) -> None:\n"
            "    rb = _rb()\n"
            + _cycle_preamble(name)
            + "\n"
        )
        out = OUT_DIR / f"{name}_cycle.py"
        out.write_text(header + body, encoding="utf-8")
        print(f"wrote {out.name} ({len(body_lines)} lines)")


if __name__ == "__main__":
    main()
