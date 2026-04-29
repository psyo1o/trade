# -*- coding: utf-8 -*-
"""
Phase 3 — 전략별 **휩쏘·하락 함정** 필터.

* ``TREND_V8``: 15분봉 + 호가 → 가짜 돌파·펌프 의심도(0~100).
* ``SWING_FIB``: 일봉(10~15개) + 호가 → 칼날·데드캣·좀비 의심도(0~100).

LLM 실패 시 전략별 **룰베이스**로 대체하며, 반환 dict에 산출 경로를 포함한다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pyupbit
import requests
import yfinance as yf


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        return float(v)
    except Exception:
        return float(default)


_ROOT_AI_KEYS_CACHE: Dict[str, str] | None = None


def _load_root_ai_keys_file() -> Dict[str, str]:
    """프로젝트 루트 ``ai_keys.txt`` — ``KEY=value`` 한 줄씩 (``tests/ai_keys.txt`` 와 동일 형식)."""
    global _ROOT_AI_KEYS_CACHE
    if _ROOT_AI_KEYS_CACHE is not None:
        return _ROOT_AI_KEYS_CACHE
    _ROOT_AI_KEYS_CACHE = {}
    key_file = Path(__file__).resolve().parents[1] / "ai_keys.txt"
    if not key_file.is_file():
        return _ROOT_AI_KEYS_CACHE
    try:
        for line in key_file.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            _ROOT_AI_KEYS_CACHE[k.strip()] = v.strip().strip("'\"")
    except Exception:
        _ROOT_AI_KEYS_CACHE = {}
    return _ROOT_AI_KEYS_CACHE


def _get_secret(key_name: str, config: dict | None = None) -> str:
    env_val = os.environ.get(key_name, "").strip()
    if env_val:
        return env_val
    if isinstance(config, dict):
        cfg_val = str(config.get(key_name, "") or "").strip()
        if cfg_val:
            return cfg_val
    file_val = _load_root_ai_keys_file().get(key_name, "").strip()
    if file_val:
        return file_val
    return ""


def normalize_ai_strategy_type(strategy_type: str | None) -> str:
    s = str(strategy_type or "TREND_V8").strip().upper()
    return "SWING_FIB" if s == "SWING_FIB" else "TREND_V8"


def get_recent_15m_ohlcv(ticker: str, market: str, count: int = 10) -> List[Dict[str, float]]:
    """최근 15분봉 ``count``개 → ``[{o,h,l,c,v}, ...]``."""
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


def get_recent_daily_ohlcv(ticker: str, market: str, count: int = 15) -> List[Dict[str, float]]:
    """최근 일봉 ``count``개(기본 15) → ``[{o,h,l,c,v}, ...]``. 스윙 AI 필터용."""
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return []
    n = max(5, min(30, int(count)))

    if market.upper() == "COIN" or symbol.startswith("KRW-"):
        try:
            df = pyupbit.get_ohlcv(symbol, interval="day", count=n)
            if df is not None and not df.empty:
                rows = []
                for _, r in df.tail(n).iterrows():
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
            df = yf.Ticker(cand).history(interval="1d", period="400d")
            if df is None or df.empty:
                continue
            rows = []
            for _, r in df.tail(n).iterrows():
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
    if broker is None:
        return {"bid_size_total": 0.0, "ask_size_total": 0.0}
    try:
        resp = broker.fetch_price(str(ticker or "").strip().upper())
    except Exception:
        return {"bid_size_total": 0.0, "ask_size_total": 0.0}

    bid, ask = _collect_orderbook_qtys(resp)
    return {"bid_size_total": bid, "ask_size_total": ask}


def get_orderbook_summary_for_coin(ticker: str) -> Dict[str, float]:
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


def _rule_based_trend_v8(candles: List[Dict[str, float]], orderbook: Dict[str, float]) -> int:
    """15m 전제: 윗꼬리·거래량 급감·실패 돌파·호가 역전 가중."""
    if not candles or len(candles) < 3:
        return 50

    recent = candles[-3:]
    highs = [_to_float(c.get("h", 0.0)) for c in candles]
    closes = [_to_float(c.get("c", 0.0)) for c in candles]
    volumes = [_to_float(c.get("v", 0.0)) for c in candles]

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


def _rule_based_swing_fib(daily: List[Dict[str, float]], orderbook: Dict[str, float]) -> int:
    """일봉 전제: 연속 음봉·저점 이탈·거래량 고갈·매도벽 가중."""
    if not daily or len(daily) < 5:
        return 50

    closes = [_to_float(c.get("c", 0.0)) for c in daily]
    opens = [_to_float(c.get("o", 0.0)) for c in daily]
    lows = [_to_float(c.get("l", 0.0)) for c in daily]
    vols = [_to_float(c.get("v", 0.0)) for c in daily]

    score = 30

    consec_red = 0
    for i in range(len(daily) - 1, -1, -1):
        if closes[i] < opens[i]:
            consec_red += 1
        else:
            break
    score += min(28, consec_red * 7)

    if len(lows) >= 5:
        recent_low = lows[-1]
        base = lows[-6:-1] if len(lows) >= 6 else lows[:-1]
        prior_min = min(base) if base else recent_low
        if prior_min > 0 and recent_low < prior_min * 0.995:
            score += 18

    avg_vol = sum(vols[:-1]) / max(1, len(vols) - 1)
    last_vol = vols[-1]
    if avg_vol > 0 and last_vol < avg_vol * 0.38:
        score += 22

    if len(closes) >= 4:
        if closes[-1] < closes[-2] < closes[-3]:
            score += 14

    bid = _to_float(orderbook.get("bid_size_total", 0.0))
    ask = _to_float(orderbook.get("ask_size_total", 0.0))
    imbalance = (bid - ask) / max(1e-9, bid + ask)
    if imbalance < -0.14:
        score += 12

    return max(0, min(100, int(round(score))))


def _rule_based_by_strategy(
    candles: List[Dict[str, float]],
    orderbook: Dict[str, float],
    strategy_type: str,
) -> int:
    st = normalize_ai_strategy_type(strategy_type)
    if st == "SWING_FIB":
        return _rule_based_swing_fib(candles, orderbook)
    return _rule_based_trend_v8(candles, orderbook)


# 봇 임계(ai_false_breakout_threshold 등)와 채점 일치를 위해 LLM 프롬프트에 동일 주입
_FALSE_BREAKOUT_SCORE_RUBRIC = """
[필수 지시사항: 0~100점 위험도(prob) 절대 평가 가이드]
점수 산정 시 다음의 기준을 엄격하게 따르세요. (높을수록 위험함)

