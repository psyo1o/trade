# -*- coding: utf-8 -*-
"""
미장 유니버스 스크리너 — S&P500 시총 Top100 + Nasdaq100 상위 50 (Tier1 중복 제외).

실행
    * ``run_bot.start_scanner_scheduler`` 가 **매일 15:20 US/Eastern(미장 개장 10분 전)** 에
      ``run_us_screener`` 를 호출해 ``us_universe_cache.json`` 을 강제 재빌드한다.
    * 단독 테스트 시 이 파일을 직접 실행해도 된다 (``python us_screener.py``).

원리
    * 실제 유니버스 빌드 로직은 ``run_bot.get_top_market_cap_tickers`` 에 있으므로
      ``force_refresh=True`` 로 호출해 캐시 TTL 을 무시하고 즉시 다시 계산한다.
    * 위키(S&P500/Nasdaq100) HTML 파싱에는 ``lxml`` 또는 ``html5lib`` 가 필요하다.
"""
from __future__ import annotations

import traceback
from datetime import datetime

try:
    import pytz  # type: ignore
except Exception:  # pragma: no cover
    pytz = None  # noqa: N816


def run_us_screener(limit: int = 150) -> list[str]:
    """
    미장 유니버스를 **강제 재빌드**하고, ``us_universe_cache.json`` 을 새로 쓴다.

    * 반환값: 재빌드된 티커 리스트(실패 시 백업 리스트).
    * 스케줄러(``run_bot.start_scanner_scheduler``) 에서 매일 호출한다.
    """
    import run_bot  # 지연 임포트 — 순환 의존/부팅 시간 최소화

    now_tag = datetime.now(pytz.timezone("US/Eastern")) if pytz else datetime.now()
    print(f"🌙 [미장 발굴기] US 유니버스 재빌드 시작 ({now_tag:%Y-%m-%d %H:%M %Z})")

    try:
        tickers = run_bot.get_top_market_cap_tickers(limit=limit, force_refresh=True)
    except Exception as e:
        print(f"🚨 [미장 발굴기] 재빌드 실패: {e}")
        traceback.print_exc()
        return []

    print(f"🎉 [미장 발굴기] 완료 — 총 {len(tickers)}개 (us_universe_cache.json 갱신)")
    return tickers


if __name__ == "__main__":
    run_us_screener()
