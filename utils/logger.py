# -*- coding: utf-8 -*-
"""
콘솔 + 파일 이중 로깅 — ``print`` / ``stderr`` 를 퀀트 전용 로거로 우회.

- ``setup_quant_logging()`` : ``logs/{YYYY}년/{M}월/bot.log`` 에 **자정마다 롤오버**.
  보관 **무제한**(삭제 없음). 일자 파일은 같은 월 폴더의 ``bot.YYYY-MM-DD.log``.
- ``StreamToLogger`` : 터미널에는 그대로 쓰되, cp949 콘솔에서는 이모지 등으로
  ``UnicodeEncodeError`` 가 나지 않도록 ``_safe_write_terminal`` 로 한 번 감싼다.
- 파일 핸들러는 UTF-8 이므로 한글·이모지가 그대로 남는다.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

_quant_logger = None
_LOG_ROOT = Path("logs")
_SEOUL = ZoneInfo("Asia/Seoul")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _seoul_now() -> datetime:
    return datetime.now(_SEOUL)


def _log_month_dir(dt: datetime | None = None) -> Path:
    """``logs/2026년/9월`` — 월은 0 패딩 없음."""
    z = dt or _seoul_now()
    if z.tzinfo is None:
        z = z.replace(tzinfo=_SEOUL)
    else:
        z = z.astimezone(_SEOUL)
    d = _LOG_ROOT / f"{z.year}년" / f"{z.month}월"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _month_dir_for_ymd(year: int, month: int) -> Path:
    d = _LOG_ROOT / f"{int(year)}년" / f"{int(month)}월"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _daily_log_namer(default_name: str) -> str:
    """
    TimedRotatingFileHandler 기본 이름(`…/bot.log.YYYY-MM-DD`)을
    ``logs/{Y}년/{M}월/bot.YYYY-MM-DD.log`` 로 바꾼다.
    """
    marker = ".log."
    if marker in default_name:
        date_str = default_name.rsplit(marker, 1)[-1].strip()
        m = _DATE_RE.search(date_str)
        if m:
            y, mo, _d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dest = _month_dir_for_ymd(y, mo) / f"bot.{m.group(0)}.log"
            return str(dest)
        head, tail = default_name.split(marker, 1)
        return f"{head}.{tail}.log"
    return default_name if default_name.endswith(".log") else f"{default_name}.log"


def _migrate_flat_daily_logs() -> None:
    """예전의 ``logs/bot.YYYY-MM-DD.log`` 를 연·월 폴더로 이동(삭제 없음)."""
    if not _LOG_ROOT.is_dir():
        return
    patterns = ("bot.*.log", "bot.log.*")
    seen: set[Path] = set()
    for pat in patterns:
        for p in _LOG_ROOT.glob(pat):
            if not p.is_file() or p in seen:
                continue
            # 이미 월 폴더 아래면 스킵
            if p.parent != _LOG_ROOT:
                continue
            m = _DATE_RE.search(p.name)
            if not m:
                continue
            dest_dir = _month_dir_for_ymd(int(m.group(1)), int(m.group(2)))
            dest = dest_dir / f"bot.{m.group(0)}.log"
            if dest.resolve() == p.resolve():
                continue
            if dest.exists():
                # 충돌 시 원본 유지
                continue
            try:
                p.replace(dest)
                seen.add(p)
            except OSError:
                pass


class MonthFolderTimedRotatingFileHandler(TimedRotatingFileHandler):
    """자정 롤오버 후 현재 ``bot.log`` 경로를 이번 달 폴더로 맞춘다."""

    def doRollover(self) -> None:
        super().doRollover()
        self._ensure_current_month_path()

    def _ensure_current_month_path(self) -> None:
        target = os.path.abspath(str(_log_month_dir() / "bot.log"))
        current = os.path.abspath(str(self.baseFilename))
        if current == target:
            return
        if self.stream:
            self.stream.close()
            self.stream = None
        # 빈 파일이 옛 월 폴더에 생겼으면 정리 시도
        try:
            if os.path.isfile(current) and os.path.getsize(current) == 0:
                os.remove(current)
        except OSError:
            pass
        self.baseFilename = target
        if not self.delay:
            self.stream = self._open()


def _safe_write_terminal(stream, buf: str) -> None:
    """Windows cp949 콘솔 등에서 이모지 출력 시 UnicodeEncodeError 방지."""
    if not stream:
        return
    try:
        stream.write(buf)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "utf-8"
        stream.write(buf.encode(enc, errors="replace").decode(enc, errors="replace"))


class StreamToLogger:
    """
    ``sys.stdout`` / ``stderr`` 대체용 파일류 객체.

    ``write`` 시 (1) 원래 터미널 (2) ``logging`` 한 줄 로그 — 두 곳에 동시 기록.
    """

    def __init__(self, logger, log_level=logging.INFO):
        self.logger = logger
        self.log_level = log_level
        self.terminal = sys.__stdout__ if sys.__stdout__ else sys.stdout

    def write(self, buf):
        if self.terminal:
            _safe_write_terminal(self.terminal, buf)
            self.terminal.flush()

        for line in buf.rstrip().splitlines():
            if line.strip():
                self.logger.log(self.log_level, line.rstrip())

    def flush(self):
        if self.terminal:
            self.terminal.flush()


def get_quant_logger():
    return _quant_logger


def setup_quant_logging():
    """
    ``QuantBot`` 로거에 월 폴더 ``TimedRotatingFileHandler`` 를 붙이고
    ``sys.stdout``/``stderr`` 를 교체한다.

    프로세스당 **한 번** 호출하는 것을 권장한다. 중복 핸들러는 기존 것을 비운 뒤 다시 붙인다.
    """
    global _quant_logger

    _LOG_ROOT.mkdir(exist_ok=True)
    _migrate_flat_daily_logs()
    log_dir = _log_month_dir()

    quant_logger = logging.getLogger("QuantBot")
    quant_logger.setLevel(logging.INFO)
    quant_logger.propagate = False

    if quant_logger.hasHandlers():
        quant_logger.handlers.clear()

    # backupCount=0 → 오래된 일자 로그를 삭제하지 않음
    log_handler = MonthFolderTimedRotatingFileHandler(
        filename=str(log_dir / "bot.log"),
        when="midnight",
        interval=1,
        backupCount=0,
        encoding="utf-8",
    )
    log_handler.namer = _daily_log_namer
    log_handler.setFormatter(logging.Formatter("%(message)s"))
    quant_logger.addHandler(log_handler)

    sys.stdout = StreamToLogger(quant_logger, logging.INFO)
    sys.stderr = StreamToLogger(quant_logger, logging.ERROR)

    _quant_logger = quant_logger

    print(f"\n{'=' * 60}")
    print(
        f"🤖 기관급 로깅 시작: 매일 자정 롤오버 · "
        f"{log_dir.as_posix()}/ (보관 무제한) 장착 완료"
    )
    print(f"{'=' * 60}\n")

    return quant_logger
