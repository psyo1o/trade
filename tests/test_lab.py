# -*- coding: utf-8 -*-
"""
Phase 1 샌드박스 — 동적 섹터 쏠림 방지 (MDD 1차 방어)

아래 환경변수·``LAB_*`` 플래그 설명은 **이 파일을 스크립트로 직접 실행**할 때만 해당된다.
운영 ``run_bot`` / GUI 경로와는 별개이며, 운영 코드(run_bot.py, strategy/*)는 수정하지 않는다.
실행: 프로젝트 루트에서  py -3.11 tests/test_lab.py

규칙(프롬프트 Phase 1과 동일 수식, 시장별 MAX 사용):
  · 미장: 동일 섹터 허용 max(1, MAX_POSITIONS_US // 2)
  · 국장: 동일 섹터 허용 max(1, MAX_POSITIONS_KR // 2)
  · 코인: 섹터 분리 불가 → 이 파일에서 조회·락 대상 제외

실계좌 기본: run_phase1_real_lab_us() + run_phase1_real_lab_kr() 순 실행. 코인 API 미호출.

환경변수
  LAB_MOCK_ONLY=1       : 실계좌 생략, [US-Mock] 시나리오만.
  LAB_LIVE_YF=1         : Mock 끝에 AAPL yfinance 실조회(선택).
  LAB_CANDIDATES=SPY,QQQ,NVDA   : 미장 가상 매수 후보.
  LAB_CANDIDATES_KR=005930,000660 : 국장 가상 매수 후보(6자리).
  LAB_SKIP_US=1 / LAB_SKIP_KR=1 : 해당 실계좌 블록만 건너뜀.
  LAB_PHASE2_ONLY=1     : Phase 2(TWAP 분할 매수/매도) 샌드박스만 실행.
  LAB_TWAP_DELAY_SEC=0  : Phase 2 슬라이스 사이 대기(초). 기본 0(빠른 테스트).
  운영 run_bot Phase2: config.json — twap_enabled, twap_krw_threshold(원), twap_usd_threshold(달러), twap_slice_delay_sec(초).
  buy_window_minutes_before_close: 국장(15:30)/미장(16:00 ET)/코인 KST09:00 기준 마감 N분 전만 매수(기본 30).
  LAB_PHASE5_ONLY=1     : Phase 5(계좌 MDD 킬스위치 / 청산 계획) 샌드박스만 실행.
  LAB_PHASE3_ONLY=1     : Phase 3(AI 휩쏘 필터) 샌드박스만 실행.
  LAB_PHASE4_ONLY=1     : Phase 4(거시 방어막) 샌드박스만 실행.
  LAB_MDD_PCT=5         : Phase 5-A 시장별 킬 시뮬 하락률 임계(%).
  LAB_ACCOUNT_CIRCUIT_PCT=15 : Phase 5-B 합산 서킷 임계(%).
  LAB_FALSE_BREAK_THRESHOLD=70 : Phase 3 차단 임계값(기본 70%).
  LAB_AI_PROVIDER=openai|gemini|both|auto : Phase 3 AI 제공자 선택(기본 auto).
  LAB_USE_AI=1          : 룰베이스 대신 AI 호출 시도.
  OPENAI_API_KEY / GOOGLE_API_KEY : 선택한 AI 제공자 키.
  tests/ai_keys.txt               : 이 샌드박스 전용(환경변수 없을 때). 운영 run_bot 은
                                    config.json 동일 키 또는 루트 ``ai_keys.txt`` (``strategy/ai_filter._get_secret``).
  LAB_VIX=27.5          : Phase 4 테스트용 VIX 값(미지정 시 실조회 시도).
  LAB_FGI=82            : Phase 4 테스트용 Fear&Greed 값(0~100).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Set, Tuple

_BOT_ROOT = Path(__file__).resolve().parents[1]
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))

import yfinance as yf

from strategy.macro_guard import evaluate_macro_guard, get_macro_guard_snapshot

from execution.circuit_break import evaluate_total_account_circuit, estimate_usdkrw
from execution.guard import (
    ACCOUNT_CIRCUIT_COOLDOWN_KEY,
    in_account_circuit_cooldown,
    load_state,
    save_state,
    set_account_circuit_cooldown,
    update_peak_equity_total_krw,
)
from execution.order_twap import plan_krw_slices, plan_sell_qty_twap, plan_usd_slices, run_krw_slices, run_qty_slice_sells

_KEY_FILE_CACHE: Dict[str, str] = {}


def _read_key_from_local_file(key_name: str) -> str:
    """
    tests/ai_keys.txt 형식:
      GOOGLE_API_KEY=xxxx
      OPENAI_API_KEY=yyyy
    """
    global _KEY_FILE_CACHE
    if not _KEY_FILE_CACHE:
        key_file = Path(__file__).resolve().parent / "ai_keys.txt"
        if key_file.exists():
            try:
                for line in key_file.read_text(encoding="utf-8").splitlines():
                    raw = line.strip()
                    if not raw or raw.startswith("#") or "=" not in raw:
                        continue
                    k, v = raw.split("=", 1)
                    _KEY_FILE_CACHE[k.strip()] = v.strip().strip("'\"")
            except Exception:
                _KEY_FILE_CACHE = {}
    return _KEY_FILE_CACHE.get(key_name, "")


def _get_secret(key_name: str) -> str:
    """환경변수 우선, 없으면 tests/ai_keys.txt."""
    env_val = os.environ.get(key_name, "").strip()
    if env_val:
        return env_val
    return _read_key_from_local_file(key_name).strip()

# ---------------------------------------------------------------------------
# 미장 (US)
# ---------------------------------------------------------------------------


def max_us_positions_per_sector(max_positions_us: int) -> int:
    """동일 섹터 허용 상한: max(1, MAX_POSITIONS_US // 2)"""
    return max(1, int(max_positions_us) // 2)


def is_us_equity_ticker(ticker: str) -> bool:
    """국장(숫자코드)·코인(KRW-) 제외한 미국 주식 티커."""
    t = str(ticker or "").strip().upper()
    if not t or t.startswith("KRW-"):
        return False
    if t.isdigit():
        return False
    return True


def build_sector_resolver(
    mock_sector_by_ticker: Dict[str, str],
) -> Tuple[Callable[[str], str], Dict[str, str]]:
    """Mock 섹터 (교과서 시나리오)."""
    cache: Dict[str, str] = {}

    def sector_for(ticker: str) -> str:
        t = str(ticker or "").strip().upper()
        if t in cache:
            return cache[t]
        sec = mock_sector_by_ticker.get(t, "Unknown")
        cache[t] = sec
        return sec

    return sector_for, cache


def count_us_positions_by_sector(
    positions_keys: Dict[str, object],
    sector_for: Callable[[str], str],
) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for sym in positions_keys:
        if not is_us_equity_ticker(sym):
            continue
        counts[sector_for(sym)] += 1
    return dict(counts)


def allow_new_us_buy_by_sector(
    candidate_ticker: str,
    position_keys: Dict[str, object],
    max_positions_us: int,
    sector_for: Callable[[str], str],
) -> Tuple[bool, str]:
    if not is_us_equity_ticker(candidate_ticker):
        return True, "미국 주식이 아니면 섹터 락 미적용(통과)"

    limit = max_us_positions_per_sector(max_positions_us)
    sec = sector_for(candidate_ticker)
    same = 0
    for sym in position_keys:
        if not is_us_equity_ticker(sym):
            continue
        if sector_for(sym) == sec:
            same += 1
    if same >= limit:
        return (
            False,
            f"[SECTOR_LOCK:US] 섹터 '{sec}' 보유 {same}개 >= 한도 {limit} (MAX_POSITIONS_US={max_positions_us}) → 패스",
        )
    return True, f"[SECTOR_LOCK:US] 섹터 '{sec}' 보유 {same}개 < 한도 {limit} → 허용"


# ---------------------------------------------------------------------------
# 국장 (KR) — yfinance .KS / .KQ
# ---------------------------------------------------------------------------


def max_kr_positions_per_sector(max_positions_kr: int) -> int:
    """동일 섹터 허용 상한: max(1, MAX_POSITIONS_KR // 2)"""
    return max(1, int(max_positions_kr) // 2)


def is_kr_equity_ticker(ticker: str) -> bool:
    """6자리 국내 주식 코드(장부 키 기준). 코인·미장 제외."""
    t = str(ticker or "").strip()
    if not t or t.startswith("KRW-") or t.upper().startswith("KRW-"):
        return False
    if not t.isdigit():
        return False
    return len(t.zfill(6)) == 6


def build_yfinance_kr_sector_resolver(sleep_sec: float = 0.35) -> Tuple[Callable[[str], str], Dict[str, str]]:
    """KOSPI .KS 우선, 없으면 .KQ (yfinance info sector / industry)."""
    cache: Dict[str, str] = {}

    def sector_for(code: str) -> str:
        c = str(code or "").strip().zfill(6)
        if not is_kr_equity_ticker(c):
            return "Unknown"
        if c in cache:
            return cache[c]
        try:
            import time as _time

            import yfinance as yf  # type: ignore
        except ImportError:
            cache[c] = "Unknown"
            return cache[c]

        for suffix in (".KS", ".KQ"):
            try:
                _time.sleep(sleep_sec)
                sym = f"{c}{suffix}"
                info = yf.Ticker(sym).info or {}
                sec = info.get("sector") or info.get("industry")
                if sec:
                    cache[c] = str(sec)
                    return cache[c]
            except Exception:  # noqa: BLE001
                continue
        cache[c] = "Unknown"
        return cache[c]

    return sector_for, cache


def count_kr_positions_by_sector(
    positions_keys: Dict[str, object],
    sector_for: Callable[[str], str],
    normalize: Callable[[str], str],
) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for sym in positions_keys:
        k = normalize(sym)
        if not is_kr_equity_ticker(k):
            continue
        counts[sector_for(k)] += 1
    return dict(counts)


def allow_new_kr_buy_by_sector(
    candidate_code: str,
    position_keys: Dict[str, object],
    max_positions_kr: int,
    sector_for: Callable[[str], str],
    normalize: Callable[[str], str],
) -> Tuple[bool, str]:
    cand = normalize(str(candidate_code or ""))
    if not is_kr_equity_ticker(cand):
        return True, "국장 종목이 아니면 섹터 락 미적용(통과)"

    limit = max_kr_positions_per_sector(max_positions_kr)
    sec = sector_for(cand)
    same = 0
    for raw in position_keys:
        k = normalize(raw)
        if not is_kr_equity_ticker(k):
            continue
        if sector_for(k) == sec:
            same += 1
    if same >= limit:
        return (
            False,
            f"[SECTOR_LOCK:KR] 섹터 '{sec}' 보유 {same}개 >= 한도 {limit} (MAX_POSITIONS_KR={max_positions_kr}) → 패스",
        )
    return True, f"[SECTOR_LOCK:KR] 섹터 '{sec}' 보유 {same}개 < 한도 {limit} → 허용"


# ---------------------------------------------------------------------------
# Mock 시나리오 (미장만, 교과서)
# ---------------------------------------------------------------------------


def _print_us_mock_scenario(title: str, max_us: int, book: Dict[str, object], candidate: str, mock_map: Dict[str, str]) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    sector_for, _ = build_sector_resolver(mock_map)
    limit = max_us_positions_per_sector(max_us)
    by_sec = count_us_positions_by_sector(book, sector_for)
    ok, msg = allow_new_us_buy_by_sector(candidate, book, max_us, sector_for)
    print(f"  MAX_POSITIONS_US = {max_us}  →  동일 섹터 한도 = {limit}")
    print(f"  현재 미장 섹터 분포: {by_sec}")
    print(f"  후보 매수: {candidate} (섹터={sector_for(candidate)})")
    print(f"  결과: {'허용' if ok else '차단'}")
    print(f"  {msg}")


def run_all_mock_scenarios() -> None:
    print("\n" + "#" * 72)
    print("#  [US-Mock] [1]~[5] 가짜 장부. 실계좌와 무관.")
    print("#  실계좌: py -3.11 tests/test_lab.py  |  Mock만: LAB_MOCK_ONLY=1")
    print("#" * 72)

    mock = {
        "AAPL": "Technology",
        "MSFT": "Technology",
        "NVDA": "Technology",
        "JPM": "Financial Services",
        "XOM": "Energy",
        "KO": "Consumer Defensive",
    }

    _print_us_mock_scenario(
        "[US-Mock 1] MAX_US=3, Tech 1종 → 동일 섹터 추가(MSFT) 차단",
        3,
        {"AAPL": {}, "005930": {}},
        "MSFT",
        mock,
    )
    _print_us_mock_scenario(
        "[US-Mock 2] MAX_US=3, Financial 1종 → Tech 신규(NVDA) 허용",
        3,
        {"JPM": {}},
        "NVDA",
        mock,
    )
    _print_us_mock_scenario(
        "[US-Mock 3] MAX_US=6, Tech 3종 → 동일 섹터 4번째(GOOGL=Tech) 차단",
        6,
        {"AAPL": {}, "MSFT": {}, "NVDA": {}},
        "GOOGL",
        {**mock, "GOOGL": "Technology"},
    )
    _print_us_mock_scenario(
        "[US-Mock 4] MAX_US=6, Tech 3종 → 다른 섹터(XOM) 허용",
        6,
        {"AAPL": {}, "MSFT": {}, "NVDA": {}},
        "XOM",
        mock,
    )
    _print_us_mock_scenario(
        "[US-Mock 5] 장부에 미국 티커 없음(코인+국장만) → 미장 신규 허용",
        3,
        {"KRW-BTC": {}, "005930": {}},
        "AAPL",
        mock,
    )


def build_yfinance_us_sector_resolver(sleep_sec: float = 0.35) -> Tuple[Callable[[str], str], Dict[str, str]]:
    """미장: yfinance Ticker(티커 그대로)."""
    cache: Dict[str, str] = {}

    def sector_for(ticker: str) -> str:
        t = str(ticker or "").strip().upper()
        if not t:
            return "Unknown"
        if t in cache:
            return cache[t]
        try:
            import time as _time

            import yfinance as yf  # type: ignore

            _time.sleep(sleep_sec)
            info = yf.Ticker(t).info or {}
            sec = info.get("sector") or info.get("industry") or "Unknown"
        except Exception as e:  # noqa: BLE001
            sec = f"lookup_error:{type(e).__name__}"
        cache[t] = str(sec)
        return cache[t]

    return sector_for, cache


def run_phase1_real_lab_us() -> None:
    """Phase1 실계좌 샌드박스 — 미장만 (KIS API ∪ 장부 ∪ yfinance)."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    print("\n" + "=" * 72)
    print("[Phase1 실계좌: US] KIS 미장 + 장부 미국 키 + yfinance")
    print("  (코인 미조회)")
    print("=" * 72)

    import run_bot as rb  # noqa: WPS433

    try:
        rb.refresh_brokers_if_needed(force=False)
    except Exception as e:  # noqa: BLE001
        print(f"  refresh_brokers_if_needed 경고: {e}")

    from api import kis_api  # noqa: WPS433

    if kis_api.broker_kr is None and kis_api.broker_us is None:
        print("  broker_kr / broker_us 모두 None — config·kis_token 확인.")

    held_us = rb.get_held_stocks_us() if kis_api.broker_us is not None else None
    state = rb.load_state(rb.STATE_PATH)
    pos = state.get("positions", {}) if isinstance(state.get("positions"), dict) else {}

    us_ledger: Dict[str, object] = {}
    for raw_k in pos:
        k = rb.normalize_ticker(raw_k)
        if is_us_equity_ticker(k):
            us_ledger[k] = pos[raw_k]

    held_norm: List[str] = []
    if held_us is not None:
        held_norm = [rb.normalize_ticker(x) for x in held_us if x]

    print(f"  run_bot.MAX_POSITIONS_US = {rb.MAX_POSITIONS_US}")
    print(f"  동일 섹터 한도 = {max_us_positions_per_sector(rb.MAX_POSITIONS_US)}")
    print(f"  미장 API 보유: {held_norm if held_us is not None else '(조회 실패 또는 broker_us 없음)'}")
    print(f"  장부 미국 키: {list(us_ledger.keys())}")

    api_set: Set[str] = set(held_norm)
    ledger_set = set(us_ledger.keys())
    only_api = sorted(api_set - ledger_set)
    only_ledger = sorted(ledger_set - api_set)
    if only_api:
        print(f"  (참고) API에만 있음: {only_api}")
    if only_ledger:
        print(f"  (참고) 장부에만 있음: {only_ledger}")

    sector_for, _ = build_yfinance_us_sector_resolver()
    print("\n  --- yfinance 섹터 (미장 API ∪ 장부) ---")
    tickers_to_lookup = sorted(api_set | ledger_set)
    if not tickers_to_lookup:
        print("  (미국 주식 티커 없음)")
        return

    for sym in tickers_to_lookup:
        sec = sector_for(sym)
        src = []
        if sym in api_set:
            src.append("API")
        if sym in ledger_set:
            src.append("장부")
        print(f"    {sym:8s}  sector={sec!r}  [{'+'.join(src)}]")

    by_sec = count_us_positions_by_sector(us_ledger, sector_for)
    print(f"\n  장부 기준 미장 섹터 분포: {by_sec}")

    raw_cands = os.environ.get("LAB_CANDIDATES", "SPY,QQQ,NVDA").replace(";", ",")
    candidates = [c.strip().upper() for c in raw_cands.split(",") if c.strip()]
    print("\n  --- 가상 미장 매수 후보 (LAB_CANDIDATES) ---")
    for c in candidates:
        ok, msg = allow_new_us_buy_by_sector(c, us_ledger, rb.MAX_POSITIONS_US, sector_for)
        print(f"    후보 {c}: {'허용' if ok else '차단'}  |  {msg}")


def run_phase1_real_lab_kr() -> None:
    """Phase1 실계좌 샌드박스 — 국장만 (KIS API ∪ 장부 ∪ yfinance .KS/.KQ)."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    print("\n" + "=" * 72)
    print("[Phase1 실계좌: KR] KIS 국장 + 장부 국내 키 + yfinance (.KS/.KQ)")
    print("  (코인 미조회 · 미장 섹터와 별도)")
    print("=" * 72)

    import run_bot as rb  # noqa: WPS433

    try:
        rb.refresh_brokers_if_needed(force=False)
    except Exception as e:  # noqa: BLE001
        print(f"  refresh_brokers_if_needed 경고: {e}")

    from api import kis_api  # noqa: WPS433

    held_kr = rb.get_held_stocks_kr() if kis_api.broker_kr is not None else None
    state = rb.load_state(rb.STATE_PATH)
    pos = state.get("positions", {}) if isinstance(state.get("positions"), dict) else {}

    kr_ledger: Dict[str, object] = {}
    for raw_k in pos:
        k = rb.normalize_ticker(raw_k)
        if is_kr_equity_ticker(k):
            kr_ledger[k] = pos[raw_k]

    held_norm: List[str] = []
    if held_kr is not None:
        held_norm = [rb.normalize_ticker(x) for x in held_kr if x]

    print(f"  run_bot.MAX_POSITIONS_KR = {rb.MAX_POSITIONS_KR}")
    print(f"  동일 섹터 한도 = {max_kr_positions_per_sector(rb.MAX_POSITIONS_KR)}")
    print(f"  국장 API 보유: {held_norm if held_kr is not None else '(조회 실패 또는 broker_kr 없음)'}")
    print(f"  장부 국내 키: {list(kr_ledger.keys())}")

    api_set: Set[str] = set(held_norm)
    ledger_set = set(kr_ledger.keys())
    only_api = sorted(api_set - ledger_set)
    only_ledger = sorted(ledger_set - api_set)
    if only_api:
        print(f"  (참고) API에만 있음: {only_api}")
    if only_ledger:
        print(f"  (참고) 장부에만 있음: {only_ledger}")

    sector_for, _ = build_yfinance_kr_sector_resolver()
    norm = rb.normalize_ticker

    print("\n  --- yfinance 섹터 (국장 API ∪ 장부, 6자리) ---")
    codes = sorted(api_set | ledger_set)
    if not codes:
        print("  (국내 주식 코드 없음)")
        return

    for code in codes:
        sec = sector_for(code)
        src = []
        if code in api_set:
            src.append("API")
        if code in ledger_set:
            src.append("장부")
        print(f"    {code}  sector={sec!r}  [{'+'.join(src)}]")

    by_sec = count_kr_positions_by_sector(kr_ledger, sector_for, norm)
    print(f"\n  장부 기준 국장 섹터 분포: {by_sec}")

    raw_cands = os.environ.get("LAB_CANDIDATES_KR", "005930,000660,035420").replace(";", ",")
    candidates = [norm(c.strip()) for c in raw_cands.split(",") if c.strip()]
    print("\n  --- 가상 국장 매수 후보 (LAB_CANDIDATES_KR) ---")
    for c in candidates:
        ok, msg = allow_new_kr_buy_by_sector(c, kr_ledger, rb.MAX_POSITIONS_KR, sector_for, norm)
        print(f"    후보 {c}: {'허용' if ok else '차단'}  |  {msg}")


def _rule_based_false_breakout_prob(candles_15m_10: List[Dict[str, float]], orderbook: Dict[str, float]) -> int:
    """OpenAI 비활성 시 fallback 확률 점수기(0~100)."""
    if not candles_15m_10 or len(candles_15m_10) < 3:
        return 50

    recent = candles_15m_10[-3:]
    highs = [float(c["h"]) for c in candles_15m_10]
    closes = [float(c["c"]) for c in candles_15m_10]
    volumes = [float(c["v"]) for c in candles_15m_10]

    max_recent_high = max(float(c["h"]) for c in recent)
    last_close = closes[-1]
    avg_vol = sum(volumes[:-1]) / max(1, len(volumes) - 1)
    last_vol = volumes[-1]

    body = abs(float(recent[-1]["c"]) - float(recent[-1]["o"]))
    upper_wick = float(recent[-1]["h"]) - max(float(recent[-1]["c"]), float(recent[-1]["o"]))
    wick_ratio = upper_wick / max(1e-9, body + upper_wick)

    resistance = max(highs[:-1]) if len(highs) > 1 else highs[-1]
    failed_break = 1 if (max_recent_high >= resistance and last_close < resistance) else 0

    bid = float(orderbook.get("bid_size_total", 0.0))
    ask = float(orderbook.get("ask_size_total", 0.0))
    imbalance = (bid - ask) / max(1e-9, bid + ask)

    score = 35
    if wick_ratio > 0.55:
        score += 20
    if last_vol < avg_vol * 0.85:
        score += 15
    if failed_break:
        score += 20
    if imbalance < -0.12:
        score += 15

    return max(0, min(100, int(round(score))))


def _asset_type_from_ticker(ticker: str) -> str:
    t = str(ticker or "").strip().upper()
    if t.startswith("KRW-"):
        return "coin"
    return "stock"


def _build_aux_context_for_asset(asset_type: str) -> Dict[str, Any]:
    """
    [핵심 분기]
    - stock: 나스닥 선물(NQ=F) 15분봉 흐름
    - coin : KRW-BTC 15분봉 추세
    """
    if asset_type == "coin":
        # 코인 보조 데이터: KRW-BTC 15분봉
        try:
            import pyupbit  # type: ignore

            df = pyupbit.get_ohlcv("KRW-BTC", interval="minute15", count=10)
            if df is not None and not df.empty:
                candles = [
                    {
                        "o": float(r["open"]),
                        "h": float(r["high"]),
                        "l": float(r["low"]),
                        "c": float(r["close"]),
                        "v": float(r["volume"]),
                    }
                    for _, r in df.tail(10).iterrows()
                ]
                return {"aux_type": "btc_15m", "aux_candles": candles}
        except Exception:
            pass
        return {"aux_type": "btc_15m", "aux_candles": []}

    # 주식 보조 데이터: 나스닥 선물(NQ=F) 15분봉
    try:
        df = yf.Ticker("NQ=F").history(interval="15m", period="3d")
        if df is not None and not df.empty:
            candles = [
                {
                    "o": float(r["Open"]),
                    "h": float(r["High"]),
                    "l": float(r["Low"]),
                    "c": float(r["Close"]),
                    "v": float(r["Volume"]),
                }
                for _, r in df.tail(10).iterrows()
            ]
            return {"aux_type": "nasdaq_futures_15m", "aux_candles": candles}
    except Exception:
        pass
    return {"aux_type": "nasdaq_futures_15m", "aux_candles": []}


def _openai_false_breakout_prob(
    ticker: str,
    candles_15m_10: List[Dict[str, float]],
    orderbook: Dict[str, float],
    model_name: str = "gpt-4o-mini",
) -> Tuple[int, str]:
    """OpenAI 가능 시 호출, 아니면 fallback."""
    api_key = _get_secret("OPENAI_API_KEY")
    if not api_key:
        return _rule_based_false_breakout_prob(candles_15m_10, orderbook), "fallback:OPENAI_API_KEY 없음"

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return _rule_based_false_breakout_prob(candles_15m_10, orderbook), "fallback:openai 패키지 없음"

    client = OpenAI(api_key=api_key)
    asset_type = _asset_type_from_ticker(ticker)
    aux_ctx = _build_aux_context_for_asset(asset_type)
    prompt = (
        "당신은 기관 퀀트 트레이더다. 아래 15분봉 10개와 호가 요약을 보고 "
        "False Breakout(가짜 돌파) 함정 확률을 0~100 정수로 평가하라.\n"
        "[핵심 분기]\n"
        "- stock이면 나스닥 선물 흐름을 더 크게 반영\n"
        "- coin이면 KRW-BTC 15분봉 추세를 더 크게 반영\n"
        "출력은 JSON만: {\"false_breakout_prob\": <int>, \"rationale\": \"짧은 한국어\"}\n"
        f"ticker={ticker}, asset_type={asset_type}\n"
        f"candles={candles_15m_10}\n"
        f"orderbook={orderbook}\n"
        f"aux_context={aux_ctx}\n"
    )
    try:
        resp = client.responses.create(
            model=model_name,
            input=prompt,
            temperature=0.1,
        )
        text = (resp.output_text or "").strip()
        import json

        payload = json.loads(text)
        prob = int(payload.get("false_breakout_prob", 50))
        reason = str(payload.get("rationale", "no_rationale"))
        return max(0, min(100, prob)), reason
    except Exception as e:  # noqa: BLE001
        return _rule_based_false_breakout_prob(candles_15m_10, orderbook), f"fallback:{type(e).__name__}"


def _gemini_false_breakout_prob(
    ticker: str,
    candles_15m_10: List[Dict[str, float]],
    orderbook: Dict[str, float],
    model_name: str = "gemini-2.5-flash",
) -> Tuple[int, str]:
    """Google Gemini REST 호출. 실패 시 fallback."""
    api_key = _get_secret("GOOGLE_API_KEY")
    if not api_key:
        return _rule_based_false_breakout_prob(candles_15m_10, orderbook), "fallback:GOOGLE_API_KEY 없음"

    try:
        import requests  # type: ignore
    except Exception:
        return _rule_based_false_breakout_prob(candles_15m_10, orderbook), "fallback:requests 패키지 없음"

    asset_type = _asset_type_from_ticker(ticker)
    aux_ctx = _build_aux_context_for_asset(asset_type)
    prompt = (
        "당신은 기관 퀀트 트레이더다. 아래 15분봉 10개와 호가 요약을 보고 "
        "False Breakout(가짜 돌파) 함정 확률을 0~100 정수로 평가하라.\n"
        "[핵심 분기]\n"
        "- stock이면 나스닥 선물 흐름을 더 크게 반영\n"
        "- coin이면 KRW-BTC 15분봉 추세를 더 크게 반영\n"
        "출력은 JSON만: {\"false_breakout_prob\": <int>, \"rationale\": \"짧은 한국어\"}\n"
        f"ticker={ticker}, asset_type={asset_type}\n"
        f"candles={candles_15m_10}\n"
        f"orderbook={orderbook}\n"
        f"aux_context={aux_ctx}\n"
    )

    model_candidates = [
        model_name,
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-flash-latest",
    ]
    last_err = ""
    for mdl in model_candidates:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={api_key}"
            body = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1},
            }
            resp = requests.post(url, json=body, timeout=20)
            if resp.status_code == 404:
                # 다른 모델 후보를 시도
                last_err = f"{mdl}:404"
                continue
            if resp.status_code >= 400:
                last_err = f"{mdl}:{resp.status_code}:{resp.text[:120]}"
                continue
            resp.raise_for_status()
            data = resp.json()
            parts = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])
            )
            text = str(parts[0].get("text", "") if parts else "").strip()
            if not text:
                continue
            import json
            import re

            # 코드블럭/잡텍스트 방어
            m = re.search(r"\{.*\}", text, flags=re.S)
            payload = json.loads(m.group(0) if m else text)
            prob = int(payload.get("false_breakout_prob", 50))
            reason = str(payload.get("rationale", "no_rationale"))
            return max(0, min(100, prob)), reason
        except Exception as e:
            last_err = f"{mdl}:{type(e).__name__}"
            continue

    msg = f"fallback:Gemini 모델/응답 실패 ({last_err})" if last_err else "fallback:Gemini 모델/응답 실패"
    return _rule_based_false_breakout_prob(candles_15m_10, orderbook), msg


