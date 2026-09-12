# -*- coding: utf-8 -*-
"""
Phase5 시장별 서킷 쿨다운 해제 · 고점 리셋 (오발동 복구).

사용:
  py -3.11 scripts/reset_phase5_market_circuit.py --market US
  py -3.11 scripts/reset_phase5_market_circuit.py --market KR --dry-run
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_PATH = ROOT / "bot_state.json"


def _load_state(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"state not found: {path}")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise SystemExit(f"empty state: {path}")
    return json.loads(raw)


def reset_market_circuit(state: dict, market: str, *, peak_override: float | None = None) -> list[str]:
    from execution.guard import (
        ACCOUNT_CIRCUIT_MARKET_PEAK_RESET_PENDING_KEY,
        PHASE5_MARKET_COOLDOWNS_KEY,
        PHASE5_PENDING_LIQUIDATION_MARKETS_KEY,
        peak_equity_market_key,
    )

    mk = str(market or "").strip().upper()
    if mk not in ("KR", "US", "COIN"):
        raise SystemExit(f"unsupported market: {market}")

    lines: list[str] = []
    mode = str(state.get("account_circuit_mode", "") or "").strip().lower()
    index_mode = mode == "per_market_index"
    cds = state.get(PHASE5_MARKET_COOLDOWNS_KEY)
    if isinstance(cds, dict) and mk in cds:
        old = cds.pop(mk)
        lines.append(f"removed cooldown {mk}={old}")
        if not cds:
            state.pop(PHASE5_MARKET_COOLDOWNS_KEY, None)

    pending = state.get(PHASE5_PENDING_LIQUIDATION_MARKETS_KEY)
    if isinstance(pending, list):
        kept = [x for x in pending if str(x).strip().upper() != mk]
        if len(kept) != len(pending):
            lines.append(f"removed {mk} from phase5_pending_liquidation_markets")
            if kept:
                state[PHASE5_PENDING_LIQUIDATION_MARKETS_KEY] = kept
            else:
                state.pop(PHASE5_PENDING_LIQUIDATION_MARKETS_KEY, None)
                state["phase5_pending_liquidation"] = False
                lines.append("phase5_pending_liquidation=false")

    peak_key = peak_equity_market_key(mk)
    old_peak = float(state.get(peak_key, 0) or 0)
    if index_mode:
        lines.append(f"account_circuit_mode=per_market_index — peak_equity_* 는 Phase5 미사용")
    elif peak_override is not None and peak_override > 0:
        new_peak = float(peak_override)
        state[peak_key] = new_peak
        lines.append(f"{peak_key} ← --peak {new_peak:,.2f}")
    elif mk in ("KR", "US"):
        from services.ledger_valuation import kis_display_total, ledger_holdings_value_native, market_equity_for_risk

        new_peak = float(market_equity_for_risk(state, mk))
        snap_t = float(kis_display_total(state, mk))
        holdings = float(ledger_holdings_value_native(state, mk))
        if snap_t > new_peak * 1.15 and holdings <= 0:
            lines.append(
                f"warning: 장부 {mk} 보유 0 — snap_total ${snap_t:,.2f} vs risk ${new_peak:,.2f}; snap 사용"
            )
            new_peak = snap_t
        state[peak_key] = new_peak
        lines.append(f"{peak_key}: {old_peak:,.2f} → {new_peak:,.2f}")
    else:
        from api import coin_broker as _cb
        from execution.guard import PEAK_EQUITY_COIN_UNIT_KEY

        new_peak = float(_cb.circuit_aux_coin_native(state) or 0)
        if new_peak <= 0:
            # 구버전: 원화만 있을 때
            krw = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
            if _cb.coin_equity_quote_unit() == "USDT" and krw > 0:
                rate = float(_cb.get_krw_per_usdt() or 0) or 1.0
                new_peak = krw / rate
            else:
                new_peak = krw
        if new_peak <= 0:
            lines.append(f"skip {peak_key} — coin native unknown")
        else:
            state[peak_key] = new_peak
            state[PEAK_EQUITY_COIN_UNIT_KEY] = _cb.coin_equity_quote_unit()
            unit = "USDT" if state[PEAK_EQUITY_COIN_UNIT_KEY] == "USDT" else "원"
            lines.append(f"{peak_key}: {old_peak:,.2f} → {new_peak:,.2f}{unit}")

    pr = state.get(ACCOUNT_CIRCUIT_MARKET_PEAK_RESET_PENDING_KEY)
    if isinstance(pr, dict) and mk in pr:
        pr[mk] = False
        state[ACCOUNT_CIRCUIT_MARKET_PEAK_RESET_PENDING_KEY] = pr
        lines.append(f"account_circuit_market_peak_reset_pending[{mk}]=false")

    if not index_mode:
        last_loop = state.get("phase5_last_loop_equity_by_market")
        if isinstance(last_loop, dict) and peak_key in state:
            last_loop[mk] = float(state.get(peak_key, 0) or 0)
            state["phase5_last_loop_equity_by_market"] = last_loop
            lines.append(f"phase5_last_loop_equity_by_market[{mk}]={last_loop[mk]:,.2f}")

    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase5 시장별 서킷 쿨다운·고점 오발동 복구")
    ap.add_argument("--market", required=True, choices=["KR", "US", "COIN", "kr", "us", "coin"])
    ap.add_argument("--peak", type=float, default=None, help="고점 수동 지정 (미지정 시 risk·스냅샷 자동)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", type=Path, default=STATE_PATH)
    args = ap.parse_args()

    state = _load_state(args.state)
    changes = reset_market_circuit(state, args.market, peak_override=args.peak)
    for line in changes:
        print(line)

    if args.dry_run:
        print("[dry-run] not saved")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = args.state.with_name(f"bot_state.before_circuit_reset_{stamp}.json")
    shutil.copy2(args.state, backup)
    print(f"backup → {backup.name}")

    args.state.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"saved {args.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
