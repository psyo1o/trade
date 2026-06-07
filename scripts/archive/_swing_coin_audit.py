# -*- coding: utf-8 -*-
"""일회성: 보유 코인 스윙 매도선 점검."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from api import coin_broker, coin_config
from strategy.rules import (
    check_swing_exit,
    get_swing_exit_display_price,
    get_swing_scale_out_target_price,
    SWING_SCALE_OUT_R_MULT,
    get_swing_hard_stop_floor,
    get_swing_profit_lock_floor,
    reconcile_swing_position,
    _swing_profit_lock_tier_label,
)

state = json.loads((ROOT / "bot_state.json").read_text(encoding="utf-8"))
positions = state.get("positions", {})
coins = {
    k: v
    for k, v in positions.items()
    if str(k).upper().startswith(("USDT-", "KRW-", "BTC-", "ETH-"))
}

print("=== 보유 코인 스윙 매도선 점검 ===")
print(f"거래소: {'바이낸스' if coin_config.is_binance() else '업비트'}")
print()

for t, pos in coins.items():
    print(f"--- {t} ---")
    ohlcv = coin_broker.fetch_ohlcv(t, "day", 250) or []
    curr = float(coin_broker.get_current_price(t) or pos.get("curr_p") or 0)
    buy = float(pos.get("buy_p", 0))
    max_p = max(float(pos.get("max_p", buy)), curr)
    pos2 = dict(pos)
    pos2["max_p"] = max_p
    reconcile_swing_position(pos2, ohlcv, reference_price=curr)

    hard = get_swing_hard_stop_floor(pos2, ohlcv)
    lock = get_swing_profit_lock_floor(buy, max_p)
    exit_line = get_swing_exit_display_price(curr, pos2, ohlcv)
    half = get_swing_scale_out_target_price(pos2)
    action, reason = check_swing_exit(pos2, pd.DataFrame(ohlcv), reference_price=curr)

    profit = (curr - buy) / buy * 100 if buy else 0
    max_profit = (max_p - buy) / buy * 100 if buy else 0

    print(f"  전략: {pos.get('strategy_type')} | tier: {pos.get('tier')}")
    print(f"  평단: {buy:.8f}")
    print(f"  장부 sl_p(저장값): {float(pos.get('sl_p', 0)):.8f}")
    print(f"  entry_fib: {float(pos.get('entry_fib_level', 0)):.8f}")
    print(f"  max_p: {max_p:.8f} (최고수익 {max_profit:+.2f}%)")
    print(f"  현재가: {curr:.8f} (수익 {profit:+.2f}%)")
    print(f"  하드스탑(피보·구름): {hard:.8f}")
    lock_lbl = _swing_profit_lock_tier_label(buy, max_p) if lock > 0 else "미적용"
    print(f"  수익락 바닥: {lock:.8f} ({lock_lbl})")
    print(f"  >> 통합 매도선(표시): {exit_line:.8f}")
    if half:
        print(f"  1차익절 볼밴: {half:.8f}")
    print(f"  시그널: {action}" + (f" — {reason}" if reason else ""))
    if curr > 0 and exit_line > 0:
        print(f"  매도선 대비: {(curr / exit_line - 1) * 100:+.2f}%p")
    print(f"  일봉: {len(ohlcv)}개")
    print()