def _resolve_ai_provider() -> str:
    provider = os.environ.get("LAB_AI_PROVIDER", "auto").strip().lower()
    if provider in ("openai", "gemini", "both"):
        return provider
    if _get_secret("GOOGLE_API_KEY"):
        return "gemini"
    if _get_secret("OPENAI_API_KEY"):
        return "openai"
    return "rule_based"


def evaluate_false_breakout_filter(
    ticker: str,
    candles_15m_10: List[Dict[str, float]],
    orderbook: Dict[str, float],
    threshold: int = 70,
    use_ai: bool = False,
    ai_provider: str = "auto",
) -> Dict[str, Any]:
    """확률 >= threshold면 강제 패스."""
    provider = ai_provider.strip().lower() if ai_provider else "auto"
    if use_ai and provider == "auto":
        provider = _resolve_ai_provider()

    if use_ai and provider == "openai":
        prob, rationale = _openai_false_breakout_prob(ticker, candles_15m_10, orderbook)
        provider_used = "openai"
    elif use_ai and provider == "gemini":
        prob, rationale = _gemini_false_breakout_prob(ticker, candles_15m_10, orderbook)
        provider_used = "gemini"
    elif use_ai and provider == "both":
        # evaluate_false_breakout_filter는 단일 결과 반환 함수이므로
        # both는 호출부(run_phase3_ai_filter_lab)에서 분기 처리한다.
        prob = _rule_based_false_breakout_prob(candles_15m_10, orderbook)
        rationale = "rule_based(both_mode_dispatch_in_runner)"
        provider_used = "rule_based"
    elif use_ai and provider == "rule_based":
        prob = _rule_based_false_breakout_prob(candles_15m_10, orderbook)
        rationale = "rule_based(auto_no_key)"
        provider_used = "rule_based"
    else:
        prob = _rule_based_false_breakout_prob(candles_15m_10, orderbook)
        rationale = "rule_based"
        provider_used = "rule_based"
    # "70%를 넘으면" => strict greater-than
    blocked = prob > int(threshold)
    return {
        "ticker": ticker,
        "false_breakout_prob": prob,
        "threshold": int(threshold),
        "blocked": blocked,
        "rationale": rationale,
        "provider": provider_used,
    }


