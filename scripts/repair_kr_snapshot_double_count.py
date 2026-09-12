# -*- coding: utf-8 -*-
"""
KR 스냅샷 이중합산 복구 — 예수=현금+보유로 부푼 snap/aux/peak 교정.

사용:
  py -3.11 scripts/repair_kr_snapshot_double_count.py --dry-run
  py -3.11 scripts/repair_kr_snapshot_double_count.py
  py -3.11 scripts/repair_kr_snapshot_double_count.py --cash 436653
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


def _load(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise SystemExit(f"empty state: {path}")
    return json.loads(raw)


def repair(
    state: dict,
    *,
    cash_override: float | None = None,
    sync_peak_to_risk: bool = True,
) -> list[str]:
    from services import ledger_valuation as lv

    lines: list[str] = []
    snap = state.get("last_kis_display_snapshot")
    if not isinstance(snap, dict):
        snap = {}
    kr = snap.get("kr") if isinstance(snap.get("kr"), dict) else {}
    old_cash = float(kr.get("cash", 0) or 0)
    old_total = float(kr.get("total", 0) or 0)
    stock = float(lv.ledger_holdings_value_native(state, "KR") or 0)

    if cash_override is not None and cash_override > 0:
        new_cash = float(cash_override)
    else:
        # 이중합산 추정: cash ≈ true_cash + stock → true_cash = cash - stock
        if stock > 0 and old_cash > stock and abs(old_total - (old_cash + stock)) <= max(
            25_000.0, stock * 0.08
        ):
            new_cash = old_cash - stock
            lines.append(
                f"detected double-count: cash {old_cash:,.0f} − stock {stock:,.0f}"
            )
        else:
            # 폴백: 직전 정상으로 알려진 마감 전 예수 (수동 --cash 권장)
            new_cash = old_cash
            lines.append("no auto double-count signature — cash unchanged (pass --cash)")

    if new_cash < 0:
        new_cash = 0.0
    new_total = new_cash + stock
    kr = dict(kr)
    kr["cash"] = int(new_cash)
    kr["total"] = int(new_total)
    snap["kr"] = kr
    snap["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_kis_display_snapshot"] = snap
    lines.append(
        f"snap KR cash {old_cash:,.0f}→{new_cash:,.0f} total {old_total:,.0f}→{new_total:,.0f}"
    )

    risk = float(lv.market_equity_for_risk(state, "KR") or new_total)
    # last_loop를 먼저 정상화해야 risk 2차 방어가 오염값을 붙잡지 않음
    last = state.get("phase5_last_loop_equity_by_market")
    if not isinstance(last, dict):
        last = {}
    old_loop = float(last.get("KR", 0) or 0)
    last["KR"] = float(new_total)
    state["phase5_last_loop_equity_by_market"] = last
    lines.append(f"phase5_last_loop KR {old_loop:,.0f}→{new_total:,.0f}")

    risk = float(lv.market_equity_for_risk(state, "KR") or new_total)
    old_aux = float(state.get("circuit_aux_last_kr_krw", 0) or 0)
    state["circuit_aux_last_kr_krw"] = float(risk)
    lines.append(f"circuit_aux_last_kr_krw {old_aux:,.0f}→{risk:,.0f}")

    if sync_peak_to_risk:
        old_peak = float(state.get("peak_equity_KR", 0) or 0)
        state["peak_equity_KR"] = float(risk)
        lines.append(f"peak_equity_KR {old_peak:,.0f}→{risk:,.0f}")

    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="KR 스냅샷 이중합산 복구")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cash", type=float, default=None, help="정상 예수(원) 수동 지정")
    ap.add_argument("--keep-peak", action="store_true", help="peak_equity_KR 유지")
    ap.add_argument("--state", type=Path, default=STATE_PATH)
    args = ap.parse_args()

    state = _load(args.state)
    lines = repair(
        state,
        cash_override=args.cash,
        sync_peak_to_risk=not args.keep_peak,
    )
    for ln in lines:
        print(f"  {ln}")
    if args.dry_run:
        print("dry-run - not saved")
        return 0
    bak = args.state.with_suffix(
        args.state.suffix + f".bak_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy2(args.state, bak)
    print(f"backup → {bak}")
    args.state.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"saved → {args.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