0 ~ 30점 (안전): 강력한 펀더멘털이나 수급이 뒷받침된 완벽한 진짜 돌파 또는 찐바닥 다지기. (매수 적극 권장)

31 ~ 69점 (보통): 시장의 일반적인 노이즈나 약간의 윗꼬리가 있지만, 상승/반등 추세 자체는 살아있는 상태. (주식/코인 모두 통과 구간)

70 ~ 79점 (경고): 호가창 허수나 거래량 급감 등 휩소(가짜 돌파) 징후가 뚜렷함. (주식 시장에서는 여기서부터 매수를 차단해야 하는 레벨)

80 ~ 100점 (치명적 위험): 악질적인 세력의 펌프 앤 덤프(설거지), 또는 지지선이 완전히 붕괴된 떨어지는 칼날. (모든 시장에서 무조건 진입 차단)
""".strip()


def _build_llm_prompt_v8(candles: List[Dict[str, float]], orderbook: Dict[str, float]) -> str:
    return (
        "당신은 기관 퀀트 트레이더다. 제공된 15분봉 시계열과 호가창 요약을 분석하여, "
        "이 돌파가 세력의 가짜 펌핑(휩소)·펌프 앤 덤프·물량 떠넘기기(설거지)일 위험도를 "
        "0~100 정수로 평가하라. 비정상적인 윗꼬리, 거래량 급증 후 급감, 매도 호가 대비 매수 호가의 허수(Spoofing) 징후에 가중치를 두어라.\n"
        f"{_FALSE_BREAKOUT_SCORE_RUBRIC}\n"
        '출력은 JSON만: {"false_breakout_prob": <int>, "rationale": "짧은 한국어 1~3문장"}\n'
        f"candles_15m={candles}\n"
        f"orderbook={orderbook}\n"
    )


def _build_llm_prompt_swing(candles: List[Dict[str, float]], orderbook: Dict[str, float]) -> str:
    return (
        "당신은 기관 퀀트 트레이더다. 제공된 일봉(Daily) 캔들과 호가창을 분석하여, "
        "이 타점이 바닥을 다지고 반등할 자리가 아니라 지지선 붕괴 후 수직 낙하하는 '떨어지는 칼날', "
        "데드캣 바운스, 또는 거래량이 말라붙은 '좀비' 상태일 위험도를 0~100 정수로 평가하라. "
        "하락 모멘텀 지속성, 바닥 다지기(도지·거래량 축소 등) 부재에 가중치를 두어라.\n"
        f"{_FALSE_BREAKOUT_SCORE_RUBRIC}\n"
        '출력은 JSON만: {"false_breakout_prob": <int>, "rationale": "짧은 한국어 1~3문장"}\n'
        '키 이름은 반드시 false_breakout_prob 이다(위험도가 높을수록 숫자를 크게).\n'
        f"candles_daily={candles}\n"
        f"orderbook={orderbook}\n"
    )


def _parse_llm_json(text: str) -> Tuple[int, str] | None:
    if not text:
        return None
    import json

    m = re.search(r"\{.*\}", text, flags=re.S)
    raw = m.group(0) if m else text.strip()
    try:
        payload = json.loads(raw)
        prob = int(payload.get("false_breakout_prob", 50))
        reason = str(payload.get("rationale", "no_rationale"))
        return max(0, min(100, prob)), reason
    except Exception:
        return None


def _openai_model_from_config(config: dict | None) -> str:
    """``config.json`` 의 ``ai_false_breakout_openai_model`` (기본 ``gpt-4o-mini``)."""
    if isinstance(config, dict):
        m = str(config.get("ai_false_breakout_openai_model", "") or "").strip()
        if m:
            return m
    return "gpt-4o-mini"


def _openai_prob(
    candles: List[Dict[str, float]],
    orderbook: Dict[str, float],
    config: dict | None,
    strategy_type: str,
    model_name: str | None = None,
) -> Tuple[int, str, bool]:
    api_key = _get_secret("OPENAI_API_KEY", config)
    st = normalize_ai_strategy_type(strategy_type)
    if not api_key:
        prob = _rule_based_by_strategy(candles, orderbook, st)
        return prob, "fallback:OPENAI_API_KEY 없음 → rule_based", False

    prompt = (
        _build_llm_prompt_swing(candles, orderbook)
        if st == "SWING_FIB"
        else _build_llm_prompt_v8(candles, orderbook)
    )

    mdl = (model_name or "").strip() or _openai_model_from_config(config)

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        prob = _rule_based_by_strategy(candles, orderbook, st)
        return prob, "fallback:openai 패키지 없음 → rule_based", False

    client = OpenAI(api_key=api_key)
    try:
        resp = client.responses.create(model=mdl, input=prompt, temperature=0.1)
        text = (resp.output_text or "").strip()
        parsed = _parse_llm_json(text)
        if parsed is None:
            prob = _rule_based_by_strategy(candles, orderbook, st)
            return prob, "fallback:openai JSON 파싱 실패 → rule_based", False
        prob, reason = parsed
        return prob, reason, True
    except Exception as e:
        prob = _rule_based_by_strategy(candles, orderbook, st)
        return prob, f"fallback:{type(e).__name__} → rule_based", False


def _gemini_prob(
    candles: List[Dict[str, float]],
    orderbook: Dict[str, float],
    config: dict | None,
    strategy_type: str,
    model_name: str = "gemini-2.5-flash",
) -> Tuple[int, str, bool]:
    api_key = _get_secret("GOOGLE_API_KEY", config)
    st = normalize_ai_strategy_type(strategy_type)
    if not api_key:
        prob = _rule_based_by_strategy(candles, orderbook, st)
        return prob, "fallback:GOOGLE_API_KEY 없음 → rule_based", False

    prompt = (
        _build_llm_prompt_swing(candles, orderbook)
        if st == "SWING_FIB"
        else _build_llm_prompt_v8(candles, orderbook)
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
            parsed = _parse_llm_json(text)
            if parsed is None:
                last_err = f"{mdl}:empty_or_bad_json"
                continue
            prob, reason = parsed
            return prob, reason, True
        except Exception as e:
            last_err = f"{mdl}:{type(e).__name__}"
            continue

    prob = _rule_based_by_strategy(candles, orderbook, st)
    msg = f"fallback:Gemini 모델/응답 실패 ({last_err}) → rule_based" if last_err else "fallback:Gemini 실패 → rule_based"
    return prob, msg, False


def summarize_ai_rationale(text: str, max_chars: int = 160) -> str:
    """로그용 사유 1~2문장 요약."""
    s = str(text or "").strip().replace("\n", " ")
    if not s:
        return "(사유 없음)"
    # 문장 단위로 최대 2개까지
    parts = re.split(r"(?<=[.!?。])\s+", s)
    if len(parts) >= 2 and len(parts[0]) + len(parts[1]) <= max_chars:
        out = (parts[0] + " " + parts[1]).strip()
        return out if len(out) <= max_chars else out[: max_chars - 3] + "..."
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 3] + "..."


def evaluate_false_breakout_filter(
    ticker: str,
    candles: List[Dict[str, float]] | None = None,
    orderbook: Dict[str, float] | None = None,
    threshold: int = 70,
    use_ai: bool = True,
    ai_provider: str = "gemini",
    config: dict | None = None,
    strategy_type: str | None = None,
    *,
    candles_15m_10: List[Dict[str, float]] | None = None,
) -> Dict[str, Any]:
    """
    위험도 ``false_breakout_prob`` (0~100) 가 ``threshold`` 이상이면 매수 차단.

    LLM 호출 시 ``_FALSE_BREAKOUT_SCORE_RUBRIC`` 으로 0~100 의미를 봇 임계와 정렬한다.

    Returns
        evaluation_engine: 점수 산출 경로 ``gemini`` | ``openai`` | ``rule_based``
        llm_success: 외부 LLM이 유효 JSON을 반환했으면 True
        strategy_type: ``TREND_V8`` | ``SWING_FIB``
        openai_fallback_used: 주 제공자가 Gemini인데 실패 후 OpenAI로 재시도해 성공했으면 True
    """
    rows = candles if candles is not None else (candles_15m_10 or [])
    ob = orderbook if orderbook is not None else {}
    st = normalize_ai_strategy_type(strategy_type)

    provider_in = str(ai_provider or "gemini").strip().lower()
    llm_success = False
    evaluation_engine = "rule_based"
    openai_fallback_used = False

    fallback_after_gemini = True
    if isinstance(config, dict) and "ai_false_breakout_openai_fallback" in config:
        fallback_after_gemini = bool(config.get("ai_false_breakout_openai_fallback"))

    if not use_ai:
        prob = _rule_based_by_strategy(rows, ob, st)
        rationale = "rule_based(use_ai=False)"
    elif provider_in == "openai":
        prob, rationale, llm_success = _openai_prob(rows, ob, config, st)
        evaluation_engine = "openai" if llm_success else "rule_based"
    else:
        prob, rationale, llm_success = _gemini_prob(rows, ob, config, st)
        evaluation_engine = "gemini" if llm_success else "rule_based"

        if (
            not llm_success
            and fallback_after_gemini
            and _get_secret("OPENAI_API_KEY", config).strip()
        ):
            prob_o, rationale_o, ok_o = _openai_prob(rows, ob, config, st)
            if ok_o:
                prob, rationale = prob_o, f"[Gemini 실패→OpenAI 폴백] {rationale_o}"
                llm_success = True
                evaluation_engine = "openai"
                openai_fallback_used = True

    blocked = prob >= int(threshold)
    provider_label = provider_in if use_ai else "rule_based"

    return {
        "ticker": ticker,
        "false_breakout_prob": prob,
        "threshold": int(threshold),
        "blocked": blocked,
        "rationale": rationale,
        "provider": provider_label,
        "strategy_type": st,
        "evaluation_engine": evaluation_engine,
        "llm_success": llm_success,
        "openai_fallback_used": openai_fallback_used,
    }


# 호환: 구 이름
_rule_based_false_breakout_prob = _rule_based_trend_v8
