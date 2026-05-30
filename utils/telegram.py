# -*- coding: utf-8 -*-
"""
텔레그램 Bot API 래퍼 + 프로세스 종료 훅.

흐름
    1. ``run_bot`` 기동 시 ``configure_telegram(config)`` 로 토큰·chat_id 주입.
    2. ``register_telegram_atexit()`` 로 ``atexit`` 에 ``shutdown_handler`` 등록.
    3. 비정상 종료(스택이 남는 경우) 시 짧은 요약을 ``send_telegram`` 으로 밀어 넣는다.

메시지 본문은 **기본적으로 일반 텍스트**로 보냅니다. 예전처럼 ``Markdown`` 을 쓰면
종목명·사유 등에 ``_`` · ``*`` · ``[`` 가 섞일 때 Telegram이
``can't parse entities`` 로 전체 전송을 거부하는 경우가 많아 기본값에서 제외했습니다.
"""
import atexit
import logging
import re
import threading
import time
import traceback
from datetime import datetime

import requests

_config = None
_bot_source_label = "run_bot.py"
_alert_handler_attached = False


def configure_telegram(config: dict, bot_source_label: str = "run_bot.py"):
    global _config, _bot_source_label
    _config = config
    _bot_source_label = bot_source_label


def send_telegram(message: str) -> bool:
    """Telegram Bot API 로 전송. 성공 시 ``True``.

    - 본문은 **JSON POST** 로 보냄(URL 쿼리로 긴 생존신고를 실을 때 생기는 불안정 완화).
    - 연결·읽기 타임아웃·일시 장애는 지수 백오프 재시도.
    - ``parse_mode`` 미사용: 보유 종목 줄·사유 등에 특수문자가 섞여도 전송 실패하지 않음.
    """
    if not _config:
        print("⚠️ 텔레그램: config 미설정 (configure_telegram 호출 필요)")
        return False

    api_url = f"https://api.telegram.org/bot{_config['telegram_token']}/sendMessage"
    payload = {
        "chat_id": _config["telegram_chat_id"],
        "text": message,
    }

    connect_timeout = 30
    read_timeout = 60
    max_attempts = 5
    if isinstance(_config, dict):
        try:
            connect_timeout = max(5, int(_config.get("telegram_connect_timeout_sec", connect_timeout)))
        except Exception:
            pass
        try:
            read_timeout = max(10, int(_config.get("telegram_read_timeout_sec", read_timeout)))
        except Exception:
            pass
        try:
            max_attempts = max(1, int(_config.get("telegram_max_retries", max_attempts)))
        except Exception:
            pass
    last_err: Exception | None = None

    for attempt in range(max_attempts):
        try:
            resp = requests.post(api_url, json=payload, timeout=(connect_timeout, read_timeout))
            if resp.status_code == 429:
                try:
                    ra = int(resp.json().get("parameters", {}).get("retry_after", 3))
                except Exception:
                    ra = 3
                print(f"⚠️ 텔레그램 rate limit — {min(ra, 60)}초 후 재시도 ({attempt + 1}/{max_attempts})")
                time.sleep(min(ra, 60))
                continue

            try:
                data = resp.json()
            except Exception:
                last_err = RuntimeError(f"HTTP {resp.status_code}, 본문 JSON 아님: {resp.text[:200]}")
                time.sleep(min(2**attempt, 30))
                continue

            if resp.status_code == 200 and data.get("ok"):
                return True

            desc = data.get("description", resp.text[:300])
            last_err = RuntimeError(f"Telegram API: {desc}")
            # 잘못된 요청은 재시도해도 동일할 수 있으나 일시 오류 구분이 어려워 1~2회만 백오프
            time.sleep(min(2**attempt, 20))

        except requests.RequestException as e:
            last_err = e
            wait = min(2**attempt, 30)
            print(
                f"⚠️ 텔레그램 전송 재시도 ({attempt + 1}/{max_attempts}) … "
                f"{type(e).__name__}: {e} (timeout={connect_timeout}/{read_timeout}s)"
            )
            time.sleep(wait)

    print(f"⚠️ 텔레그램 전송 실패(최종): {last_err}")
    return False


