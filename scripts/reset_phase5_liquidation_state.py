# -*- coding: utf-8 -*-
"""
Phase5 대기청산·서킷 쿨다운·실패 멱등(phase5) 해제 — bot_state.json 정리.

사용:
  py -3.11 scripts/reset_phase5_liquidation_state.py
  py -3.11 scripts/reset_phase5_liquidation_state.py --restore-from-bak
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "bot_state.json"
BAK_PATH = ROOT / "bot_state.json.bak"

KEYS_CLEAR = (
    "phase5_pending_liquidation_markets",
    "phase5_pending_liquidation",
    "account_circuit_market_cooldowns",
    "account_circuit_cooldown_until",
)

KEYS_FALSE = ("account_circuit_peak_reset_pending",)


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    return json.loads(raw)


def _prune_phase5_idempotency(state: dict) -> int:
    """phase5 레인 failed/inflight 만 제거 (filled 체결 기록은 유지)."""
    oid = state.get("order_idempotency")
    if not isinstance(oid, dict):
        return 0
    removed = 0
    for k in list(oid.keys()):
        ent = oid.get(k)
        if not isinstance(ent, dict):
            continue
        key_l = str(k).lower()
        note = str(ent.get("note", "") or "").lower()
        status = str(ent.get("status", "") or "").lower()
        is_phase5 = ":phase5:" in key_l or "phase5" in note
        if is_phase5 and status in ("failed", "inflight", "pending"):
            del oid[k]
            removed += 1
    return removed


def _merge_restore_if_empty(main: dict, bak: dict) -> bool:
    """메인 positions 가 비었으면 .bak 에서 장부·표시·circuit_aux 복구."""
    pos = main.get("positions")
    if isinstance(pos, dict) and pos:
        return False
    bak_pos = bak.get("positions")
    if not isinstance(bak_pos, dict) or not bak_pos:
        return False
    for key in (
        "positions",
        "stats",
        "cooldown",
        "ticker_cooldowns",
        "last_kis_display_snapshot",
        "last_coin_display_snapshot",
        "settings",
        "circuit_aux_last_kr_krw",
        "circuit_aux_last_usd_total",
        "circuit_aux_last_coin_krw",
        "last_kr_cash_krw",
        "last_us_cash_usd",
        "peak_total_equity",
        "phase5_last_loop_total_krw",
        "phase5_share_anchor",
        "phase5_share_anchor_week",
        "last_reset_week",
        "_phase5_aux_sync",
        "order_idempotency",
        "buy_inflight",
        "sell_inflight",
    ):
        if key in bak:
            main[key] = bak[key]
    return True


def reset_state(state: dict) -> list[str]:
    lines: list[str] = []
    for k in KEYS_CLEAR:
        if k in state:
            state.pop(k, None)
            lines.append(f"removed {k}")
    for k in KEYS_FALSE:
        if state.get(k):
            state[k] = False
            lines.append(f"set {k}=False")
    n = _prune_phase5_idempotency(state)
    if n:
        lines.append(f"pruned {n} phase5 failed/inflight idempotency keys")
    state.setdefault("sell_inflight", {})
    state.setdefault("buy_inflight", {})
    if isinstance(state.get("sell_inflight"), dict):
        state["sell_inflight"].clear()
    if isinstance(state.get("buy_inflight"), dict):
        state["buy_inflight"].clear()
        lines.append("cleared buy_inflight / sell_inflight")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--restore-from-bak",
        action="store_true",
        help="positions 비어 있으면 bot_state.json.bak 에서 복구 후 플래그 해제",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    main_st = _load_json(STATE_PATH)
    if not main_st:
        main_st = {"positions": {}, "stats": {"wins": 0, "losses": 0, "total_profit": 0.0}}

    if args.restore_from_bak or not main_st.get("positions"):
        bak = _load_json(BAK_PATH)
        if _merge_restore_if_empty(main_st, bak):
            print("restored positions & display/circuit_aux from bot_state.json.bak")

    if not STATE_PATH.is_file() and not args.dry_run:
        pass
    elif STATE_PATH.is_file() and not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(STATE_PATH, STATE_PATH.with_name(f"bot_state.before_reset_{stamp}.json"))

    changes = reset_state(main_st)
    for line in changes:
        print(line)

    if args.dry_run:
        print("[dry-run] not saved")
        return 0

    STATE_PATH.write_text(
        json.dumps(main_st, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"saved {STATE_PATH} (positions={len(main_st.get('positions') or {})})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
