# -*- coding: utf-8 -*-
"""market_cycles 본문 — run_bot 모듈 네임스페이스 심볼에 rb. 접두."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_BOT = ROOT / "run_bot.py"
CYCLES = ROOT / "execution" / "market_cycles"

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
}


def _run_bot_module_names() -> set[str]:
    tree = ast.parse(RUN_BOT.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("__"):
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


def prefix_body(body: str, rb_names: set[str]) -> str:
    for name in sorted(rb_names, key=len, reverse=True):
        if name in SKIP or name.startswith("__"):
            continue
        body = re.sub(rf"(?<![.\w]){re.escape(name)}(?=\s*[\(\[\.])", f"rb.{name}", body)
        body = re.sub(rf"(?<![.\w]){re.escape(name)}(?=\s*=)", f"rb.{name}", body)
        body = re.sub(
            rf",\s*{re.escape(name)}(\s*[,)])",
            rf", rb.{name}\1",
            body,
        )
    return body


def main() -> None:
    rb_names = _run_bot_module_names()
    for path in (CYCLES / "kr_cycle.py", CYCLES / "us_cycle.py", CYCLES / "coin_cycle.py"):
        text = path.read_text(encoding="utf-8")
        split_at = text.find("\n    if is_market_open")
        if split_at < 0:
            split_at = text.find("\n    if ")
        head = text[: split_at + 1]
        body = text[split_at + 1 :]
        body = prefix_body(body, rb_names)
        path.write_text(head + body, encoding="utf-8")
        print(path.name, "ok")


if __name__ == "__main__":
    main()
