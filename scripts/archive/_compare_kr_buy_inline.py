# -*- coding: utf-8 -*-
"""HEAD kr_cycle 인라인 매수 vs kr_buy_cycle 본문 비교."""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def get_fn_body(text: str, name: str) -> str:
    tree = ast.parse(text)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            seg = ast.get_source_segment(text, n) or ""
            return seg.split(":", 1)[1]
    return ""


def norm(s: str, dedent: int = 0) -> str:
    s = re.sub(r"^\s*rb = _rb\(\)\s*\n", "", s, flags=re.M)
    s = re.sub(r"\brb\.", "", s)
    s = s.replace("ctx.final_targets_kr", "FINAL_TARGETS_KR")
    s = s.replace("final_targets_kr", "FINAL_TARGETS_KR")
    s = s.replace("ctx.buy_fills", "BUY_FILLS")
    lines = []
    for line in s.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if dedent and line.startswith(" " * dedent):
            line = line[dedent:]
        lines.append(line.rstrip())
    return "\n".join(lines)


def main() -> int:
    head_kr = subprocess.check_output(
        ["git", "show", "HEAD:execution/market_cycles/kr_cycle.py"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    )
    cur_buy = (ROOT / "execution/market_cycles/kr_buy_cycle.py").read_text(encoding="utf-8")
    cur = norm(get_fn_body(cur_buy, "run_kr_buy_cycle"))

    pat = r"ctx\.buy_zone_kr = True\n(.*?)\n    else:\n        rb\._log_kr_market_closed"
    m = re.search(pat, head_kr, re.S)
    if not m:
        print("HEAD inline buy block not found")
        return 1

    inline = norm(m.group(1), dedent=16)

    key_ops = [
        "_phase4_hedge_only_active",
        "_apply_phase4_hedge_buy_targets",
        "_merge_hedge_into_buy_targets",
        "get_market_index_change",
        "_execute_kr_market_buy_twap",
        "decide_entry_signals",
        "_ai_false_breakout_buy_gate",
        "_register_swing_risk_after_buy",
        "MAX_POSITIONS_KR",
        "_can_open_new_respecting_hedge_bypass",
        "_log_portfolio_heat_block",
    ]
    print("KR buy — key calls (HEAD inline vs kr_buy_cycle):")
    ok = True
    for k in key_ops:
        h, c = k in inline, k in cur
        if h != c:
            ok = False
        print(f"  {k}: HEAD={h} CUR={c} {'OK' if h == c else 'MISMATCH'}")

    anchor = "hedge_only = _phase4_hedge_only_active"
    ic, cc = inline.find(anchor), cur.find(anchor)
    if ic >= 0 and cc >= 0:
        sub_i, sub_c = inline[ic:], cur[cc:]
        if sub_i == sub_c:
            print("ANCHOR: hedge_only ~ end — IDENTICAL")
        else:
            ok = False
            print("ANCHOR: hedge_only ~ end — DIFF")
            for i, (a, b) in enumerate(zip(sub_i.splitlines(), sub_c.splitlines())):
                if a != b:
                    print(f"  +{i}: H: {a[:100]}")
                    print(f"  +{i}: C: {b[:100]}")
                    if i >= 5:
                        break
            if len(sub_i.splitlines()) != len(sub_c.splitlines()):
                print(f"  line count {len(sub_i.splitlines())} vs {len(sub_c.splitlines())}")
    else:
        print(f"anchor missing ic={ic} cc={cc}")
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
