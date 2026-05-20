# -*- coding: utf-8 -*-
"""
매매내역용 섹터(업종) — Phase1 ``strategy.sector_lock`` 과 **동일 조회·캐시** 재사용.

``get_kr_sector`` / ``get_us_sector`` 를 그대로 호출하므로 차단 로직과 API 중복이 없다.
"""
from __future__ import annotations


def trade_record_key(item: dict) -> str:
    """매매 1건 식별 키 (백필·오버레이 맵용)."""
    ts = str(item.get("timestamp") or "").strip()
    mk = str(item.get("market") or "").strip().upper()
    tk = str(item.get("ticker") or "").strip()
    side = str(item.get("side") or "").strip().upper()
    return f"{ts}|{mk}|{tk}|{side}"


def resolve_trade_sector(market: str, ticker: str) -> str:
    """
    시장·티커 → 섹터 문자열 (``sector_lock`` 위임).

    * KR — ``get_kr_sector`` (``_KR_SECTOR_CACHE``)
    * US — ``get_us_sector`` (``_US_SECTOR_CACHE``)
    * COIN — ``암호화폐`` (섹터락 대상 아님)
    """
    from strategy.sector_lock import get_kr_sector, get_us_sector

    mk = str(market or "").strip().upper()
    tk = str(ticker or "").strip()
    if not tk:
        return ""

    if mk == "COIN" or tk.startswith("KRW-") or tk.startswith("USDT-"):
        return "암호화폐"

    if mk == "KR" or tk.isdigit():
        return get_kr_sector(tk)

    if mk == "US":
        return get_us_sector(tk)

    return "Unknown"


def sector_for_trade_record(item: dict, overlay: dict | None = None) -> str:
    """기록 dict + 선택적 오버레이 맵에서 섹터 문자열."""
    sec = str(item.get("sector") or "").strip()
    if sec:
        try:
            from api.kr_stock_meta import normalize_kr_sector_label

            sec = normalize_kr_sector_label(sec)
        except Exception:
            pass
        return sec
    if overlay:
        key = trade_record_key(item)
        sec = str(overlay.get(key) or overlay.get("by_trade_key", {}).get(key) or "").strip()
        if sec:
            return sec
    mk = str(item.get("market") or "").strip()
    tk = str(item.get("ticker") or "").strip()
    if mk and tk:
        return resolve_trade_sector(mk, tk)
    return ""