def run_phase3_ai_filter_lab() -> None:
    """Phase 3 샌드박스: Mock 15분봉 10개 + Mock 호가창."""
    print("\n" + "=" * 72)
    print("[Phase3 샌드박스] AI 휩쏘(False Breakout) 필터")
    print("=" * 72)

    threshold = int(os.environ.get("LAB_FALSE_BREAK_THRESHOLD", "70"))
    use_ai = os.environ.get("LAB_USE_AI", "").strip().lower() in ("1", "true", "yes", "on")
    ai_provider = os.environ.get("LAB_AI_PROVIDER", "auto").strip().lower()
    print(f"  use_ai={use_ai}, provider={ai_provider}")

    candles_a = [
        {"o": 100.0, "h": 101.2, "l": 99.8, "c": 100.9, "v": 120000},
        {"o": 100.9, "h": 101.6, "l": 100.3, "c": 101.2, "v": 118000},
        {"o": 101.2, "h": 102.4, "l": 100.8, "c": 101.9, "v": 115000},
        {"o": 101.9, "h": 103.1, "l": 101.7, "c": 102.5, "v": 112000},
        {"o": 102.5, "h": 103.9, "l": 102.2, "c": 103.4, "v": 108000},
        {"o": 103.4, "h": 104.0, "l": 102.9, "c": 103.1, "v": 96000},
        {"o": 103.1, "h": 104.2, "l": 102.8, "c": 103.7, "v": 94000},
        {"o": 103.7, "h": 104.8, "l": 103.0, "c": 103.5, "v": 90000},
        {"o": 103.5, "h": 105.4, "l": 103.2, "c": 103.6, "v": 86000},
        {"o": 103.6, "h": 106.0, "l": 103.1, "c": 103.4, "v": 78000},
    ]
    orderbook_a = {"bid_size_total": 420000, "ask_size_total": 620000}

    candles_b = [
        {"o": 50.0, "h": 50.5, "l": 49.9, "c": 50.4, "v": 80000},
        {"o": 50.4, "h": 50.8, "l": 50.2, "c": 50.7, "v": 82000},
        {"o": 50.7, "h": 51.0, "l": 50.5, "c": 50.9, "v": 84000},
        {"o": 50.9, "h": 51.2, "l": 50.7, "c": 51.1, "v": 86000},
        {"o": 51.1, "h": 51.5, "l": 50.9, "c": 51.3, "v": 90000},
        {"o": 51.3, "h": 51.8, "l": 51.1, "c": 51.6, "v": 94000},
        {"o": 51.6, "h": 52.0, "l": 51.4, "c": 51.9, "v": 98000},
        {"o": 51.9, "h": 52.3, "l": 51.8, "c": 52.2, "v": 102000},
        {"o": 52.2, "h": 52.8, "l": 52.0, "c": 52.6, "v": 108000},
        {"o": 52.6, "h": 53.4, "l": 52.5, "c": 53.2, "v": 122000},
    ]
    orderbook_b = {"bid_size_total": 680000, "ask_size_total": 520000}
    candles_c = [
        {"o": 2100.0, "h": 2120.0, "l": 2090.0, "c": 2115.0, "v": 55000},
        {"o": 2115.0, "h": 2140.0, "l": 2110.0, "c": 2132.0, "v": 53000},
        {"o": 2132.0, "h": 2165.0, "l": 2128.0, "c": 2151.0, "v": 51000},
        {"o": 2151.0, "h": 2188.0, "l": 2149.0, "c": 2179.0, "v": 50000},
        {"o": 2179.0, "h": 2210.0, "l": 2170.0, "c": 2204.0, "v": 48000},
        {"o": 2204.0, "h": 2248.0, "l": 2200.0, "c": 2210.0, "v": 45000},
        {"o": 2210.0, "h": 2265.0, "l": 2205.0, "c": 2218.0, "v": 42000},
        {"o": 2218.0, "h": 2288.0, "l": 2210.0, "c": 2220.0, "v": 40000},
        {"o": 2220.0, "h": 2310.0, "l": 2215.0, "c": 2230.0, "v": 38000},
        {"o": 2230.0, "h": 2350.0, "l": 2222.0, "c": 2234.0, "v": 36000},
    ]
    orderbook_c = {"bid_size_total": 350000, "ask_size_total": 540000}

    for ticker, candles, orderbook in [
        ("MOCK_TRAP_A", candles_a, orderbook_a),
        ("MOCK_CLEAN_B", candles_b, orderbook_b),
        ("KRW-ETH", candles_c, orderbook_c),
    ]:
        print(f"\n  [{ticker}] threshold={threshold}%")
        providers_to_run = [ai_provider]
        if use_ai and ai_provider == "both":
            providers_to_run = ["openai", "gemini"]

        for p in providers_to_run:
            result = evaluate_false_breakout_filter(
                ticker=ticker,
                candles_15m_10=candles,
                orderbook=orderbook,
                threshold=threshold,
                use_ai=use_ai,
                ai_provider=p,
            )
            print(f"    provider = {result['provider']}")
            print(f"    false_breakout_prob = {result['false_breakout_prob']}%")
            print(f"    decision = {'차단(패스)' if result['blocked'] else '통과(매수 후보 유지)'}")
            print(f"    rationale = {result['rationale']}")


