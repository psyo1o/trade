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
import time
import traceback
from datetime import datetime

import requests

_config = None
_bot_source_label = "run_bot.py"


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

    max_attempts = 4
    last_err: Exception | None = None

    for attempt in range(max_attempts):
        try:
            resp = requests.post(api_url, json=payload, timeout=(15, 45))
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
            print(f"⚠️ 텔레그램 전송 재시도 ({attempt + 1}/{max_attempts}) … {type(e).__name__}: {e}")
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