def shutdown_handler():
    """프로그램 비정상 종료 시 에러 보고"""
    from utils.logger import get_quant_logger

    # 로그 파일 정상 종료
    if get_quant_logger():
        try:
            print(f"\n{'='*60}")
            print(f"🛑 로깅 종료: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*60}\n")
        except Exception as e:
            print(f"⚠️ 로그 파일 종료 실패: {e}")

    err = traceback.format_exc()
    if "SystemExit" not in err and "KeyboardInterrupt" not in err and "NoneType: None" not in err and err.strip() != 'None':
        send_telegram(f"🚨 [긴급] `{_bot_source_label}` 봇 에러 발생:\n```{repr(err[-250:])}```")


def register_telegram_atexit():
    atexit.register(shutdown_handler)


class _TelegramAlertHandler(logging.Handler):
    """``QuantBot`` 로거에서 예외/경고 라인을 모아 텔레그램으로 푸시.

    - **대상 라인** (다음 중 하나라도 매칭되면 캡처):
      1. ``WARNING`` 이상 레벨 (헤드리스 모드의 stderr 트레이스백 등)
      2. 본문에 ``⚠️`` / ``🚨`` / ``🛑`` 이 들어간 라인
      3. 파이썬 트레이스백 헤더 (``Traceback (most recent call last):``)
      4. 파이썬 예외 마지막 줄 (``XxxError: ...`` / ``XxxException: ...``)
    - **GUI sticky 캡처**: 위 시그널이 한 번 잡히면 ``sticky_window_sec`` 동안 들어오는
      후속 라인도 모두 알림에 포함 — GUI(``RedirectText``)가 모든 라인을 ``INFO`` 로 흘려보내기 때문에
      트레이스백 중간의 ``  File "...", line N, in ...`` / 코드 라인이 토큰 없이 들어오는 걸 빠짐없이 잡기 위함.
    - **묶음 전송**: 첫 캡처 후 ``flush_interval_sec`` 동안 들어온 라인을 한 메시지로 합쳐서 전송.
    - **폭주 방지**: 분당 최대 ``max_per_minute`` 건, 같은 본문은 ``dedup_window_sec`` 동안 1회만.
    - **루프 차단**: 본문에 ``텔레그램`` / ``telegram`` 이 들어간 라인은 무시
      (``send_telegram`` 자체에서 찍는 재시도 경고가 다시 트리거되는 것을 방지).
    - **운영 로그 제외**: ``손실 구간`` 등 정기 보유 감시 문구는 GUI 로그에만 남기고 텔레그램에는 보내지 않음.
    """

    _ALERT_PREFIX_TOKENS = ("⚠️", "🚨", "🛑")
    _TRACEBACK_HEADER = "Traceback (most recent call last):"
    # ``ValueError: x`` / ``builtins.RuntimeError: y`` / ``KeyError`` 등
    _EXC_LINE_RE = re.compile(r"^\s*([A-Za-z_][\w\.]*)(Error|Exception|Warning)(:|\s|$)")
    # 매 사이클 보유 감시용 INFO성 로그 — ``⚠️`` 가 있어도 텔레그램 에러 알림에서 제외
    _ROUTINE_ALERT_SUPPRESS_SUBSTRINGS = (
        "손실 구간:",
        "손실 구간：",
    )

    def __init__(
        self,
        flush_interval_sec: float = 2.0,
        max_per_minute: int = 6,
        dedup_window_sec: float = 300.0,
        sticky_window_sec: float = 1.5,
        max_chars: int = 3500,
    ) -> None:
        super().__init__(logging.INFO)
        self._flush_interval = max(0.5, float(flush_interval_sec))
        self._max_per_minute = max(1, int(max_per_minute))
        self._dedup_window = max(30.0, float(dedup_window_sec))
        self._sticky_window = max(0.0, float(sticky_window_sec))
        self._max_chars = max(500, int(max_chars))

        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._timer: threading.Timer | None = None
        self._send_times: list[float] = []
        self._recent: dict[int, float] = {}
        self._sticky_until: float = 0.0

    def _suppress_routine_alert(self, msg: str) -> bool:
        return any(token in msg for token in self._ROUTINE_ALERT_SUPPRESS_SUBSTRINGS)

    def _is_alert(self, record: logging.LogRecord, msg: str) -> bool:
        now = time.time()
        is_signal = False
        if record.levelno >= logging.WARNING:
            is_signal = True
        elif any(tok in msg for tok in self._ALERT_PREFIX_TOKENS):
            is_signal = True
        else:
            stripped = msg.lstrip()
            if stripped.startswith(self._TRACEBACK_HEADER):
                is_signal = True
            elif self._EXC_LINE_RE.match(stripped):
                is_signal = True

        if is_signal:
            if self._sticky_window > 0:
                self._sticky_until = now + self._sticky_window
            return True
        # 시그널 라인 직후의 후속(인덴트된 ``File`` / 코드) 라인을 sticky 윈도우 동안 모두 흡수
        if now < self._sticky_until:
            return True
        return False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        if not msg or not msg.strip():
            return
        # 텔레그램 전송 자체에서 찍는 라인은 무시(루프 차단)
        low = msg.lower()
        if "텔레그램" in msg or "telegram" in low:
            return
        if self._suppress_routine_alert(msg):
            return
        try:
            from utils.yfinance_guard import is_yahoo_noise_line

            if is_yahoo_noise_line(msg):
                return
        except Exception:
            pass
        with self._lock:
            if not self._is_alert(record, msg):
                return
            self._buffer.append(msg.rstrip())
            if self._timer is None:
                self._timer = threading.Timer(self._flush_interval, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            buf = self._buffer
            self._buffer = []
            self._timer = None
        if not buf:
            return

        body = "\n".join(buf).rstrip()
        if len(body) > self._max_chars:
            body = body[-self._max_chars:]

        now = time.time()
        # 만료된 dedup 키 청소
        for k, t in list(self._recent.items()):
            if now - t > self._dedup_window:
                self._recent.pop(k, None)
        key = hash(body[:500])
        if key in self._recent:
            return
        self._recent[key] = now

        # 분당 송신량 제한
        self._send_times = [t for t in self._send_times if now - t < 60.0]
        if len(self._send_times) >= self._max_per_minute:
            return
        self._send_times.append(now)

        try:
            send_telegram(f"🚨 [{_bot_source_label} 에러/경고]\n{body}")
        except Exception:
            pass


def attach_telegram_error_alerts(
    flush_interval_sec: float = 2.0,
    max_per_minute: int = 6,
    dedup_window_sec: float = 300.0,
    sticky_window_sec: float = 1.5,
) -> bool:
    """``QuantBot`` 로거에 텔레그램 에러/경고 알림 핸들러를 부착(중복 부착 차단).

    config 의 ``telegram_error_alerts`` 가 ``false`` 면 부착을 건너뛴다.
    이미 한 번 부착한 프로세스에서는 다시 호출되어도 즉시 ``True`` 를 반환한다.
    """
    global _alert_handler_attached
    if _alert_handler_attached:
        return True
    if not _config:
        return False
    if isinstance(_config, dict) and not _config.get("telegram_error_alerts", True):
        return False
    if not _config.get("telegram_token") or not _config.get("telegram_chat_id"):
        return False

    quant_logger = logging.getLogger("QuantBot")
    handler = _TelegramAlertHandler(
        flush_interval_sec=flush_interval_sec,
        max_per_minute=max_per_minute,
        dedup_window_sec=dedup_window_sec,
        sticky_window_sec=sticky_window_sec,
    )
    handler.setFormatter(logging.Formatter('%(message)s'))
    quant_logger.addHandler(handler)
    _alert_handler_attached = True
    return True