def run_phase4_macro_guard_lab() -> None:
    """Phase 4 샌드박스: 거시 데이터로 예산 차단/축소 판단 (운영과 동일 `get_macro_guard_snapshot`)."""
    print("\n" + "=" * 72)
    print("[Phase4 샌드박스] Macro 대체데이터 방어막 (VIX/Fear&Greed)")
    print("=" * 72)

    lab_cfg: Dict[str, Any] = {"macro_guard_enabled": True}
    vix_env = os.environ.get("LAB_VIX", "").strip()
    fgi_env = os.environ.get("LAB_FGI", "").strip()
    if vix_env:
        lab_cfg["macro_vix_override"] = vix_env
    if fgi_env:
        lab_cfg["macro_fgi_override"] = fgi_env

    snap = get_macro_guard_snapshot(lab_cfg)
    vix_val = float(snap.get("vix") or 0.0)
    fgi_val = int(snap.get("fgi") or 0)
    vix_src = str(snap.get("vix_source") or "?")
    fgi_src = str(snap.get("fgi_source") or "?")

    base_budget = 1_000_000.0
    mult = float(snap.get("budget_multiplier", 1.0))
    adjusted_budget = base_budget * mult

    print(f"  VIX = {vix_val:.2f} ({vix_src})")
    print(f"  Fear&Greed = {fgi_val} ({fgi_src})")
    print(f"  decision = {snap.get('mode')} | reason = {snap.get('reason')}")
    print(f"  budget: {int(base_budget):,} -> {int(adjusted_budget):,} (x{mult})")

    print("\n  --- 시나리오 검증 (Mock) ---")
    for name, vix, fgi in [
        ("NORMAL", 18.0, 55),
        ("EXTREME_GREED", 19.5, 84),
        ("HIGH_VIX", 29.0, 62),
    ]:
        d = evaluate_macro_guard(vix, fgi)
        print(
            f"    {name:14s} | VIX={vix:>5.1f}, FGI={fgi:>3d} -> "
            f"{d['mode']:>6s}, budget x{d['budget_multiplier']}, {d['reason']}"
        )


