# -*- coding: utf-8 -*-
"""
``run_bot.bat`` 대체 — GUI 자동 재시작.

권장 (미니 PC, CMD 없음 + 재시작):
    pythonw launch_gui.py --no-console
    start_gui.vbs

CMD 없음, 재시작 없음 (창 닫으면 끝):
    pythonw launch_gui.py --no-console --once
    start_gui_once.vbs

콘솔에서 보기:
    py -3.11 launch_gui.py
    py -3.11 launch_gui.py --once
    py -3.11 launch_gui.py --no-net-watch
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GUI_SCRIPT = ROOT / "run_gui.py"
LOG_PATH = ROOT / "logs" / "launcher.log"
RESTART_SEC = 10

if sys.platform == "win32":
    _CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
else:
    _CREATE_NO_WINDOW = 0
    _DETACHED_PROCESS = 0


def _clear_pycache() -> None:
    for rel in ("__pycache__", Path("strategy") / "__pycache__"):
        p = ROOT / rel
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)


def _resolve_pythonw() -> Path | None:
    """현재 인터프리터와 짝인 pythonw.exe."""
    try:
        exe = Path(sys.executable).resolve()
    except Exception:
        return None
    if exe.name.lower() == "pythonw.exe":
        return exe
    pw = exe.with_name("pythonw.exe")
    return pw if pw.is_file() else None


def _gui_python_argv(no_console: bool) -> list[str]:
    """GUI 프로세스 실행 argv (``-B -W ignore``)."""
    if no_console:
        pw = _resolve_pythonw()
        if pw is not None:
            return [str(pw), "-B", "-W", "ignore"]
    if sys.platform == "win32" and shutil.which("py"):
        return ["py", "-3.11", "-B", "-W", "ignore"]
    return [sys.executable, "-B", "-W", "ignore"]


def _log(msg: str, *, echo: bool) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if echo:
        print(line)


def _reexec_hidden_launcher(argv_tail: list[str]) -> bool:
    """``py python.exe`` 로 --no-console 실행 시 pythonw 로 분리 재실행 (CMD 닫아도 유지)."""
    if sys.platform != "win32":
        return False
    if Path(sys.executable).name.lower() == "pythonw.exe":
        return False
    pw = _resolve_pythonw()
    if pw is None:
        return False
    script = str(Path(__file__).resolve())
    cmd = [str(pw), "-B", script, *argv_tail]
    flags = _CREATE_NO_WINDOW | _DETACHED_PROCESS
    subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        creationflags=flags,
        close_fds=True,
    )
    return True


def _run_once(extra_env: dict[str, str], *, no_console: bool) -> int:
    env = os.environ.copy()
    env.setdefault("QT_LOGGING_RULES", "*.debug=false;*.warning=false")
    env.update(extra_env)
    cmd = [*_gui_python_argv(no_console), str(GUI_SCRIPT)]
    kwargs: dict = {"cwd": str(ROOT), "env": env}
    if no_console and sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return int(subprocess.call(cmd, **kwargs))


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot GUI launcher (run_bot.bat 대체)")
    parser.add_argument("--once", action="store_true", help="종료 시 재시작하지 않음")
    parser.add_argument(
        "--no-net-watch",
        action="store_true",
        help="네트워크 감시 자동 종료 끔 (BOT_DISABLE_NET_WATCH=1)",
    )
    parser.add_argument("--no-pycache-clear", action="store_true", help="__pycache__ 삭제 생략")
    parser.add_argument(
        "--no-console",
        action="store_true",
        help="CMD 없음, GUI만 표시, 재시작 로그는 logs/launcher.log",
    )
    args = parser.parse_args()

    if args.no_console and _reexec_hidden_launcher(sys.argv[1:]):
        return 0

    os.chdir(ROOT)
    echo = not args.no_console
    extra_env: dict[str, str] = {}
    if args.no_net_watch:
        extra_env["BOT_DISABLE_NET_WATCH"] = "1"

    while True:
        if not args.no_pycache_clear:
            _clear_pycache()

        _log("Starting run_gui.py ...", echo=echo)

        code = _run_once(extra_env, no_console=args.no_console)

        if args.once:
            _log(f"GUI exited (code {code}), --once", echo=echo)
            return code

        _log(f"GUI stopped (exit {code}); restart in {RESTART_SEC}s", echo=echo)
        try:
            time.sleep(RESTART_SEC)
        except KeyboardInterrupt:
            _log("Restart loop interrupted", echo=echo)
            return 0


if __name__ == "__main__":
    if not GUI_SCRIPT.is_file():
        print(f"run_gui.py not found: {GUI_SCRIPT}", file=sys.stderr)
        sys.exit(1)
    raise SystemExit(main())
