# -*- coding: utf-8 -*-
"""
Phase 1 — 동일 **섹터** 과다 보유 방지 (국장·미장 주식만, 코인 제외).

규칙
    * 후보 종목의 섹터(yfinance ``info``)를 조회해, 이미 장부에 같은 섹터가 몇 개인지 센다.
    * 허용 상한 = ``max(1, (max_positions_XX + 1) // 2)`` — 시장별 최대 포지션의 절반(올림)을 넘기면 신규 매수 거절.
    * 섹터가 ``Unknown`` 이면 **보수적으로 통과**(데이터 부족으로 기회를 막지 않음).

성능
    * 종목별 섹터는 프로세스 단위 메모리 캐시(``_US_SECTOR_CACHE``, ``_KR_SECTOR_CACHE``).
"""
from __future__ import annotations

import time
from typing import Dict, Tuple, Mapping

import yfinance as yf

_US_SECTOR_CACHE: Dict[str, str] = {}
_KR_SECTOR_CACHE: Dict[str, str] = {}


def _norm_symbol(value: str) -> str:
    """티커 비교용 정규화(대문자·앞뒤 공백 제거)."""
    return str(value or "").strip().upper()


def _is_us_stock(symbol: str) -> bool:
    s = _norm_symbol(symbol)
    if not s or s.startswith("KRW-"):
        return False
    if s.isdigit():
        return False
    return True


def _is_kr_stock(symbol: str) -> bool:
    s = str(symbol or "").strip()
    return bool(s) and s.isdigit()


def _extract_sector(info: dict) -> str:
    sector = str((info or {}).get("sector") or (info or {}).get("industry") or "").strip()
    return sector if sector else "Unknown"


def seed_us_sector_cache(sectors: Mapping[str, str]) -> None:
    """``us_universe_cache.json`` 등에서 미리 조회한 GICS 섹터를 프로세스 캐시에 주입한다."""
    for raw_k, raw_v in (sectors or {}).items():
        k = _norm_symbol(str(raw_k).replace(".", "-"))
        v = str(raw_v or "").strip() or "Unknown"
        _US_SECTOR_CACHE[k] = v


def get_us_sector(symbol: str) -> str:
    ticker = _norm_symbol(symbol)
    if ticker in _US_SECTOR_CACHE:
        return _US_SECTOR_CACHE[ticker]
    try:
        info = yf.Ticker(ticker).info or {}
        sector = _extract_sector(info)
    except Exception:
        sector = "Unknown"
    _US_SECTOR_CACHE[ticker] = sector
    return sector


def get_kr_sector(symbol: str) -> str:
    code = str(symbol or "").strip().zfill(6)
    if code in _KR_SECTOR_CACHE:
        return _KR_SECTOR_CACHE[code]

    sector = "Unknown"
    for suffix in (".KS", ".KQ"):
        try:
            info = yf.Ticker(f"{code}{suffix}").info or {}
            sector = _extract_sector(info)
            if sector != "Unknown":
                break
        except Exception:
            continue
        finally:
            time.sleep(0.1)

    _KR_SECTOR_CACHE[code] = sector
    return sector


def allow_us_sector_entry(candidate: str, positions: dict, max_positions_us: int, normalize_ticker) -> Tuple[bool, str]:
    """``(허용 여부, 사람이 읽기 좋은 이유 문자열)`` — 로그·텔레그램에 그대로 실을 수 있다."""
    limit = max(1, (int(max_positions_us) + 1) // 2)
    cand = normalize_ticker(candidate)
    if not _is_us_stock(cand):
        return True, "[SECTOR_LOCK:US] 미국 주식 아님"

    cand_sector = get_us_sector(cand)
    if cand_sector == "Unknown":
        return True, f"[SECTOR_LOCK:US] {cand} 섹터 미확인(Unknown) → 통과"

    same_sector_count = 0
    for raw in (positions or {}).keys():
        key = normalize_ticker(raw)
        if not _is_us_stock(key):
            continue
        sec = get_us_sector(key)
        if sec == "Unknown":
            continue
        if sec == cand_sector:
            same_sector_count += 1

    if same_sector_count >= limit:
        return False, f"[SECTOR_LOCK:US] '{cand_sector}' 보유 {same_sector_count}개 >= 한도 {limit}"
    return True, f"[SECTOR_LOCK:US] '{cand_sector}' 보유 {same_sector_count}개 < 한도 {limit}"


def allow_kr_sector_entry(candidate: str, positions: dict, max_positions_kr: int, normalize_ticker) -> Tuple[bool, str]:
    """국장 6자리 종목에 대해 ``allow_us_sector_entry`` 와 동일 패턴."""
    limit = max(1, (int(max_positions_kr) + 1) // 2)
    cand = normalize_ticker(candidate)
    if not _is_kr_stock(cand):
        return True, "[SECTOR_LOCK:KR] 국장 종목 아님"

    cand_sector = get_kr_sector(cand)
    if cand_sector == "Unknown":
        return True, f"[SECTOR_LOCK:KR] {cand} 섹터 미확인(Unknown) → 통과"

    same_sector_count = 0
    for raw in (positions or {}).keys():
        key = normalize_ticker(raw)
        if not _is_kr_stock(key):
            continue
        sec = get_kr_sector(key)
        if sec == "Unknown":
            continue
        if sec == cand_sector:
            same_sector_count += 1

    if same_sector_count >= limit:
        return False, f"[SECTOR_LOCK:KR] '{cand_sector}' 보유 {same_sector_count}개 >= 한도 {limit}"
    return True, f"[SECTOR_LOCK:KR] '{cand_sector}' 보유 {same_sector_count}개 < 한도 {limit}"
