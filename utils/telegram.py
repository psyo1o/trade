# -*- coding: utf-8 -*-
"""
텔레그램 Bot API 래퍼 + 프로세스 종료 훅.

흐름
    1. ``run_bot`` 기동 시 ``configure_telegram(config)`` 로 토큰·chat_id 주입.
    2. ``register_telegram_atexit()`` 로 ``atexit`` 에 ``shutdown_handler`` 등록.
    3. 비정상 종료(스택이 남는 경우) 시 짧은 요약을 ``send_telegram`` 으로 밀어 넣는다.

``parse_mode`` 는 ``Markdown`` 이므로 메시지 본문에서 해당 문법에 맞춘다.
"""
import atexit
import traceback
from datetime import datetime

import requests

_config = None
_bot_source_label = "run_bot.py"


def configure_telegram(config: dict, bot_source_label: str = "run_bot.py"):
    global _config, _bot_source_label
    _config = config
    _bot_source_label = bot_source_label


def send_telegram(message):
    """``configure_telegram`` 가 선행되지 않았으면 조용히 로그만 남기고 반환."""
    if not _config:
        print("⚠️ 텔레그램: config 미설정 (configure_telegram 호출 필요)")
        return
    url = f"https://api.telegram.org/bot{_config['telegram_token']}/sendMessage"
    params = {"chat_id": _config['telegram_chat_id'], "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, params=params, timeout=10)
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패: {e}")


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
