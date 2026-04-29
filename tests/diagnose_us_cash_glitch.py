from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_bot
from api.kis_parsers import parse_us_cash_fallback


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(d)


def sample_once() -> dict:
    us_cash_raw = _f(run_bot.get_us_cash_real(run_bot.kis_api.broker_us), 0.0)
    us_bal = run_bot.ensure_dict(run_bot.get_us_positions_with_retry())
    out2 = run_bot.safe_get(us_bal, "output2", {})
    output1 = run_bot.ensure_list(us_bal.get("output1", []))

    us_cash_fb = _f(parse_us_cash_fallback(out2, run_bot._to_float), 0.0)
    us_cash_recovered = _f(run_bot._recover_us_cash_from_output2_if_needed(us_cash_raw, out2), 0.0)
    us_stock_value = _f(run_bot._compute_us_stock_value_from_output(us_bal, out2), 0.0)
    us_total_recovered = us_cash_recovered + us_stock_value

    snap = run_bot.load_last_kis_display_snapshot()
    us_snap = snap.get("us") if isinstance(snap.get("us"), dict) else {}
    us_snap_cash = _f(us_snap.get("cash", 0.0), 0.0)
    us_snap_total = _f(us_snap.get("total", 0.0), 0.0)

    us_label_cash = 0.0
    us_label_total = 0.0
    try:
        labels = run_bot.build_account_snapshot_for_report().get("labels", {})
        us_label = labels.get("us") if isinstance(labels.get("us"), dict) else {}
        us_label_cash = _f(us_label.get("cash", 0.0), 0.0)
        us_label_total = _f(us_label.get("total", 0.0), 0.0)
    except Exception:
        # 진단 목적상 GUI 라벨 경로가 막혀도(raw/fallback 비교) 계속 진행
        pass

    return {
        "raw_cash": us_cash_raw,
        "fb_cash": us_cash_fb,
        "recovered_cash": us_cash_recovered,
        "stock_value": us_stock_value,
        "recovered_total": us_total_recovered,
        "snapshot_cash": us_snap_cash,
        "snapshot_total": us_snap_total,
        "label_cash": us_label_cash,
        "label_total": us_label_total,
        "rows": len(output1),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="US 예수금 튐(예: 297 -> 77) 진단")
    p.add_argument("--count", type=int, default=6, help="샘플 횟수")
    p.add_argument("--interval", type=float, default=2.0, help="샘플 간격(초)")
    args = p.parse_args()

    print("=== US cash glitch diagnostic ===")
    try:
        run_bot.refresh_brokers_if_needed(force=True)
        print("broker_init=ok")
    except Exception as e:
        print(f"broker_init=fail ({type(e).__name__}: {e})")
    print(f"time={datetime.now().isoformat(timespec='seconds')} count={args.count} interval={args.interval}s")
    print("-" * 110)
    print(
        "idx | raw_cash | fb_cash | recovered_cash | stock_value | recovered_total | "
        "snapshot(cash/total) | label(cash/total) | rows | note"
    )
    print("-" * 110)

    for i in range(1, args.count + 1):
        d = sample_once()
        if d["rows"] == 0 and i == 1:
            print("warn: US output1 rows=0 (잔고조회 실패/빈계좌/토큰 이슈 가능)")
        note = []
        if d["rows"] > 0 and d["raw_cash"] > 0 and d["recovered_cash"] > d["raw_cash"] * 1.8:
            note.append("raw_cash_low?")
        if d["rows"] > 0 and d["snapshot_cash"] > 0 and d["raw_cash"] < d["snapshot_cash"] * 0.6:
            note.append("raw<<snapshot")
        if d["rows"] > 0 and d["label_cash"] > 0 and d["raw_cash"] < d["label_cash"] * 0.6:
            note.append("raw<<label")
        note_txt = ",".join(note) if note else "-"
        print(
            f"{i:>3} | "
            f"${d['raw_cash']:>8.2f} | ${d['fb_cash']:>8.2f} | ${d['recovered_cash']:>13.2f} | "
            f"${d['stock_value']:>10.2f} | ${d['recovered_total']:>14.2f} | "
            f"${d['snapshot_cash']:.2f}/${d['snapshot_total']:.2f} | "
            f"${d['label_cash']:.2f}/${d['label_total']:.2f} | "
            f"{d['rows']:>4} | {note_txt}"
        )
        if i < args.count:
            time.sleep(max(0.1, args.interval))

    print("-" * 110)
    print("해석: note가 반복되면 KIS raw 현금값이 간헐적으로 낮게 내려오는 케이스로 판단.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