def evaluate_account_killswitch(
    peak_equity: float,
    current_equity: float,
    mdd_limit_pct: float = 5.0,
) -> Dict[str, Any]:
    """
    계좌 단위 고점 대비 하락이 임계를 넘으면 킬스위치(신규 매수 중단·청산 검토).
    guard.check_mdd_break 와 동일한 비율: current < peak * (1 - mdd_limit_pct/100) 일 때 발동.
    """
    peak = float(peak_equity)
    cur = float(current_equity)
    if peak <= 0:
        return {
            "triggered": False,
            "peak": peak,
            "current": cur,
            "drawdown_pct": 0.0,
            "threshold_pct": float(mdd_limit_pct),
            "reason": "peak<=0 (비교 불가)",
        }
    threshold_frac = max(0.0, float(mdd_limit_pct)) / 100.0
    floor = peak * (1.0 - threshold_frac)
    dd_pct = (peak - cur) / peak * 100.0
    triggered = cur < floor
    if triggered:
        reason = (
            f"고점 {peak:,.0f} 대비 {dd_pct:.2f}% 하락 "
            f"(임계 {mdd_limit_pct:g}% 초과 — floor {floor:,.0f})"
        )
    elif cur > peak:
        up_pct = (cur - peak) / peak * 100.0
        reason = f"고점 갱신 (+{up_pct:.2f}% vs 기록고점 {peak:,.0f})"
    else:
        reason = f"고점 대비 {dd_pct:.2f}% 하락 — 임계({mdd_limit_pct:g}%) 이내"
    return {
        "triggered": triggered,
        "peak": peak,
        "current": cur,
        "drawdown_pct": dd_pct,
        "threshold_pct": float(mdd_limit_pct),
        "floor_equity": floor,
        "reason": reason,
    }


