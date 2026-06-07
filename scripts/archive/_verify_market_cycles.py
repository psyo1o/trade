# -*- coding: utf-8 -*-
"""market_cycles — run_bot 네임스페이스 대비 미정의 이름 스캔."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_BOT = ROOT / "run_bot.py"
CYCLES = [
    ROOT / "execution" / "market_cycles" / "kr_cycle.py",
    ROOT / "execution" / "market_cycles" / "us_cycle.py",
    ROOT / "execution" / "market_cycles" / "coin_cycle.py",
]

SKIP = {
    "ctx",
    "rb",
    "state",
    "weather",
    "macro_mult",
    "macro_reason",
    "macro_snap",
    "_buy_cycle_tag",
    "final_targets",
    "_alpha_target_vol",
    "True",
    "False",
    "None",
    "print",
    "int",
    "float",
    "str",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "len",
    "range",
    "min",
    "max",
    "sum",
    "abs",
    "round",
    "open",
    "type",
    "isinstance",
    "enumerate",
    "zip",
    "any",
    "all",
    "sorted",
    "reversed",
    "Exception",
    "ValueError",
    "KeyError",
    "json",
    "time",
    "datetime",
    "timedelta",
    "traceback",
    "pd",
    "pytz",
    "Exception",
}


def _run_bot_names() -> set[str]:
    tree = ast.parse(RUN_BOT.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _local_defs(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.For):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in node.generators:
                if isinstance(gen.target, ast.Name):
                    names.add(gen.target.id)
    return names


def _load_names(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.add(node.id)
    return out


def check_file(path: Path, rb_names: set[str]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    locals_ = _local_defs(tree) | SKIP
    # common loop / closure locals in cycles
    for m in re.finditer(r"^\s+(?:for|if)\s+(\w+)", text, re.M):
        locals_.add(m.group(1))
    issues = []
    for name in sorted(_load_names(tree)):
        if name in locals_ or name.startswith("_") and name in locals_:
            continue
        if name in SKIP:
            continue
        if name in rb_names and f"rb.{name}" not in text and name not in text.split("def run_")[0]:
            # used bare but exists only on run_bot
            if re.search(rf"(?<![.\w]){re.escape(name)}(?!\s*\.)", text):
                issues.append(name)
    return sorted(set(issues))


def main() -> int:
    rb_names = _run_bot_names()
    any_issue = False
    for path in CYCLES:
        issues = check_file(path, rb_names)
        if issues:
            any_issue = True
            print(f"{path.name}: possible bare run_bot refs: {', '.join(issues[:30])}")
            if len(issues) > 30:
                print(f"  ... +{len(issues) - 30} more")
        else:
            print(f"{path.name}: OK (no obvious bare run_bot names)")
    return 1 if any_issue else 0


if __name__ == "__main__":
    sys.exit(main())
