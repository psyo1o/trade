# -*- coding: utf-8 -*-
"""
KR / US / COIN 대체 데이터(Alternative Data) 사전 연구용 독립 스크립트.

운영 봇 본체(run_bot.py, strategy/*)와 분리되어 있으며, API 키·토큰이 없어도
Mock 폴백으로 터미널에서 함수 동작을 검증할 수 있다.

실행 (저장소 루트):
  python tests/test_alt_data_research.py
  py -3.11 tests/test_alt_data_research.py

설정 우선순위 (앞이 우선):
  1. 환경변수 KIS_KEY, KIS_SECRET, KIS_ACCESS_TOKEN, CUSTOMS_API_KEY
  2. 루트 ``config.json`` (``kis_key``, ``kis_secret``, ``kis_account``, 선택 ``customs_api_key``)
  3. 루트 ``kis_token.json`` 의 ``access_token`` (KIS_ACCESS_TOKEN 미설정 시)
  4. 위가 없으면 해당 모듈은 Mock

미장 옵션·코인 고래 포지션은 공개 API(yfinance, Binance)로 키 없이 시도한다.
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 공통 설정 / 유틸
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "kis_key": "",
    "kis_secret": "",
    "kis_account": "",
    "kis_base_url": "https://openapi.koreainvestment.com:9443",
    "kis_access_token": "",
    "customs_api_key": "",
    "customs_base_url": "https://unipass.customs.go.kr:38010/ext/rest",
    "request_timeout_sec": 12,
}


def _pick_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _load_kis_access_token_from_file(token_path: Path | None = None) -> str:
    path = token_path or (ROOT / "kis_token.json")
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("access_token") or "").replace("Bearer ", "").strip()


def load_research_config(config_path: Path | None = None) -> dict[str, Any]:
    """운영 ``config.json``·``kis_token.json`` 과 환경변수를 연구용 dict로 합친다."""
    cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
    path = config_path or (ROOT / "config.json")
    file_cfg: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                file_cfg = raw
        except Exception as exc:
            print(f"⚠️ config.json 읽기 실패 ({path}): {exc}")
    else:
        print(f"⚠️ config.json 없음 — 환경변수·Mock만 사용 ({path})")

    cfg["kis_key"] = _pick_text(os.environ.get("KIS_KEY"), file_cfg.get("kis_key"))
    cfg["kis_secret"] = _pick_text(os.environ.get("KIS_SECRET"), file_cfg.get("kis_secret"))
    cfg["kis_account"] = _pick_text(os.environ.get("KIS_ACCOUNT"), file_cfg.get("kis_account"))
    cfg["kis_access_token"] = _pick_text(
        os.environ.get("KIS_ACCESS_TOKEN"),
        _load_kis_access_token_from_file(),
    )
    cfg["customs_api_key"] = _pick_text(
        os.environ.get("CUSTOMS_API_KEY"),
        file_cfg.get("customs_api_key"),
    )
    cfg["kis_base_url"] = _pick_text(
        os.environ.get("KIS_BASE_URL"),
        file_cfg.get("kis_base_url"),
        DEFAULT_CONFIG["kis_base_url"],
    )
    cfg["customs_base_url"] = _pick_text(
        os.environ.get("CUSTOMS_BASE_URL"),
        file_cfg.get("customs_base_url"),
        DEFAULT_CONFIG["customs_base_url"],
    )
    timeout_raw = file_cfg.get("alt_data_request_timeout_sec", file_cfg.get("request_timeout_sec"))
    if timeout_raw is not None:
        try:
            cfg["request_timeout_sec"] = int(timeout_raw)
        except (TypeError, ValueError):
            pass
    return cfg


def _cfg(config: dict[str, Any] | None, key: str, default: Any = "") -> Any:
    if isinstance(config, dict) and key in config:
        return config.get(key, default)
    return DEFAULT_CONFIG.get(key, default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(_safe_float(value, float(default)))
    except Exception:
        return int(default)


def _print_section(title: str) -> None:
    print("\n" + "-" * 72)
    print(title)
    print("-" * 72)


def _format_kis_day(day: str) -> str:
    digits = "".join(ch for ch in str(day or "") if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return str(day or "").strip()


# ---------------------------------------------------------------------------
# 모듈 1: 국장(KR) 알파 데이터
# ---------------------------------------------------------------------------

def _mock_kis_investor_flow(ticker: str, reason: str) -> dict[str, Any]:
    rng = random.Random(str(ticker))
    rows: list[dict[str, Any]] = []
    base_day = datetime.now().date()
    for i in range(5):
        day = base_day - timedelta(days=i + 1)
        foreign_net = rng.randint(-120_000, 180_000)
        inst_net = rng.randint(-90_000, 150_000)
        rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "foreign_net_qty": foreign_net,
                "institution_net_qty": inst_net,
            }
        )
    rows.reverse()
    strong_days = sum(
        1
        for row in rows
        if row["foreign_net_qty"] > 0 and row["institution_net_qty"] > 0
    )
    return {
        "ticker": str(ticker),
        "source": "mock",
        "reason": reason,
        "rows": rows,
        "strong_flow_days": strong_days,
        "is_strong_flow": strong_days >= 3,
    }


def _kis_headers(config: dict[str, Any] | None, *, tr_id: str) -> dict[str, str]:
    token = str(_cfg(config, "kis_access_token", "") or "").replace("Bearer ", "").strip()
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": str(_cfg(config, "kis_key", "") or ""),
        "appsecret": str(_cfg(config, "kis_secret", "") or ""),
        "tr_id": tr_id,
        "custtype": "P",
    }


def get_kis_investor_flow(ticker: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """최근 5영업일 외국인/기관 순매수 수량 조회.

    3일 이상 양쪽 모두 순매수(쌍끌이)이면 ``is_strong_flow=True``.
    토큰·키가 없거나 API 실패 시 Mock 데이터로 폴백한다.
    """
    code = "".join(ch for ch in str(ticker or "") if ch.isdigit()).zfill(6)
    if not code or code == "000000":
        return _mock_kis_investor_flow(ticker, "invalid_ticker")

    token = str(_cfg(config, "kis_access_token", "") or "").strip()
    app_key = str(_cfg(config, "kis_key", "") or "").strip()
    app_secret = str(_cfg(config, "kis_secret", "") or "").strip()
    if not token or not app_key or not app_secret:
        return _mock_kis_investor_flow(code, "missing_kis_credentials")

    base_url = str(_cfg(config, "kis_base_url", DEFAULT_CONFIG["kis_base_url"]))
    timeout = _safe_float(_cfg(config, "request_timeout_sec", 12), 12.0)

    # [국내주식-012] 주식현재가 투자자 — 종목·일자별 외국인/기관 순매수
    url = f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
    }

    try:
        res = requests.get(
            url,
            headers=_kis_headers(config, tr_id="FHKST01010900"),
            params=params,
            timeout=timeout,
        )
        payload = res.json() if hasattr(res, "json") else {}
    except Exception as exc:
        return _mock_kis_investor_flow(code, f"request_error:{type(exc).__name__}")

    if str(payload.get("rt_cd", "")) != "0":
        msg = str(payload.get("msg1", "") or payload.get("msg_cd", "") or "kis_error")
        return _mock_kis_investor_flow(code, f"kis_api_error:{msg}")

    raw_rows = payload.get("output") or payload.get("output1") or []
    if isinstance(raw_rows, dict):
        raw_rows = [raw_rows]

    parsed: list[dict[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        day = str(
            row.get("stck_bsop_date")
            or row.get("bsop_date")
            or row.get("trd_dd")
            or row.get("date")
            or ""
        ).strip()
        foreign_net = _safe_int(
            row.get("frgn_ntby_qty")
            or row.get("frgn_ntby_vol")
            or row.get("frgn_ntby_tr_pbmn")
            or row.get("foreign_net_qty")
            or 0
        )
        inst_net = _safe_int(
            row.get("orgn_ntby_qty")
            or row.get("orgn_ntby_vol")
            or row.get("orgn_ntby_tr_pbmn")
            or row.get("institution_net_qty")
            or 0
        )
        if not day:
            continue
        parsed.append(
            {
                "date": day,
                "foreign_net_qty": foreign_net,
                "institution_net_qty": inst_net,
            }
        )

    if not parsed:
        return _mock_kis_investor_flow(code, "empty_kis_response")

    parsed.sort(key=lambda x: x["date"])
    settled = [
        row
        for row in parsed
        if row["foreign_net_qty"] != 0 or row["institution_net_qty"] != 0
    ]
    rows = (settled if settled else parsed)[-5:]
    strong_days = sum(
        1
        for row in rows
        if row["foreign_net_qty"] > 0 and row["institution_net_qty"] > 0
    )
    return {
        "ticker": code,
        "source": "kis",
        "reason": "ok",
        "rows": rows,
        "strong_flow_days": strong_days,
        "is_strong_flow": strong_days >= 3,
    }


def _mock_customs_export_data(hs_code: str, reason: str) -> dict[str, Any]:
    rng = random.Random(str(hs_code))
    month = datetime.now().strftime("%Y-%m")
    export_usd = round(rng.uniform(8_000_000, 120_000_000), 2)
    return {
        "hs_code": str(hs_code),
        "month": month,
        "export_amount_usd": export_usd,
        "source": "mock",
        "reason": reason,
        "endpoint": "mock://customs/export",
    }


def get_customs_export_data(hs_code: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """관세청 OpenAPI 형태의 당월 수출액 조회 프레임워크.

    실제 키가 없거나 응답이 비정상이면 Mock 수출액을 반환한다.
    """
    code = str(hs_code or "").strip()
    if not code:
        return _mock_customs_export_data("000000", "invalid_hs_code")

    api_key = str(_cfg(config, "customs_api_key", "") or "").strip()
    if not api_key:
        return _mock_customs_export_data(code, "missing_customs_api_key")

    base_url = str(_cfg(config, "customs_base_url", DEFAULT_CONFIG["customs_base_url"]))
    timeout = _safe_float(_cfg(config, "request_timeout_sec", 12), 12.0)
    month = datetime.now().strftime("%Y%m")
    endpoint = f"{base_url.rstrip('/')}/expDclrQry"
    params = {
        "crkyCn": api_key,
        "hsSgn": code,
        "inqYm": month,
        "pageNo": 1,
        "numOfRows": 10,
    }

    try:
        res = requests.get(endpoint, params=params, timeout=timeout)
        payload = res.json() if hasattr(res, "json") else {}
    except Exception as exc:
        return _mock_customs_export_data(code, f"request_error:{type(exc).__name__}")

    body = payload.get("response", payload) if isinstance(payload, dict) else {}
    items = body.get("items") if isinstance(body, dict) else None
    if isinstance(items, dict):
        items = items.get("item")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return _mock_customs_export_data(code, "empty_customs_response")

    first = items[0] if isinstance(items[0], dict) else {}
    export_usd = _safe_float(
        first.get("expDlr")
        or first.get("expAmt")
        or first.get("expUsdAmt")
        or first.get("exportAmountUsd")
        or 0.0
    )
    if export_usd <= 0:
        return _mock_customs_export_data(code, "invalid_customs_amount")

    return {
        "hs_code": code,
        "month": month,
        "export_amount_usd": export_usd,
        "source": "customs_api",
        "reason": "ok",
        "endpoint": endpoint,
    }


# ---------------------------------------------------------------------------
# 모듈 2: 미장(US) 알파 데이터
# ---------------------------------------------------------------------------

def _mock_options_imbalance(ticker: str, reason: str) -> dict[str, Any]:
    rng = random.Random(str(ticker))
    call_oi = rng.randint(120_000, 900_000)
    put_oi = rng.randint(90_000, 700_000)
    ratio = put_oi / max(call_oi, 1)
    return {
        "ticker": str(ticker).upper(),
        "source": "mock",
        "reason": reason,
        "expiry": "mock_expiry",
        "call_open_interest": call_oi,
        "put_open_interest": put_oi,
        "put_call_ratio": round(ratio, 4),
        "state": _options_state_from_ratio(ratio),
    }


def _options_state_from_ratio(ratio: float) -> str:
    if ratio >= 1.2:
        return "하방 압력/공포"
    if ratio <= 0.7:
        return "상방 배팅/탐욕"
    return "중립"


def get_options_imbalance(ticker: str) -> dict[str, Any]:
    """가장 가까운 만기 옵션 체인의 Put/Call OI 비율."""
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return _mock_options_imbalance("UNKNOWN", "invalid_ticker")

    try:
        tk = yf.Ticker(symbol)
        expiries = list(getattr(tk, "options", []) or [])
        if not expiries:
            return _mock_options_imbalance(symbol, "no_option_expiries")

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
            return _mock_options_imbalance(symbol, "empty_open_interest")

        ratio = put_oi / max(call_oi, 1.0)
        return {
            "ticker": symbol,
            "source": "yfinance",
            "reason": "ok",
            "expiry": expiry,
            "call_open_interest": int(call_oi),
            "put_open_interest": int(put_oi),
            "put_call_ratio": round(ratio, 4),
            "state": _options_state_from_ratio(ratio),
        }
    except Exception as exc:
        return _mock_options_imbalance(symbol, f"yfinance_error:{type(exc).__name__}")


# ---------------------------------------------------------------------------
# 모듈 3: 코인(COIN) 알파 데이터
# ---------------------------------------------------------------------------

def _mock_binance_whale_position(symbol: str, reason: str) -> dict[str, Any]:
    rng = random.Random(str(symbol))
    ratio = round(rng.uniform(0.55, 2.40), 4)
    return {
        "symbol": str(symbol).upper(),
        "source": "mock",
        "reason": reason,
        "period": "1d",
        "long_short_ratio": ratio,
        "state": _whale_state_from_ratio(ratio),
        "timestamp": int(time.time() * 1000),
    }


def _whale_state_from_ratio(ratio: float) -> str:
    if ratio >= 2.0:
        return "강력한 롱 매집"
    if ratio <= 0.8:
        return "숏 헷징 우위"
    return "중립"


def get_binance_whale_position(symbol: str, period: str = "1d") -> dict[str, Any]:
    """바이낸스 선물 상위 트레이더 롱/숏 포지션 비율."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return _mock_binance_whale_position("BTCUSDT", "invalid_symbol")

    url = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
    params = {
        "symbol": sym,
        "period": str(period or "1d"),
        "limit": 1,
    }

    try:
        res = requests.get(url, params=params, timeout=_safe_float(DEFAULT_CONFIG["request_timeout_sec"], 12.0))
        if res.status_code >= 400:
            return _mock_binance_whale_position(sym, f"http_{res.status_code}")
        rows = res.json()
        if not isinstance(rows, list) or not rows:
            return _mock_binance_whale_position(sym, "empty_response")

        latest = rows[-1] if isinstance(rows[-1], dict) else {}
        ratio = _safe_float(latest.get("longShortRatio"), 0.0)
        if ratio <= 0:
            return _mock_binance_whale_position(sym, "invalid_ratio")

        return {
            "symbol": sym,
            "source": "binance_futures",
            "reason": "ok",
            "period": str(period or "1d"),
            "long_short_ratio": round(ratio, 4),
            "state": _whale_state_from_ratio(ratio),
            "timestamp": _safe_int(latest.get("timestamp"), 0),
        }
    except Exception as exc:
        return _mock_binance_whale_position(sym, f"request_error:{type(exc).__name__}")