def simulate_liquidation_plan(
    mock_positions: List[Dict[str, Any]],
    *,
    label: str = "MOCK",
) -> List[Dict[str, Any]]:
    """
    실주문 없이 전량 청산 시뮬레이션(표시용). 반환: 정렬된 청산 행 리스트.
    """
    rows: List[Dict[str, Any]] = []
    for p in mock_positions:
        sym = str(p.get("symbol") or p.get("ticker") or "?")
        qty = p.get("qty")
        notion = p.get("notion_usd") or p.get("notional") or p.get("value_krw")
        mkt = p.get("market") or ("KR" if sym.isdigit() else ("COIN" if sym.startswith("KRW-") else "US"))
        rows.append(
            {
                "symbol": sym,
                "qty": qty,
                "notional_hint": notion,
                "market": mkt,
                "action": "SELL_ALL",
            }
        )
    print(f"\n  [{label}] 청산 계획(시뮬, 주문 없음) — {len(rows)}건")
    for r in rows:
        extra = ""
        if r["qty"] is not None:
            extra += f" qty={r['qty']}"
        if r["notional_hint"] is not None:
            extra += f" notional={r['notional_hint']}"
        print(f"    {r['market']:4s} {r['symbol']:12s} {r['action']}{extra}")
    return rows


def run_phase2_twap_lab() -> None:
    """
    Phase 2 샌드박스 — execution/order_twap.py
    · 원화 500만 / 달러 5,000 초과 시 분할 계획
    · 메인 스레드 순차 실행 + threading으로 두 갈래 동시 시뮬(실주문 없음)
    """
    delay = float(os.environ.get("LAB_TWAP_DELAY_SEC", "0").strip() or "0")

    print("\n" + "=" * 72)
    print("[Phase2 샌드박스] TWAP 분할 (슬리피지 방어 — Mock 주문만)")
    print("=" * 72)
    print(f"\n  슬라이스 간 delay = {delay}s (LAB_TWAP_DELAY_SEC)")

    print("\n  --- 원화 분할 계획 (기준 5,000,000 KRW) ---")
    for name, krw in [("소액_300만", 3_000_000), ("딱500만", 5_000_000), ("대액_1800만", 18_000_000)]:
        s = plan_krw_slices(krw)
        print(f"    {name:12s} {krw:>12,.0f} KRW -> {len(s)}슬라이스 {s}")

    print("\n  --- 달러 분할 계획 (기준 5,000 USD) ---")
    for name, usd in [("소액_3k", 3_000.0), ("대액_18k", 18_000.0)]:
        s = plan_usd_slices(usd)
        print(f"    {name:12s} ${usd:>10,.0f} -> {len(s)}슬라이스 {s}")

    print("\n  --- 수량 TWAP 계획 (평가금 기준, Mock 매도) ---")
    for qty, px in [(80, 400_000), (120, 500_000)]:
        notion = qty * px
        ch = plan_sell_qty_twap(qty, float(notion))
        print(f"    qty={qty} @ {px:,} = 평가 {notion:,} -> 청산 수량 슬라이스 {ch}")

    print("\n  --- (A) 메인 스레드 순차: 원화 1,200만 매수 Mock ---")

    def _mock_kis_buy_krw(amt: float) -> bool:
        print(f"    [MOCK KIS 시장가 매수] {amt:,.0f} KRW 전송")
        return True

    slices_a = plan_krw_slices(12_000_000.0)
    ok_a = run_krw_slices(slices_a, _mock_kis_buy_krw, delay_sec=delay)
    print(f"    결과: {'OK' if ok_a else 'FAIL'}")

    print("\n  --- (B) 백그라운드 스레드 2개: KR원화 TWAP + USD TWAP 동시 실행 ---")
    results: Dict[str, Any] = {}

    def _worker_kr() -> None:
        def _ex(x: float) -> bool:
            print(f"    [thread-KR] MOCK {x:,.0f} KRW")
            return True

        results["kr_ok"] = run_krw_slices(plan_krw_slices(10_000_000.0), _ex, delay_sec=delay)

    def _worker_us() -> None:
        def _exu(x: float) -> bool:
            print(f"    [thread-US] MOCK ${x:,.2f}")
            return True

        results["us_ok"] = run_krw_slices(plan_usd_slices(14_000.0), _exu, delay_sec=delay)

    t_kr = threading.Thread(target=_worker_kr, name="twap-kr", daemon=True)
    t_us = threading.Thread(target=_worker_us, name="twap-us", daemon=True)
    t_kr.start()
    t_us.start()
    t_kr.join(timeout=300.0)
    t_us.join(timeout=300.0)
    print(f"    join 완료 | kr_ok={results.get('kr_ok')} us_ok={results.get('us_ok')}")

    print("\n  --- (C) 한 슬라이스 실패 시 중단(Mock) ---")

    def _fail_second(amt: float) -> bool:
        if amt < 4_000_000:
            print(f"    [MOCK] OK slice {amt:,.0f}")
            return True
        print(f"    [MOCK] FAIL slice {amt:,.0f}")
        return False

    ok_c = run_krw_slices(plan_krw_slices(12_000_000.0), _fail_second, delay_sec=0.0)
    print(f"    기대: 중간 실패 -> FAIL / 실제={ok_c}")

    print("\n완료. 운영 이식 전 분할 크기·스레드·콜백 동작을 확인하세요.")


