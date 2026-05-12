# -*- coding: utf-8 -*-
"""
Phase 3 — 뉴스 악재 센티먼트 필터.

LLM(Gemini/OpenAI)은 OHLCV·호가 숫자가 아니라 **최근 뉴스 헤드라인**만 보고
단기 치명 악재 여부를 0~100 위험도로 평가한다. 뉴스 수집 실패·본문 없음 시
LLM 호출 없이 위험도 0(통과)으로 룰베이스 매매를 방해하지 않는다.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

import requests
import yfinance as yf

_NEWS_LOOKBACK_SEC = 24 * 3600
_NAVER_NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_ROOT_AI_KEYS_CACHE: Dict[str, str] | None = None


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        return float(v)
    except Exception:
        return float(default)


def _load_root_ai_keys_file() -> Dict[str, str]:
    """프로젝트 루트 ``ai_keys.txt`` — ``KEY=value`` 한 줄씩."""
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


def _normalize_market(market: str | None) -> str:
    return str(market or "").strip().upper()


def _yf_news_symbol(ticker: str, market: str) -> str | None:
    sym = str(ticker or "").strip().upper()
    mk = _normalize_market(market)
    if mk == "KR" or sym.isdigit():
        return None
    if sym.startswith("USDT-"):
        base = sym.split("-", 1)[1]
        return f"{base}-USD"
    if sym.startswith("KRW-"):
        base = sym.split("-", 1)[1]
        return f"{base}-USD"
    return sym


def _news_item_timestamp(item: dict) -> float | None:
    for key in ("providerPublishTime", "pubDate", "published_at", "publishedAt", "datetime"):
        raw = item.get(key)
        if raw is None:
            continue
        try:
            if isinstance(raw, (int, float)):
                ts = float(raw)
                if ts > 1e12:
                    ts /= 1000.0
                return ts
            if isinstance(raw, str):
                s = raw.strip()
                if s.isdigit():
                    if len(s) >= 12:
                        try:
                            dt = datetime.strptime(s[:12], "%Y%m%d%H%M")
                            return dt.replace(tzinfo=timezone(timedelta(hours=9))).timestamp()
                        except Exception:
                            pass
                    ts = float(s)
                    if ts > 1e12:
                        ts /= 1000.0
                    return ts
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
        except Exception:
            continue
    return None


def _extract_yfinance_news_fields(item: dict) -> tuple[str, float | None]:
    if not isinstance(item, dict):
        return "", None
    payload = item.get("content") if isinstance(item.get("content"), dict) else item
    title = str(payload.get("title") or payload.get("headline") or item.get("title") or "").strip()
    ts = _news_item_timestamp(payload)
    if ts is None:
        ts = _news_item_timestamp(item)
    return title, ts


def _fetch_yfinance_news_text(symbol: str, max_headlines: int = 10) -> str:
    sym = str(symbol or "").strip()
    if not sym:
        return ""
    try:
        items = yf.Ticker(sym).news or []
    except Exception:
        return ""

    now_ts = time.time()
    lines: list[str] = []
    for item in items:
        title, ts = _extract_yfinance_news_fields(item)
        if not title:
            continue
        if ts is not None and (now_ts - ts) > _NEWS_LOOKBACK_SEC:
            continue
        lines.append(title)
        if len(lines) >= max(1, int(max_headlines)):
            break
    return "\n".join(lines)


def _fetch_kr_naver_news_headlines(ticker: str, limit: int = 5) -> str:
    code = "".join(ch for ch in str(ticker or "") if ch.isdigit()).zfill(6)
    if not code or code == "000000":
        return ""
    url = f"https://api.stock.naver.com/news/stock/{code}?page=1&pageSize={max(5, int(limit))}"
    try:
        res = requests.get(
            url,
            headers={
                **_NAVER_NEWS_HEADERS,
                "Referer": "https://m.stock.naver.com/",
            },
            timeout=8,
        )
        if res.status_code >= 400:
            return ""
        data = res.json()
    except Exception:
        return ""

    now_ts = time.time()
    titles: list[str] = []
    blocks = data if isinstance(data, list) else [data]
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for item in block.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("titleFull") or item.get("title") or "").strip()
            if not title or title in titles:
                continue
            ts = _news_item_timestamp(item)
            if ts is not None and (now_ts - ts) > _NEWS_LOOKBACK_SEC:
                continue
            titles.append(title)
            if len(titles) >= max(1, int(limit)):
                return "\n".join(titles)
    return "\n".join(titles)


def collect_recent_news_text(ticker: str, market: str, max_headlines: int = 10) -> str:
    """시장별 최근 뉴스 헤드라인 텍스트.

    * KR — 네이버 금융 종목 뉴스 상위 5개
    * US / COIN — ``yfinance`` ``Ticker.news`` (최근 24시간, 영문 헤드라인)
    """
    mk = _normalize_market(market)
    if mk == "KR":
        return _fetch_kr_naver_news_headlines(ticker, limit=5).strip()
    symbol = _yf_news_symbol(ticker, mk)
    if not symbol:
        return ""
    return _fetch_yfinance_news_text(symbol, max_headlines=max_headlines).strip()


def _build_llm_prompt_news(news_text: str) -> str:
    return (
        "당신은 헤지펀드의 리스크 관리자입니다. 다음은 이 종목의 최근 뉴스 헤드라인입니다. "
        "이 텍스트 안에 주가에 단기적으로 치명적인 타격을 줄 수 있는 명백한 악재"
        "(예: 횡령, 배임, 대규모 유상증자, 상장폐지 위기, CEO 구속 등)가 포함되어 있는지 분석하십시오. "
        "악재가 확실하다면 위험도 점수를 80~100점 사이로, 단순 노이즈거나 호재/중립이라면 0~30점 사이로 반환하십시오.\n"
        '출력은 JSON만: {"false_breakout_prob": <int>, "rationale": "짧은 한국어 1~3문장"}\n'
        f"news_text={news_text}\n"
    )


def _parse_llm_json(text: str) -> Tuple[int, str] | None:
    if not text:
        return None
    import json

    m = re.search(r"\{.*\}", text, flags=re.S)
    raw = m.group(0) if m else text.strip()
    try:
        payload = json.loads(raw)
        prob = int(payload.get("false_breakout_prob", 0))
        reason = str(payload.get("rationale", "no_rationale"))
        return max(0, min(100, prob)), reason
    except Exception:
        return None


def _openai_model_from_config(config: dict | None) -> str:
    if isinstance(config, dict):
        m = str(config.get("ai_false_breakout_openai_model", "") or "").strip()
        if m:
            return m
    return "gpt-4o-mini"


def _openai_news_prob(
    news_text: str,
    config: dict | None,
    model_name: str | None = None,
) -> Tuple[int, str, bool]:
    api_key = _get_secret("OPENAI_API_KEY", config)
    if not api_key:
        return 0, "skip:OPENAI_API_KEY 없음 → 위험도 0", False

    prompt = _build_llm_prompt_news(news_text)
    mdl = (model_name or "").strip() or _openai_model_from_config(config)

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return 0, "skip:openai 패키지 없음 → 위험도 0", False

    client = OpenAI(api_key=api_key)
    try:
        resp = client.responses.create(model=mdl, input=prompt, temperature=0.1)
        text = (resp.output_text or "").strip()
        parsed = _parse_llm_json(text)
        if parsed is None:
            return 0, "skip:openai JSON 파싱 실패 → 위험도 0", False
        prob, reason = parsed
        return prob, reason, True
    except Exception as e:
        return 0, f"skip:{type(e).__name__} → 위험도 0", False


def _gemini_news_prob(
    news_text: str,
    config: dict | None,
    model_name: str = "gemini-2.5-flash",
) -> Tuple[int, str, bool]:
    api_key = _get_secret("GOOGLE_API_KEY", config)
    if not api_key:
        return 0, "skip:GOOGLE_API_KEY 없음 → 위험도 0", False

    prompt = _build_llm_prompt_news(news_text)
    model_candidates = [
        model_name,
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
    ]
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

    msg = f"skip:Gemini 실패 ({last_err}) → 위험도 0" if last_err else "skip:Gemini 실패 → 위험도 0"
    return 0, msg, False


def summarize_ai_rationale(text: str, max_chars: int = 160) -> str:
    """로그용 사유 1~2문장 요약."""
    s = str(text or "").strip().replace("\n", " ")
    if not s:
        return "(사유 없음)"
    parts = re.split(r"(?<=[.!?。])\s+", s)
    if len(parts) >= 2 and len(parts[0]) + len(parts[1]) <= max_chars:
        out = (parts[0] + " " + parts[1]).strip()
        return out if len(out) <= max_chars else out[: max_chars - 3] + "..."
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 3] + "..."


def evaluate_false_breakout_filter(
    ticker: str,
    market: str,
    threshold: int = 70,
    use_ai: bool = True,
    ai_provider: str = "gemini",
    config: dict | None = None,
    strategy_type: str | None = None,
    *,
    candles: Any = None,
    orderbook: Any = None,
    candles_15m_10: Any = None,
) -> Dict[str, Any]:
    """뉴스 악재 위험도 ``false_breakout_prob`` (0~100) 가 ``threshold`` 이상이면 매수 차단."""
    st = normalize_ai_strategy_type(strategy_type)
    provider_in = str(ai_provider or "gemini").strip().lower()
    news_text = collect_recent_news_text(ticker, market)

    if not news_text.strip():
        return {
            "ticker": ticker,
            "market": _normalize_market(market),
            "false_breakout_prob": 0,
            "threshold": int(threshold),
            "blocked": False,
            "rationale": "skip:뉴스 없음/수집 실패 → 위험도 0",
            "provider": provider_in if use_ai else "skip",
            "strategy_type": st,
            "evaluation_engine": "skip_no_news",
            "llm_success": False,
            "openai_fallback_used": False,
        }

    if not use_ai:
        return {
            "ticker": ticker,
            "market": _normalize_market(market),
            "false_breakout_prob": 0,
            "threshold": int(threshold),
            "blocked": False,
            "rationale": "skip:use_ai=False → 위험도 0",
            "provider": "skip",
            "strategy_type": st,
            "evaluation_engine": "skip_no_ai",
            "llm_success": False,
            "openai_fallback_used": False,
        }

    llm_success = False
    evaluation_engine = "skip"
    openai_fallback_used = False

    fallback_after_gemini = True
    if isinstance(config, dict) and "ai_false_breakout_openai_fallback" in config:
        fallback_after_gemini = bool(config.get("ai_false_breakout_openai_fallback"))

    if provider_in == "openai":
        prob, rationale, llm_success = _openai_news_prob(news_text, config)
        evaluation_engine = "openai" if llm_success else "skip"
    else:
        prob, rationale, llm_success = _gemini_news_prob(news_text, config)
        evaluation_engine = "gemini" if llm_success else "skip"

        if (
            not llm_success
            and fallback_after_gemini
            and _get_secret("OPENAI_API_KEY", config).strip()
        ):
            prob_o, rationale_o, ok_o = _openai_news_prob(news_text, config)
            if ok_o:
                prob, rationale = prob_o, f"[Gemini 실패→OpenAI 폴백] {rationale_o}"
                llm_success = True
                evaluation_engine = "openai"
                openai_fallback_used = True

    blocked = int(prob) >= int(threshold)

    return {
        "ticker": ticker,
        "market": _normalize_market(market),
        "false_breakout_prob": int(prob),
        "threshold": int(threshold),
        "blocked": blocked,
        "rationale": rationale,
        "provider": provider_in,
        "strategy_type": st,
        "evaluation_engine": evaluation_engine,
        "llm_success": llm_success,
        "openai_fallback_used": openai_fallback_used,
    }
