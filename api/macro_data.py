# -*- coding: utf-8 -*-
"""
매크로 지표 수집 (통신/API 전담).

- US: SPY Put/Call OI 비율
- COIN: 바이낸스 상위 트레이더 롱/숏 비율
- KR: 원/달러 Z-Score(20일) + 실시간 spot + 당일 방향
- KR: VKOSPI(코스피200 변동성지수). 한투 업종코드 0503. 실패 시 None. 국장 매수 블락은 20MA 동적 스파이크(현재≥20 & 현재>20MA×1.3)

정책(차단)은 `strategy/macro_guard.py`에서 처리.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, timedelta
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)

USD_KRW_YF_SYMBOLS = ("USDKRW=X", "KRW=X")
USD_KRW_ETF_PROXY_CODE = "261240"
USD_KRW_ETF_YF_SYMBOL = "261240.KS"


def _yf_history_close_series(symbol: str, period: str = "10d"):
    try:
        import yfinance as yf  # type: ignore

        hist = yf.Ticker(symbol).history(period=period)
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        return hist["Close"]
    except Exception:
        return None


def _normalize_usdkrw_rate(raw: float) -> float:
    """yfinance KRW=X(역수) / USDKRW=X 를 KRW/USD 로 통일."""
    v = float(raw or 0)
    if v <= 0:
        return 0.0
    if v < 100.0:
        return 1.0 / v
    return v


def _daily_usdkrw_closes(min_bars: int = 21) -> tuple[Optional[Any], str]:
    """일봉 종가 시리즈(KRW/USD). 최소 ``min_bars`` 확보 시 반환."""
    for symbol in USD_KRW_YF_SYMBOLS:
        closes = _yf_history_close_series(symbol, period="3mo")
        if closes is None or closes.empty:
            continue
        try:
            normed = closes.apply(_normalize_usdkrw_rate)
            normed = normed[normed > 0]
            if len(normed) < min_bars:
                continue
            return normed, symbol
        except Exception:
            continue
    return None, ""


def _yf_intraday_spot_usdkrw() -> Optional[float]:
    """yfinance 1분봉 최신가 — 지연 일봉 종가 대신 실시간 근사."""
    for symbol in USD_KRW_YF_SYMBOLS:
        try:
            import yfinance as yf  # type: ignore

            hist = yf.Ticker(symbol).history(period="1d", interval="1m")
            if hist is None or hist.empty or "Close" not in hist.columns:
                continue
            raw = float(hist["Close"].dropna().iloc[-1])
            spot = _normalize_usdkrw_rate(raw)
            if spot > 0:
                return spot
        except Exception as e:
            log.debug("USDKRW 1m spot 실패 (%s): %s", symbol, e)
    return None


def _kis_etf_proxy_spot(*, prev_fx: float) -> Optional[float]:
    """
    KODEX 미국달러선물(261240) 당일 변동률로 전일 환율 종가를 스케일.

    ETF 가격 자체는 KRW/USD 가 아니므로, 전일 ETF 종가 대비 현재가 비율만 사용한다.
    """
    if prev_fx <= 0:
        return None
    etf_prev = _etf_last_settled_close()
    if etf_prev is None or etf_prev <= 0:
        return None
    etf_now = _kis_etf_live_price()
    if etf_now is None or etf_now <= 0:
        return None
    return float(prev_fx) * (float(etf_now) / float(etf_prev))


def _etf_last_settled_close() -> Optional[float]:
    try:
        import yfinance as yf  # type: ignore

        hist = yf.Ticker(USD_KRW_ETF_YF_SYMBOL).history(period="10d")
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        return float(closes.iloc[-1])
    except Exception as e:
        log.debug("261240 일봉 종가 실패: %s", e)
        return None


def _kis_etf_live_price() -> Optional[float]:
    try:
        from api import kis_api

        broker = getattr(kis_api, "broker_kr", None)
        if broker is None:
            return None
        resp = broker.fetch_price(USD_KRW_ETF_PROXY_CODE)
        if not isinstance(resp, dict) or str(resp.get("rt_cd", "")) != "0":
            return None
        out = resp.get("output") if isinstance(resp.get("output"), dict) else {}
        px = float(out.get("stck_prpr", 0) or 0)
        return px if px > 0 else None
    except Exception as e:
        log.debug("KIS 261240 실시간 실패: %s", e)
        return None


def _fetch_realtime_usdkrw_spot(*, prev_fx: float) -> tuple[Optional[float], str]:
    """
    실시간 USD/KRW spot.

    우선순위: yfinance 1분봉 → KIS 261240 비율 프록시.
    """
    spot = _yf_intraday_spot_usdkrw()
    if spot is not None and spot > 0:
        return spot, "USDKRW_1m"
    proxy = _kis_etf_proxy_spot(prev_fx=prev_fx)
    if proxy is not None and proxy > 0:
        return proxy, f"ETF_{USD_KRW_ETF_PROXY_CODE}"
    return None, ""


def fetch_us_put_call_ratio(symbol: str = "SPY") -> Optional[float]:
    """SPY 최근 만기 옵션 Put/Call OI 비율. 실패 시 None."""
    sym = str(symbol or "SPY").strip().upper() or "SPY"
    try:
        import pandas as pd  # type: ignore
        import yfinance as yf  # type: ignore

        tk = yf.Ticker(sym)
        expiries = list(getattr(tk, "options", []) or [])
        if not expiries:
            return None
        expiry = sorted(expiries)[0]
        chain = tk.option_chain(expiry)
        calls = chain.calls if hasattr(chain, "calls") else pd.DataFrame()
        puts = chain.puts if hasattr(chain, "puts") else pd.DataFrame()
        call_oi = 0.0
        put_oi = 0.0
        if isinstance(calls, pd.DataFrame) and not calls.empty and "openInterest" in calls.columns:
            call_oi = float(pd.to_numeric(calls["openInterest"], errors="coerce").fillna(0).sum())
        if isinstance(puts, pd.DataFrame) and not puts.empty and "openInterest" in puts.columns:
            put_oi = float(pd.to_numeric(puts["openInterest"], errors="coerce").fillna(0).sum())
        if call_oi <= 0 and put_oi <= 0:
            return None
        return float(put_oi / max(call_oi, 1.0))
    except Exception:
        return None


def fetch_coin_whale_short_ratio(symbol: str = "BTCUSDT", period: str = "1d") -> Optional[float]:
    """바이낸스 선물 상위 트레이더 BTCUSDT 롱/숏 비율(1d). 실패 시 None."""
    sym = str(symbol or "BTCUSDT").strip().upper() or "BTCUSDT"
    url = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
    params = {"symbol": sym, "period": str(period or "1d"), "limit": 1}
    try:
        res = requests.get(url, params=params, timeout=10.0)
        if res.status_code >= 400:
            return None
        rows = res.json()
        if not isinstance(rows, list) or not rows:
            return None
        latest = rows[-1] if isinstance(rows[-1], dict) else {}
        ratio = float(latest.get("longShortRatio", 0) or 0)
        return ratio if ratio > 0 else None
    except Exception:
        return None


def fetch_usd_krw_momentum() -> Optional[Dict[str, float | bool | str]]:
    """
    원/달러 동적 변동성(Z-Score) + 실시간 spot + 당일 방향.

    * ``ma20`` / ``std20``: 최근 20거래일 일봉 종가
    * ``prev_spot``: 직전 일봉 종가(전일 환율)
    * ``spot``: 1분봉 또는 261240 프록시 (지연 일봉 종가 사용 안 함)
    * ``z_score``: (spot - ma20) / std20
    * ``is_rising``: spot > prev_spot
    """
    closes, symbol = _daily_usdkrw_closes(min_bars=21)
    if closes is None or closes.empty:
        return None

    window = closes.tail(20)
    ma20 = float(window.mean())
    std20 = float(window.std(ddof=0))
    prev_spot = float(closes.iloc[-1])
    if ma20 <= 0 or prev_spot <= 0 or std20 <= 0:
        return None

    spot, spot_src = _fetch_realtime_usdkrw_spot(prev_fx=prev_spot)
    if spot is None or spot <= 0:
        return None

    z_score = float((spot - ma20) / std20)
    is_rising = bool(spot > prev_spot)
    return {
        "z_score": z_score,
        "is_rising": is_rising,
        "spot": float(spot),
        "ma20": ma20,
        "std20": std20,
        "prev_spot": prev_spot,
        "symbol": symbol,
        "spot_source": spot_src,
    }


def fetch_macro_raw() -> Dict[str, Any]:
    """글로벌 알파 원시 지표. 실패 필드는 None."""
    pcr = fetch_us_put_call_ratio()
    whale = fetch_coin_whale_short_ratio()
    fx = fetch_usd_krw_momentum()
    fx_dict = fx if isinstance(fx, dict) else {}
    return {
        "us_put_call_ratio": pcr,
        "coin_whale_long_short_ratio": whale,
        "usd_krw_fx": fx_dict or None,
        "usd_krw_z_score": fx_dict.get("z_score"),
        "usd_krw_is_rising": fx_dict.get("is_rising"),
        "usd_krw_spot": fx_dict.get("spot"),
        "usd_krw_ma20": fx_dict.get("ma20"),
        "usd_krw_symbol": fx_dict.get("symbol"),
    }


VKOSPI_MIN = 1.0
VKOSPI_MAX = 150.0
VKOSPI_KIS_ISCD = "0503"  # 한투 idxcode.mst 업종코드 (VKOSPI)
VKOSPI_YF_SYMBOL = "^KSVK"
VKOSPI_MA_WINDOW = 20
VKOSPI_SPIKE_MA_MULT = 1.3
VKOSPI_SPIKE_MIN_LEVEL = 20.0
_VKOSPI_CACHE_TTL_SEC = 600.0
_vkospi_cache: tuple[float, float] | None = None  # (monotonic, value)
_vkospi_series_cache: tuple[float, list[float]] | None = None  # (monotonic, closes asc)

_HTTP_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def _coerce_vkospi(raw: Any) -> Optional[float]:
    """VKOSPI 정상 범위(1~150)만 채택. KOSPI 지수값 오인 방지."""
    try:
        v = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if v < VKOSPI_MIN or v > VKOSPI_MAX:
        return None
    return v


def _vkospi_from_kis() -> Optional[float]:
    """한투 국내업종 현재지수(U/0503). 봇 기동 후 broker/토큰이 있을 때만."""
    try:
        from api import kis_api

        broker = getattr(kis_api, "broker_kr", None)
        cfg = getattr(kis_api, "_cfg", None)
        token = getattr(kis_api, "KIS_TOKEN", None)
        if not token and broker is not None:
            token = getattr(broker, "access_token", None)
        token = str(token or "").replace("Bearer ", "").strip()
        appkey = ""
        appsecret = ""
        if isinstance(cfg, dict):
            appkey = str(cfg.get("kis_key") or "")
            appsecret = str(cfg.get("kis_secret") or "")
        if not appkey and broker is not None:
            appkey = str(getattr(broker, "api_key", "") or "")
            appsecret = str(getattr(broker, "api_secret", "") or "")
        if not token or not appkey or not appsecret:
            return None
        base_url = str(getattr(broker, "base_url", "") or "").rstrip("/")
        if not base_url:
            base_url = "https://openapi.koreainvestment.com:9443"
        url = f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-index-price"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "appkey": appkey,
            "appsecret": appsecret,
            "tr_id": "FHPUP02100000",
            "custtype": "P",
        }
        params = {
            "fid_cond_mrkt_div_code": "U",
            "fid_input_iscd": VKOSPI_KIS_ISCD,
        }
        res = requests.get(url, headers=headers, params=params, timeout=8.0)
        if res.status_code >= 400:
            return None
        body = res.json() if hasattr(res, "json") else {}
        if not isinstance(body, dict) or str(body.get("rt_cd", "")) != "0":
            return None
        out = body.get("output")
        if isinstance(out, list) and out and isinstance(out[0], dict):
            out = out[0]
        if not isinstance(out, dict):
            return None
        return _coerce_vkospi(out.get("bstp_nmix_prpr"))
    except Exception:
        return None


def _vkospi_from_krx() -> Optional[float]:
    """KRX 파생·기타지수(MDCSTAT01201, idxIndCd=300). 로그인/WAF 실패 시 None."""
    try:
        end = date.today()
        start = end - timedelta(days=14)
        payload = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT01201",
            "locale": "ko_KR",
            "indTpCd": "1",
            "idxIndCd": "300",
            "idxCd2": "300",
            "strtDd": start.strftime("%Y%m%d"),
            "endDd": end.strftime("%Y%m%d"),
            "csvxls_isNo": "false",
        }
        res = requests.post(
            "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
            headers={
                **_HTTP_UA,
                "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
            },
            data=payload,
            timeout=8.0,
        )
        if res.status_code >= 400:
            return None
        body = res.json()
        rows = body.get("output") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        last = rows[0] if isinstance(rows[0], dict) else {}
        return _coerce_vkospi(last.get("CLSPRC_IDX") or last.get("clsprc_idx"))
    except Exception:
        return None


def fetch_vkospi() -> Optional[float]:
    """코스피200 변동성지수(VKOSPI). 실패 시 None (예외 없음).

    우선순위: 한투 업종 0503 → KRX 파생지수 JSON.
    현물 실현변동성 같은 대용치는 쓰지 않는다.
    """
    global _vkospi_cache
    now = time.monotonic()
    cached = _vkospi_cache
    if cached is not None:
        ts, val = cached
        if now - ts < _VKOSPI_CACHE_TTL_SEC and val is not None:
            return val
    for getter in (_vkospi_from_kis, _vkospi_from_krx):
        try:
            val = getter()
        except Exception:
            val = None
        if val is not None:
            _vkospi_cache = (now, val)
            return val
    return None


def _vkospi_closes_from_yf(need: int = 30) -> list[float]:
    """yfinance ``^KSVK`` 일봉 종가 (오름차순). 실패 시 []."""
    try:
        import yfinance as yf  # type: ignore

        # 영업일 ~30 + 여유
        period = "3mo" if need >= 20 else "1mo"
        hist = yf.Ticker(VKOSPI_YF_SYMBOL).history(period=period)
        if hist is None or hist.empty or "Close" not in hist.columns:
            return []
        out: list[float] = []
        for raw in hist["Close"].dropna().tolist():
            v = _coerce_vkospi(raw)
            if v is not None:
                out.append(v)
        return out[-max(need, VKOSPI_MA_WINDOW) :] if out else []
    except Exception:
        return []


def _vkospi_closes_from_krx(need: int = 30) -> list[float]:
    """KRX MDCSTAT01201 일별 종가 (오름차순). 실패 시 []."""
    try:
        end = date.today()
        start = end - timedelta(days=max(90, need * 3))
        payload = {
            "bld": "dbms/MDC/STAT/standard/MDCSTAT01201",
            "locale": "ko_KR",
            "indTpCd": "1",
            "idxIndCd": "300",
            "idxCd2": "300",
            "strtDd": start.strftime("%Y%m%d"),
            "endDd": end.strftime("%Y%m%d"),
            "csvxls_isNo": "false",
        }
        res = requests.post(
            "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
            headers={
                **_HTTP_UA,
                "Referer": "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd",
            },
            data=payload,
            timeout=8.0,
        )
        if res.status_code >= 400:
            return []
        body = res.json()
        rows = body.get("output") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            return []
        dated: list[tuple[str, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ds = str(row.get("TRD_DD") or row.get("trd_dd") or "")
            v = _coerce_vkospi(row.get("CLSPRC_IDX") or row.get("clsprc_idx"))
            if v is None:
                continue
            dated.append((ds, v))
        if not dated:
            return []
        dated.sort(key=lambda x: x[0])
        return [v for _, v in dated][-max(need, VKOSPI_MA_WINDOW) :]
    except Exception:
        return []


def fetch_vkospi_daily_closes(need: int = 30) -> list[float]:
    """VKOSPI 최근 일봉 종가 리스트(오름차순). 실패 시 [] (예외 없음)."""
    global _vkospi_series_cache
    need_n = max(int(need or 30), VKOSPI_MA_WINDOW)
    now = time.monotonic()
    cached = _vkospi_series_cache
    if cached is not None:
        ts, closes = cached
        if now - ts < _VKOSPI_CACHE_TTL_SEC and len(closes) >= min(5, need_n):
            return list(closes)
    for getter in (_vkospi_closes_from_yf, _vkospi_closes_from_krx):
        try:
            closes = getter(need_n)
        except Exception:
            closes = []
        if closes:
            _vkospi_series_cache = (now, closes)
            return list(closes)
    return []


def vkospi_ma20(closes: list[float] | None = None) -> Optional[float]:
    """종가 시리즈로 20일(또는 가용분) 단순이동평균. 실패 시 None."""
    try:
        series = list(closes) if closes is not None else fetch_vkospi_daily_closes(30)
        if not series:
            return None
        window = series[-VKOSPI_MA_WINDOW:] if len(series) >= 1 else []
        if not window:
            return None
        avg = sum(window) / float(len(window))
        if not math.isfinite(avg) or avg <= 0:
            return None
        return float(avg)
    except Exception:
        return None


def vkospi_dynamic_spike_state() -> Optional[Dict[str, float]]:
    """
    동적 변동성 스파이크 판정용 수치.

    Returns:
        ``{"current": float, "ma20": float}`` 또는 데이터 부족 시 None.
        예외는 삼키고 None.
    """
    try:
        closes = fetch_vkospi_daily_closes(30)
        ma = vkospi_ma20(closes)
        if ma is None or ma <= 0:
            return None
        current = fetch_vkospi()
        if current is None and closes:
            current = closes[-1]
        if current is None:
            return None
        return {"current": float(current), "ma20": float(ma)}
    except Exception:
        return None


def vkospi_is_dynamic_spike(
    current: float,
    ma20: float,
    *,
    mult: float = VKOSPI_SPIKE_MA_MULT,
    min_level: float = VKOSPI_SPIKE_MIN_LEVEL,
) -> bool:
    """현재 > 20MA×mult 이고 현재 ≥ min_level 이면 단기 급등."""
    try:
        cur = float(current)
        ma = float(ma20)
        if not math.isfinite(cur) or not math.isfinite(ma) or ma <= 0:
            return False
        return cur >= float(min_level) and cur > ma * float(mult)
    except Exception:
        return False