def run_phase5_killswitch_lab() -> None:
    """Phase 5 샌드박스: 시장별 MDD(5% 등) + 합산 서킷(-15% 등) + 쿨다운 상태 + TWAP 분할(계획만)."""
    print("\n" + "=" * 72)
    print("[Phase5-A] 시장별 킬스위치 (test_lab `evaluate_account_killswitch` / guard와 유사 비율)")
    print("=" * 72)

    mdd_pct = float(os.environ.get("LAB_MDD_PCT", "5").strip() or "5")

    print(f"\n  임계: 고점 대비 {mdd_pct:g}% 이상 하락 시 발동 (current < peak * {1 - mdd_pct / 100:.4f})")
    print("\n  --- 시나리오 (Mock) ---")
    scenarios = [
        ("OK_고점갱신", 1_000_000, 1_050_000),
        ("OK_-3pct", 1_000_000, 970_000),
        ("EDGE_-5pct_exact", 1_000_000, 950_000),
        ("TRIP_-5.01pct", 1_000_000, 949_900),
        ("TRIP_-20pct", 1_000_000, 800_000),
    ]
    for name, peak, cur in scenarios:
        ev = evaluate_account_killswitch(peak, cur, mdd_pct)
        flag = "킬스위치 ON" if ev["triggered"] else "정상"
        print(f"    {name:18s} peak={peak:>10,.0f} cur={cur:>10,.0f} -> {flag} | {ev['reason']}")

    mock_pos = [
        {"symbol": "NVDA", "qty": 10, "market": "US"},
        {"symbol": "005930", "qty": 50, "market": "KR"},
        {"symbol": "KRW-BTC", "qty": 0.12, "market": "COIN"},
    ]
    simulate_liquidation_plan(mock_pos, label="킬스위치 발동 시 예시")

    print("\n" + "=" * 72)
    print("[Phase5-B] 합산 계좌 서킷 (`execution/circuit_break` + `execution/guard` 키)")
    print("=" * 72)
    trig = float(os.environ.get("LAB_ACCOUNT_CIRCUIT_PCT", "15").strip() or "15")
    print(f"\n  임계: 합산 고점 대비 {trig:g}% 이상 하락 시 서킷 (current < peak * {1 - trig / 100:.4f})")
    for name, peak, cur in [
        ("합산_OK", 200_000_000, 190_000_000),
        ("합산_서킷", 200_000_000, 160_000_000),
    ]:
        ev2 = evaluate_total_account_circuit(peak, cur, trigger_drawdown_pct=trig)
        print(f"    {name:12s} peak={peak:>12,.0f} cur={cur:>12,.0f} -> triggered={ev2['triggered']} | {ev2['reason']}")

    print("\n  --- USD/KRW (실조회 시도) ---")
    try:
        fx = estimate_usdkrw()
        print(f"    estimate_usdkrw() ≈ {fx:,.2f} KRW/USD")
    except Exception as e:
        print(f"    estimate_usdkrw 실패: {e}")

    print("\n  --- 임시 bot_state + 쿨다운 (파일 I/O Mock) ---")
    tmp = Path(os.environ.get("TMP", os.environ.get("TEMP", "."))) / "lab_phase5_state.json"
    st: Dict[str, Any] = {"positions": {}, "cooldown": {}}
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    st = load_state(tmp)
    update_peak_equity_total_krw(st, 100_000_000.0, tmp)
    st = load_state(tmp)
    print(f"    peak_equity_total_krw = {st.get('peak_equity_total_krw')}")
    set_account_circuit_cooldown(st, tmp, hours=0.001)  # ~3.6초
    st = load_state(tmp)
    print(f"    cooldown_until = {st.get(ACCOUNT_CIRCUIT_COOLDOWN_KEY)} in_cooldown={in_account_circuit_cooldown(st)}")

    print("\n  --- TWAP 분할(청산 시나리오, 주문 없음) ---")
    notion = 18_000_000.0
    print(f"    plan_krw_slices({notion:,.0f}) = {plan_krw_slices(notion)}")
    qty, price = 120, 500_000
    print(f"    plan_sell_qty_twap(qty={qty}, notional={qty * price:,}) = {plan_sell_qty_twap(qty, qty * price)}")

    def _fake_exec(q: int) -> bool:
        print(f"      [MOCK 매도] qty={q}")
        return True

    print("    run_qty_slice_sells (지연 0초):")
    run_qty_slice_sells(plan_sell_qty_twap(100, 12_000_000), _fake_exec, delay_sec=0.0)


