# -*- coding: utf-8 -*-
"""
국내 종목 메타(종목명·섹터) — KIS → 네이버 순, yfinance 미사용.

yfinance ``.KS`` / ``.KQ`` ``.info`` 는 404 로그·지연을 유발하므로 국장 6자리 코드에는 쓰지 않는다.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

import requests

_NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://m.stock.naver.com/",
}
_NAME_CACHE: Dict[str, str] = {}
_SECTOR_CACHE: Dict[str, str] = {}
_UPJONG_NAME_CACHE: Dict[str, str] = {}


def _zfill_code(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit()).zfill(6)


def _fetch_naver_basic(code: str) -> Dict[str, Any]:
    sym = _zfill_code(code)
    if not sym or sym == "000000":
        return {}
    url = f"https://m.stock.naver.com/api/stock/{sym}/basic"
    try:
        res = requests.get(url, headers=_NAVER_HEADERS, timeout=8.0)
        if res.status_code >= 400:
            return {}
        data = res.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fetch_naver_integration(code: str) -> Dict[str, Any]:
    sym = _zfill_code(code)
    if not sym or sym == "000000":
        return {}
    url = f"https://m.stock.naver.com/api/stock/{sym}/integration"
    try:
        res = requests.get(url, headers=_NAVER_HEADERS, timeout=10.0)
        if res.status_code >= 400:
            return {}
        data = res.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _total_info_value(integration: Dict[str, Any], code_key: str) -> str:
    for item in integration.get("totalInfos") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("code") or "").strip() == code_key:
            val = str(item.get("value") or "").strip()
            if val:
                return val
    return ""


def fetch_kr_name_naver(code: str) -> str:
    basic = _fetch_naver_basic(code)
    name = str(basic.get("stockName") or "").strip()
    return name


def fetch_naver_upjong_name_by_code(industry_code: str) -> str:
    """네이버 업종번호 → 한글 업종명 (finance.naver.com sise_group_detail)."""
    code = "".join(ch for ch in str(industry_code or "") if ch.isdigit())
    if not code or int(code) <= 0:
        return ""
    if code in _UPJONG_NAME_CACHE:
        return _UPJONG_NAME_CACHE[code]

    name = ""
    try:
        url = f"https://finance.naver.com/sise/sise_group_detail.naver?type=upjong&no={code}"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0)
        if res.status_code == 200:
            res.encoding = "euc-kr"
            m = re.search(r"<title>\s*([^:<]+)", res.text or "")
            if m:
                name = str(m.group(1)).strip()
    except Exception:
        name = ""

    if name:
        _UPJONG_NAME_CACHE[code] = name
    return name


def fetch_kr_sector_naver(code: str) -> str:
    """섹터: ETF 기준지수명 우선, 일반주는 네이버 industryCode → 한글 업종명."""
    sym = _zfill_code(code)
    basic = _fetch_naver_basic(sym)
    stock_end = str((basic or {}).get("stockEndType") or "").strip().lower()

    integration = _fetch_naver_integration(sym)
    if integration:
        etf_idx = _total_info_value(integration, "etfBaseIdx")
        if etf_idx:
            return f"ETF:{etf_idx[:48]}"

        issue = _total_info_value(integration, "issueName")
        if issue:
            return f"ETF_ISSUE:{issue[:32]}"

        if stock_end == "etf":
            etf_nm = str((basic or {}).get("stockName") or "").strip()
            if etf_nm:
                return f"ETF:{etf_nm[:48]}"

        ind = integration.get("industryCode")
        if ind is not None and str(ind).strip() != "":
            upjong = fetch_naver_upjong_name_by_code(str(ind).strip())
            if upjong:
                return upjong

    return ""


def fetch_kr_name_kis(broker: Any, code: str) -> str:
    if broker is None:
        return ""
    sym = _zfill_code(code)
    if not sym:
        return ""
    try:
        resp = broker.fetch_price(sym)
        if not isinstance(resp, dict) or str(resp.get("rt_cd", "")) != "0":
            return ""
        out = resp.get("output") or {}
        if not isinstance(out, dict):
            return ""
        for key in ("hts_kor_isnm", "prdt_abrv_name", "prdt_name", "bstp_kor_isnm"):
            name = str(out.get(key) or "").strip()
            if name and name != sym:
                return name
    except Exception:
        pass
    return ""


def fetch_kr_sector_kis(broker: Any, code: str) -> str:
    """KIS 현재가 응답의 업종명(있을 때만)."""
    if broker is None:
        return ""
    sym = _zfill_code(code)
    if not sym:
        return ""
    try:
        resp = broker.fetch_price(sym)
        if not isinstance(resp, dict) or str(resp.get("rt_cd", "")) != "0":
            return ""
        out = resp.get("output") or {}
        if not isinstance(out, dict):
            return ""
        sector = str(out.get("bstp_kor_isnm") or out.get("bstp_kor_issnm") or "").strip()
        return sector
    except Exception:
        pass
    return ""


def resolve_kr_company_name(code: str, *, broker: Any = None) -> str:
    """KIS 종목명 → 네이버 basic → 코드."""
    sym = _zfill_code(code)
    if not sym:
        return str(code or "").strip()

    if sym in _NAME_CACHE:
        return _NAME_CACHE[sym]

    name = fetch_kr_name_kis(broker, sym)
    if not name:
        time.sleep(0.05)
        name = fetch_kr_name_naver(sym)
    if not name:
        name = sym

    _NAME_CACHE[sym] = name
    return name


def normalize_kr_sector_label(sector: str) -> str:
    """장부·매매내역에 남은 ``KR_IND_*`` 레거시 코드를 한글 업종명으로 치환."""
    text = str(sector or "").strip()
    if not text.startswith("KR_IND_"):
        return text
    code = text[7:].lstrip("_")
    name = fetch_naver_upjong_name_by_code(code)
    return name if name else text


def resolve_kr_sector(code: str, *, broker: Any = None) -> str:
    """KIS 업종명 → 네이버 업종명/ETF → Unknown."""
    sym = _zfill_code(code)
    if not sym:
        return "Unknown"

    if sym in _SECTOR_CACHE:
        return _SECTOR_CACHE[sym]

    sector = fetch_kr_sector_kis(broker, sym)
    if not sector:
        time.sleep(0.08)
        sector = fetch_kr_sector_naver(sym)
    if not sector:
        sector = "Unknown"
    else:
        sector = normalize_kr_sector_label(sector)

    _SECTOR_CACHE[sym] = sector
    return sector
