# -*- coding: utf-8 -*-
"""git HEAD run_bot 본문 vs 추출 모듈 — rb. 정규화 후 동작 동일성 스캔."""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def fn_body(text: str, fn_name: str) -> str:
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            seg = ast.get_source_segment(text, node) or ""
            m = re.search(r":\n(.*)", seg, re.S)
            return m.group(1) if m else seg
    return ""


def normalize(body: str) -> str:
    body = re.sub(r"^\s*rb = _rb\(\)\n", "", body, flags=re.M)
    body = re.sub(r"\brb\.", "", body)
    lines = []
    for line in body.splitlines():
        s = line.rstrip()
        if not s or s.lstrip().startswith("#"):
            continue
        lines.append(s)
    return "\n".join(lines)


def compare(orig_fn: str, new_path: Path, new_fn: str, orig_text: str) -> bool:
    o = normalize(fn_body(orig_text, orig_fn))
    n = normalize(fn_body(new_path.read_text(encoding="utf-8"), new_fn))
    ok = o == n
    tag = "OK" if ok else "DIFF"
    print(f"  {orig_fn} -> {new_fn}: {tag} ({len(o.splitlines())} / {len(n.splitlines())} lines)")
    if not ok:
        ol, nl = o.splitlines(), n.splitlines()
        for i, (a, b) in enumerate(zip(ol, nl)):
            if a != b:
                print(f"    first diff @ {i + 1}:")
                print(f"      ORIG: {a[:110]}")
                print(f"      NEW : {b[:110]}")
                break
        if len(ol) != len(nl):
            print(f"    line count: orig={len(ol)} new={len(nl)}")
    return ok


def main() -> int:
    orig_rb = subprocess.check_output(
        ["git", "show", "HEAD:run_bot.py"], cwd=ROOT, text=True, encoding="utf-8"
    )
    all_ok = True
    print("order_executor (git HEAD run_bot vs execution/order_executor.py):")
    oe = ROOT / "execution" / "order_executor.py"
    for orig_fn, new_fn in [
        ("_twap_krw_budget_slices", "twap_krw_budget_slices"),
        ("_twap_usd_budget_slices", "twap_usd_budget_slices"),
        ("_execute_kr_market_buy_twap", "execute_kr_market_buy_twap"),
        ("_execute_us_market_buy_twap", "execute_us_market_buy_twap"),
        ("_execute_coin_market_buy_twap", "execute_coin_market_buy_twap"),
    ]:
        if not compare(orig_fn, oe, new_fn, orig_rb):
            all_ok = False

    print("\nkr_buy_cycle: inline block vs extracted")
    kr_buy = ROOT / "execution" / "market_cycles" / "kr_buy_cycle.py"
    kr_cycle_head = subprocess.check_output(
        ["git", "show", "HEAD:execution/market_cycles/kr_cycle.py"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    )
    # HEAD kr_cycle had inline buy; current calls run_kr_buy_cycle
    if "run_kr_buy_cycle(" in kr_cycle_head:
        print("  HEAD kr_cycle already delegated — skip inline compare")
    else:
        marker = "국장 사냥감"
        if marker in kr_cycle_head:
            print("  HEAD had inline buy in kr_cycle (manual check recommended)")
        else:
            print("  marker not in HEAD kr_cycle")

    # Wrapper passthrough: run_bot still exposes same symbols
    import run_bot as rb

    checks = [
        ("_execute_kr_market_buy_twap", rb._execute_kr_market_buy_twap),
        ("_run_kr_buy_cycle", rb._run_kr_buy_cycle),
        ("_twap_krw_budget_slices", rb._twap_krw_budget_slices),
    ]
    print("\nrun_bot public wrappers exist:")
    for name, obj in checks:
        print(f"  {name}: {callable(obj)}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
