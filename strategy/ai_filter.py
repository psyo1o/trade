# -*- coding: utf-8 -*-
"""
Phase 3 — **휩쏘(가짜 돌파)** 에 가까운 신호를 걸러낸다.

우선순위
    1. 설정·환경변수에 따라 외부 LLM(OpenAI/Gemini) 호출을 시도할 수 있다.
    2. 실패·비활성 시 **룰베이스** 점수(15m 캔들 패턴 + 호가 불균형)로 대체한다.

데이터
    * 주식: yfinance 분봉, 코인: pyupbit + (가능하면) 브로커/업비트 호가 요약.

반환 형태는 ``evaluate_false_breakout_filter`` 의 docstring 참고.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

import requests
import yfinance as yf
import pyupbit


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        return float(v)
    except Exception:
        return float(default)


def _get_secret(key_name: str, config: dict | None = None) -> str:
    env_val = os.environ.get(key_name, "").strip()
    if env_val:
        return env_val
    if isinstance(config, dict):
        cfg_val = str(config.get(key_name, "") or "").strip()
        if cfg_val:
            return cfg_val
    return ""


def get_recent_15m_ohlcv(ticker: str, market: str, count: int = 10) -> List[Dict[str, float]]:
    """Get recent 15m candles as list[o,h,l,c,v]."""
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return []

    if market.upper() == "COIN" or symbol.startswith("KRW-"):
        try:
            df = pyupbit.get_ohlcv(symbol, interval="minute15", count=count)
            if df is not None and not df.empty:
                rows = []
                for _, r in df.tail(count).iterrows():
                    rows.append(
                        {
                            "o": _to_float(r.get("open", 0.0)),
                            "h": _to_float(r.get("high", 0.0)),
                            "l": _to_float(r.get("low", 0.0)),
                            "c": _to_float(r.get("close", 0.0)),
                            "v": _to_float(r.get("volume", 0.0)),
                        }
                    )
                return rows
        except Exception:
            return []

    candidates: List[str]
    if market.upper() == "KR" and symbol.isdigit():
        code = symbol.zfill(6)
        candidates = [f"{code}.KS", f"{code}.KQ"]
    else:
        candidates = [symbol]

    for cand in candidates:
        try:
            df = yf.Ticker(cand).history(interval="15m", period="5d")
            if df is None or df.empty:
                continue
            rows = []
            for _, r in df.tail(count).iterrows():
                rows.append(
                    {
                        "o": _to_float(r.get("Open", 0.0)),
                        "h": _to_float(r.get("High", 0.0)),
                        "l": _to_float(r.get("Low", 0.0)),
                        "c": _to_float(r.get("Close", 0.0)),
                        "v": _to_float(r.get("Volume", 0.0)),
                    }
                )
            return rows
        except Exception:
            continue
    return []


def _collect_orderbook_qtys(data: Any) -> Tuple[float, float]:
    """Recursively collect bid/ask quantity-like numeric values."""
    bid_total = 0.0
    ask_total = 0.0

    def walk(node: Any):
        nonlocal bid_total, ask_total
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k).lower()
                if isinstance(v, (dict, list)):
                    walk(v)
                    continue
                num = _to_float(v, 0.0)
                if num <= 0:
                    continue
                is_qty = any(x in key for x in ("qty", "qnty", "volume", "vol", "rsqn", "tot", "sum"))
                if not is_qty:
                    continue
                if "bid" in key or "bids" in key:
                    bid_total += num
                if "ask" in key or "asks" in key or "offer" in key:
                    ask_total += num
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return bid_total, ask_total


def get_orderbook_summary_from_broker(broker: Any, ticker: str) -> Dict[str, float]:
    """Try broker.fetch_price and summarize bid/ask depth totals."""
    if broker is None:
        return {"bid_size_total": 0.0, "ask_size_total": 0.0}
    try:
        resp = broker.fetch_price(str(ticker or "").strip().upper())
    except Exception:
        return {"bid_size_total": 0.0, "ask_size_total": 0.0}

    bid, ask = _collect_orderbook_qtys(resp)
    return {"bid_size_total": bid, "ask_size_total": ask}


def get_orderbook_summary_for_coin(ticker: str) -> Dict[str, float]:
    """Upbit orderbook summary for KRW-* tickers."""
    symbol = str(ticker or "").strip().upper()
    if not symbol.startswith("KRW-"):
        return {"bid_size_total": 0.0, "ask_size_total": 0.0}
    try:
        ob = pyupbit.get_orderbook(symbol)
        units = []
        if isinstance(ob, list) and ob:
            units = ob[0].get("orderbook_units", [])
        elif isinstance(ob, dict):
            units = ob.get("orderbook_units", [])
        bid_total = 0.0
        ask_total = 0.0
        for u in units or []:
            bid_total += _to_float(u.get("bid_size", 0.0))
            ask_total += _to_float(u.get("ask_size", 0.0))
        return {"bid_size_total": bid_total, "ask_size_total": ask_total}
    except Exception:
        return {"bid_size_total": 0.0, "ask_size_total": 0.0}


def _rule_based_false_breakout_prob(candles_15m_10: List[Dict[str, float]], orderbook: Dict[str, float]) -> int:
    if not candles_15m_10 or len(candles_15m_10) < 3:
        return 50

    recent = candles_15m_10[-3:]
    highs = [_to_float(c.get("h", 0.0)) for c in candles_15m_10]
    closes = [_to_float(c.get("c", 0.0)) for c in candles_15m_10]
    volumes = [_to_float(c.get("v", 0.0)) for c in candles_15m_10]

    max_recent_high = max(_to_float(c.get("h", 0.0)) for c in recent)
    last_close = closes[-1]
    avg_vol = sum(volumes[:-1]) / max(1, len(volumes) - 1)
    last_vol = volumes[-1]

    body = abs(_to_float(recent[-1].get("c", 0.0)) - _to_float(recent[-1].get("o", 0.0)))
    upper_wick = _to_float(recent[-1].get("h", 0.0)) - max(
        _to_float(recent[-1].get("c", 0.0)),
        _to_float(recent[-1].get("o", 0.0)),
    )
    wick_ratio = upper_wick / max(1e-9, body + upper_wick)

    resistance = max(highs[:-1]) if len(highs) > 1 else highs[-1]
    failed_break = 1 if (max_recent_high >= resistance and last_close < resistance) else 0

    bid = _to_float(orderbook.get("bid_size_total", 0.0))
    ask = _to_float(orderbook.get("ask_size_total", 0.0))
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


def _openai_prob(
    candles_15m_10: List[Dict[str, float]],
    orderbook: Dict[str, float],
    config: dict | None,
    model_name: str = "gpt-4o-mini",
) -> Tuple[int, str]:
    api_key = _get_secret("OPENAI_API_KEY", config)
    if not api_key:
        return _rule_based_false_breakout_prob(candles_15m_10, orderbook), "fallback:OPENAI_API_KEY 없음"

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return _rule_based_false_breakout_prob(candles_15m_10, orderbook), "fallback:openai 패키지 없음"

    client = OpenAI(api_key=api_key)
    prompt = (
        "당신은 기관 퀀트 트레이더다. 아래 15분봉 10개와 호가 요약을 보고 "
        "False Breakout(가짜 돌파) 함정 확률을 0~100 정수로 평가하라.\n"
        "출력은 JSON만: {\"false_breakout_prob\": <int>, \"rationale\": \"짧은 한국어\"}\n"
        f"candles={candles_15m_10}\n"
        f"orderbook={orderbook}\n"
    )
    try:
        resp = client.responses.create(model=model_name, input=prompt, temperature=0.1)
        text = (resp.output_text or "").strip()
        import json

        payload = json.loads(text)
        prob = int(payload.get("false_breakout_prob", 50))
        reason = str(payload.get("rationale", "no_rationale"))
        return max(0, min(100, prob)), reason
    except Exception as e:
        return _rule_based_false_breakout_prob(candles_15m_10, orderbook), f"fallback:{type(e).__name__}"


def _gemini_prob(
    candles_15m_10: List[Dict[str, float]],
    orderbook: Dict[str, float],
    config: dict | None,
    model_name: str = "gemini-2.5-flash",
) -> Tuple[int, str]:
    api_key = _get_secret("GOOGLE_API_KEY", config)
    if not api_key:
        return _rule_based_false_breakout_prob(candles_15m_10, orderbook), "fallback:GOOGLE_API_KEY 없음"

    prompt = (
        "당신은 기관 퀀트 트레이더다. 아래 15분봉 10개와 호가 요약을 보고 "
        "False Breakout(가짜 돌파) 함정 확률을 0~100 정수로 평가하라.\n"
        "출력은 JSON만: {\"false_breakout_prob\": <int>, \"rationale\": \"짧은 한국어\"}\n"
        f"candles={candles_15m_10}\n"
        f"orderbook={orderbook}\n"
    )

    model_candidates = [model_name, "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash"]
    last_err = ""
    for mdl in model_candidates:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={api_key}"
            body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1}}
            resp = requests.post(url, json=body, timeout=20)
            if resp.status_code == 404:
                last_err = f"{mdl}:404"
                continue
            if resp.status_code >= 400:
                last_err = f"{mdl}:{resp.status_code}"
                continue
            data = resp.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
            text = str(parts[0].get("text", "") if parts else "").strip()
            if not text:
                continue
            import json
            import re

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


def evaluate_false_breakout_filter(
    ticker: str,
    candles_15m_10: List[Dict[str, float]],
    orderbook: Dict[str, float],
    threshold: int = 70,
    use_ai: bool = True,
    ai_provider: str = "gemini",
    config: dict | None = None,
) -> Dict[str, Any]:
    """Return dict with probability and block decision."""
    provider = str(ai_provider or "gemini").strip().lower()
    if not use_ai:
        prob = _rule_based_false_breakout_prob(candles_15m_10, orderbook)
        rationale = "rule_based"
        provider_used = "rule_based"
    elif provider == "openai":
        prob, rationale = _openai_prob(candles_15m_10, orderbook, config)
        provider_used = "openai"
    else:
        prob, rationale = _gemini_prob(candles_15m_10, orderbook, config)
        provider_used = "gemini"

    blocked = prob >= int(threshold)
    return {
        "ticker": ticker,
        "false_breakout_prob": prob,
        "threshold": int(threshold),
        "blocked": blocked,
        "rationale": rationale,
        "provider": provider_used,
    }

