# -*- coding: utf-8 -*-
"""
Phase 3 뉴스 수집 스모크 테스트 — ``strategy.ai_filter.collect_recent_news_text`` 만 검증한다.

LLM 호출·위험도 점수는 운영 ``test_lab.py`` (LAB_PHASE3_ONLY) 또는 봇 본체에서 확인한다.
뉴스 본문이 비어 있으면 운영 Phase 3는 LLM을 건너뛰고 위험도 0(통과)으로 처리한다.

실행 (저장소 루트):
  python tests/test_news_fetch.py
  py -3.11 tests/test_news_fetch.py

환경변수 (선택):
  NEWS_TEST_TICKER_KR, NEWS_TEST_TICKER_US, NEWS_TEST_TICKER_COIN — 기본 005930, NVDA, BTC-USD
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy.ai_filter import collect_recent_news_text


def _print_section(title: str) -> None:
    print("\n" + "-" * 72)
    print(title)
    print("-" * 72)


def _print_news_block(market: str, ticker: str, text: str) -> None:
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    print(f"market={market} ticker={ticker} headlines={len(lines)} chars={len(text or '')}")
    if not lines:
        print("(empty — Phase 3 would skip LLM and pass with risk 0)")
        return
    for i, line in enumerate(lines[:8], start=1):
        preview = line if len(line) <= 120 else line[:117] + "..."
        print(f"  {i}. {preview}")
    if len(lines) > 8:
        print(f"  ... +{len(lines) - 8} more")


def main() -> None:
    kr = os.environ.get("NEWS_TEST_TICKER_KR", "005930").strip()
    us = os.environ.get("NEWS_TEST_TICKER_US", "NVDA").strip()
    coin = os.environ.get("NEWS_TEST_TICKER_COIN", "BTC-USD").strip()

    _print_section(f"[KR] collect_recent_news_text — {kr}")
    _print_news_block("KR", kr, collect_recent_news_text(kr, "KR"))

    _print_section(f"[US] collect_recent_news_text — {us}")
    _print_news_block("US", us, collect_recent_news_text(us, "US"))

    _print_section(f"[COIN] collect_recent_news_text — {coin}")
    _print_news_block("COIN", coin, collect_recent_news_text(coin, "COIN"))


if __name__ == "__main__":
    main()
