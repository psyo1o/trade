# -*- coding: utf-8 -*-
"""
콘솔 + 파일 이중 로깅 — ``print`` / ``stderr`` 를 퀀트 전용 로거로 우회.

- ``setup_quant_logging()`` : ``logs/bot.log`` 에 **자정마다 롤오버**, 30일 보관.
- ``StreamToLogger`` : 터미널에는 그대로 쓰되, cp949 콘솔에서는 이모지 등으로
  ``UnicodeEncodeError`` 가 나지 않도록 ``_safe_write_terminal`` 로 한 번 감싼다.
- 파일 핸들러는 UTF-8 이므로 한글·이모지가 그대로 남는다.
"""
import logging
import sys
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

_quant_logger = None


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
        # 1. 터미널(콘솔)에는 그대로 출력
        if self.terminal:
            _safe_write_terminal(self.terminal, buf)
            self.terminal.flush()

        # 2. 파일에는 빈 줄(\n) 무시하고 알맹이만 기록
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
    ``QuantBot`` 로거에 ``TimedRotatingFileHandler`` 를 붙이고 ``sys.stdout``/``stderr`` 를 교체한다.

    프로세스당 **한 번** 호출하는 것을 권장한다. 중복 핸들러는 기존 것을 비운 뒤 다시 붙인다.
    """
    global _quant_logger

    LOG_DIR = Path("logs")
    LOG_DIR.mkdir(exist_ok=True)

    # 1. 퀀트 전용 로거 세팅
    quant_logger = logging.getLogger("QuantBot")
    quant_logger.setLevel(logging.INFO)
    quant_logger.propagate = False  # 중복 출력 방지

    # 기존 핸들러 싹 비우기 (중복 방지)
    if quant_logger.hasHandlers():
        quant_logger.handlers.clear()

    # 2. 자정(Midnight) 자동 롤오버 핸들러 장착
    log_handler = TimedRotatingFileHandler(
        filename=LOG_DIR / "bot.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding='utf-8'
    )

    log_handler.setFormatter(logging.Formatter('%(message)s'))
    quant_logger.addHandler(log_handler)

    # 3. 봇의 모든 print()와 에러 메시지를 로거로 멱살 잡고 끌고 옴
    sys.stdout = StreamToLogger(quant_logger, logging.INFO)
    sys.stderr = StreamToLogger(quant_logger, logging.ERROR)

    _quant_logger = quant_logger

    print(f"\n{'='*60}")
    print(f"🤖 기관급 로깅 시작: 매일 자정 롤오버 (30일 보관) 장착 완료")
    print(f"{'='*60}\n")

    return quant_logger
