# -*- coding: utf-8 -*-
"""
코인 매수 로직에 쓰이는 데이터 파이프라인 점검.

  - 일봉: ``coin_broker.fetch_ohlcv(..., "day", N)`` (V8 ``calculate_pro_signals`` 등)
  - 15분봉: ``coin_broker.fetch_ohlcv(..., "minute15", N)`` 및 ``ai_filter.get_recent_15m_ohlcv``

실행 (프로젝트 루트에서):

  py -3.11 tests/diagnose_coin_ohlcv_pipeline.py

필요: 루트 ``config.json`` (코인 거래소 키 포함 — 바이낸스면 API, 업비트면 업비트 키).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fmt_last(rows: list[dict], n: int = 3) -> str:
    if not rows:
        return "(없음)"
    tail = rows[-n:]
    parts = []
    for r in tail:
        parts.append(
            f"o={r.get('o')} h={r.get('h')} l={r.get('l')} c={r.get('c')} v={r.get('v')}"
        )
    return " | ".join(parts)


def main() -> int:
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        print("❌ config.json 이 프로젝트 루트에 없습니다.")
        return 1

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    from api import coin_config

    coin_config.configure(cfg)

    if coin_config.is_binance():
        from api import binance_api

        try:
            binance_api.init_binance(cfg)
        except Exception as e:
            print(f"❌ binance_api.init_binance 실패: {e}")
            return 1
        label = "BINANCE(CCXT)"
    else:
        from api import upbit_api

        try:
            upbit_api.init_upbit(cfg)
        except Exception as e:
            print(f"❌ upbit_api.init_upbit 실패: {e}")
            return 1
        label = "UPBIT(pyupbit)"

    bench = coin_config.btc_benchmark_ticker()
    print(f"=== 코인 OHLCV 진단 ({label}) 벤치마크 티커: {bench} ===\n")

    from api import coin_broker
    from strategy.ai_filter import get_recent_15m_ohlcv, get_recent_daily_ohlcv

    # 1) 일봉 — 매수 루프에서 보통 250 or prefetch
    day_need = 250
    try:
        day_rows = coin_broker.fetch_ohlcv(bench, "day", day_need)
    except Exception as e:
        print(f"❌ 일봉 fetch 예외: {e}")
        day_rows = []

    ok_day = bool(day_rows) and len(day_rows) >= 20
    print(f"[일봉] 요청 {day_need}봉 → 수신 {len(day_rows)}봉  {'✅' if ok_day else '⚠️ (<20이면 V8 스킵)'}")
    print(f"      마지막 일봉 샘플: {_fmt_last(day_rows, 2)}")
    print()

    # 2) 15분봉 — AI 휩쏘 게이트
    m15_need = 40
    try:
        m15_rows = coin_broker.fetch_ohlcv(bench, "minute15", m15_need)
    except Exception as e:
        print(f"❌ 15분봉 fetch 예외: {e}")
        m15_rows = []

    ok_m15 = bool(m15_rows) and len(m15_rows) >= 5
    print(f"[15분] coin_broker 요청 {m15_need}봉 → 수신 {len(m15_rows)}봉  {'✅' if ok_m15 else '⚠️'}")
    print(f"      마지막 15m 샘플: {_fmt_last(m15_rows, 3)}")
    print()

    # 3) ai_filter 래퍼 (코인 분기와 동일 경로)
    ai15 = get_recent_15m_ohlcv(bench, "COIN", 10)
    ai_d = get_recent_daily_ohlcv(bench, "COIN", 15)
    print(f"[ai_filter] get_recent_15m_ohlcv(...,10) → {len(ai15)}봉")
    print(f"              get_recent_daily_ohlcv(...,15) → {len(ai_d)}봉")
    print()

    # 4) 바이낸스일 때 유니버스 상위 1~3종목 일봉 길이만 스팟 체크
    if coin_config.is_binance():
        from api import binance_api as _bna

        try:
            tops = _bna.top_usdt_symbols_by_quote_volume(int(cfg.get("binance_universe_top", 10) or 10))
        except Exception as e:
            tops = []
            print(f"⚠️ top_usdt_symbols_by_quote_volume 실패: {e}")

        if tops:
            print(f"[유니버스] 상위 스캔 대상 {len(tops)}개 중 앞 3개 일봉 길이:")
            for t in tops[:3]:
                try:
                    dr = coin_broker.fetch_ohlcv(t, "day", 60)
                    print(f"   {t}: {len(dr)}봉 {'✅' if len(dr) >= 20 else '⚠️ OHLCV 부족'}")
                except Exception as e:
                    print(f"   {t}: 오류 {e}")

    print()
    print("요약: 일봉·15분 API가 정상이면 ‘데이터 없음’이 매수 안 된 주원인은 아님.")
    print("      로그의 ❌ 패스 사유(음봉·역배열 등)는 전략 필터 결과입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