# ---------------------------------------------------------------------------
# 출력 / 테스트
# ---------------------------------------------------------------------------

def _print_kr_flow(result: dict[str, Any]) -> None:
    print(f"ticker={result.get('ticker')} source={result.get('source')} reason={result.get('reason')}")
    print(f"is_strong_flow={result.get('is_strong_flow')} strong_flow_days={result.get('strong_flow_days')}")
    for row in result.get("rows", []):
        print(
            f"  {_format_kis_day(str(row.get('date') or ''))} | "
            f"foreign_net={row.get('foreign_net_qty'):,} | "
            f"institution_net={row.get('institution_net_qty'):,}"
        )


def _print_customs(result: dict[str, Any]) -> None:
    print(f"hs_code={result.get('hs_code')} month={result.get('month')} source={result.get('source')}")
    print(f"export_amount_usd={result.get('export_amount_usd'):,.2f} reason={result.get('reason')}")


def _print_options(result: dict[str, Any]) -> None:
    print(
        f"ticker={result.get('ticker')} expiry={result.get('expiry')} source={result.get('source')} "
        f"reason={result.get('reason')}"
    )
    print(
        f"call_oi={result.get('call_open_interest'):,} put_oi={result.get('put_open_interest'):,} "
        f"put_call_ratio={result.get('put_call_ratio')} state={result.get('state')}"
    )


def _print_whale(result: dict[str, Any]) -> None:
    print(
        f"symbol={result.get('symbol')} period={result.get('period')} source={result.get('source')} "
        f"reason={result.get('reason')}"
    )
    print(
        f"long_short_ratio={result.get('long_short_ratio')} state={result.get('state')} "
        f"timestamp={result.get('timestamp')}"
    )


if __name__ == "__main__":
    research_config = load_research_config()

    _print_section("[KR] get_kis_investor_flow — 062040 (산일전기)")
    _print_kr_flow(get_kis_investor_flow("062040", research_config))

    _print_section("[KR] get_customs_export_data — HS 850421")
    _print_customs(get_customs_export_data("850421", research_config))

    for us_ticker in ("SPY", "NVDA"):
        _print_section(f"[US] get_options_imbalance — {us_ticker}")
        _print_options(get_options_imbalance(us_ticker))

    for coin_symbol in ("BTCUSDT", "ETHUSDT"):
        _print_section(f"[COIN] get_binance_whale_position — {coin_symbol}")
        _print_whale(get_binance_whale_position(coin_symbol, period="1d"))