def optional_live_yfinance_sector() -> None:
    if os.environ.get("LAB_LIVE_YF", "").strip() != "1":
        return
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        print("\n[LAB_LIVE_YF] yfinance 미설치 — 스킵")
        return
    t = "AAPL"
    info = yf.Ticker(t).info
    sec = info.get("sector") or info.get("industry") or "?"
    print("\n" + "=" * 72)
    print(f"[LAB_LIVE_YF] yfinance 실조회: {t} sector={sec!r}")
    print("=" * 72)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if os.environ.get("LAB_PHASE2_ONLY", "").strip() in ("1", "true", "yes", "on"):
        print("Phase 2 - test_lab.py [LAB_PHASE2_ONLY]")
        run_phase2_twap_lab()
    elif os.environ.get("LAB_PHASE5_ONLY", "").strip() in ("1", "true", "yes", "on"):
        print("Phase 5 - test_lab.py [LAB_PHASE5_ONLY]")
        run_phase5_killswitch_lab()
        print("\n완료. 운영 이식 전 MDD 킬스위치·청산 계획 출력을 확인하세요.")
    elif os.environ.get("LAB_PHASE4_ONLY", "").strip() in ("1", "true", "yes", "on"):
        print("Phase 4 - test_lab.py [LAB_PHASE4_ONLY]")
        run_phase4_macro_guard_lab()
        print("\n완료. 운영 이식 전 Macro 방어막 판단을 확인하세요.")
    elif os.environ.get("LAB_PHASE3_ONLY", "").strip() in ("1", "true", "yes", "on"):
        print("Phase 3 - test_lab.py [LAB_PHASE3_ONLY]")
        run_phase3_ai_filter_lab()
        print("\n완료. 운영 이식 전 False Breakout 확률/차단 결과를 확인하세요.")
    elif os.environ.get("LAB_MOCK_ONLY", "").strip() in ("1", "true", "yes", "on"):
        print("Phase 1 - test_lab.py [LAB_MOCK_ONLY]")
        run_all_mock_scenarios()
        optional_live_yfinance_sector()
        print("\n완료. 실계좌: py -3.11 tests/test_lab.py")
    else:
        print("Phase 1 - test_lab.py [실계좌: US 블록 + KR 블록, 코인 미조회]")
        try:
            if os.environ.get("LAB_SKIP_US", "").strip() not in ("1", "true", "yes", "on"):
                run_phase1_real_lab_us()
            else:
                print("\n[LAB_SKIP_US] 미장 실계좌 블록 생략")
            if os.environ.get("LAB_SKIP_KR", "").strip() not in ("1", "true", "yes", "on"):
                run_phase1_real_lab_kr()
            else:
                print("\n[LAB_SKIP_KR] 국장 실계좌 블록 생략")
        except Exception as e:  # noqa: BLE001
            print(f"\n  실계좌 경로 오류: {e}")
            traceback.print_exc()
            print("\n  Mock만: LAB_MOCK_ONLY=1 py -3.11 tests/test_lab.py")
        print("\n완료.")
    sys.exit(0)
