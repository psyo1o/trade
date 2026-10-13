# -*- coding: utf-8 -*-
"""국장 마감 직전 지수 급락 청산 판단.

KST 15:15~15:20 (15분 사이클의 15:15 슬롯)에
KODEX200(069500) 또는 종목 상장시장 지수(KOSPI/KOSDAQ)가 전일 종가 대비
``threshold`` 이하(기본 -3.0%, ``run_bot.INDEX_CRASH_KR`` 와 동일)이면 전량 청산 대상.

* ``off``    — 아무것도 하지 않음
* ``shadow`` — ``[SHADOW-KRCRASH]`` 로그만
* ``live``   — 시장가 전량 매도 (호출 측)

지수 조회 실패(None)는 그 지수를 트리거로 쓰지 않는다. 데이터가 없으면 매도하지 않는다.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from strategy.rule_modes import normalize_rule_mode

KR_CRASH_EXIT_THRESHOLD_PCT = -3.0
KR_CRASH_EXIT_HOUR = 15
KR_CRASH_EXIT_MINUTE_START = 15
KR_CRASH_EXIT_MINUTE_END = 20  # 포함

_KST = ZoneInfo("Asia/Seoul")
_LISTING_TTL_SEC = 7 * 24 * 3600
_LISTING_MISS_TTL_SEC = 3600
_LISTING_LOCK = threading.Lock()


def in_kr_close_crash_window(now: datetime | None = None) -> bool:
    """KST 15:15:00 이상 15:20:59 이하. naive 시각은 KST 로 본다."""
    if now is None:
        current = datetime.now(_KST)
    elif now.tzinfo is None:
        current = now
    else:
        current = now.astimezone(_KST)
    if current.hour != KR_CRASH_EXIT_HOUR:
        return False
    return KR_CRASH_EXIT_MINUTE_START <= current.minute <= KR_CRASH_EXIT_MINUTE_END


def change_hits_threshold(change_pct: float | None, threshold: float = KR_CRASH_EXIT_THRESHOLD_PCT) -> bool:
    """``None``(조회 실패)은 미발동. 같으면 발동 (``<=``)."""
    if change_pct is None:
        return False
    try:
        return float(change_pct) <= float(threshold)
    except (TypeError, ValueError):
        return False


def normalize_listing(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "KOSDAQ" in text or text in ("KQ", "KSQ") or "코스닥" in str(value):
        return "KOSDAQ"
    if "KOSPI" in text or text in ("KS", "KRX") or "코스피" in str(value):
        return "KOSPI"
    return ""


def parse_listing_payload(data: Any) -> str:
    """네이버 basic 등 JSON 에서 KOSPI/KOSDAQ. 모르면 빈 문자열."""
    if not isinstance(data, dict):
        return ""
    exchange = data.get("stockExchangeType")
    if isinstance(exchange, dict):
        for key in ("nameEng", "name", "code", "nameKor"):
            hit = normalize_listing(exchange.get(key))
            if hit:
                return hit
    elif isinstance(exchange, str):
        hit = normalize_listing(exchange)
        if hit:
            return hit
    for key in ("marketName", "market", "stockExchangeName"):
        hit = normalize_listing(data.get(key))
        if hit:
            return hit
    return ""


def crash_trigger_detail(
    listing: str,
    kodex: float | None,
    kospi: float | None,
    kosdaq: float | None,
    threshold: float = KR_CRASH_EXIT_THRESHOLD_PCT,
) -> tuple[bool, str, float | None]:
    """(발동, 사유 문자열, 대표 등락률).

    KODEX200 급락이면 상장시장과 무관하게 발동.
    상장시장을 모르면 KOSPI/KOSDAQ 단독 급락만으로는 발동하지 않는다.
    """
    hits: list[str] = []
    primary: float | None = None

    def _take(label: str, change: float | None) -> None:
        nonlocal primary
        if not change_hits_threshold(change, threshold):
            return
        assert change is not None
        hits.append(f"{label} {float(change):+.2f}%")
        if primary is None or float(change) < primary:
            primary = float(change)

    _take("KODEX200", kodex)
    board = normalize_listing(listing)
    if board == "KOSDAQ":
        _take("KOSDAQ", kosdaq)
    elif board == "KOSPI":
        _take("KOSPI", kospi)
    if not hits:
        return False, "", None
    return True, " / ".join(hits), primary


@dataclass(frozen=True)
class KrIndexSnapshot:
    kodex: float | None
    kospi: float | None
    kosdaq: float | None

    def any_available(self) -> bool:
        return any(v is not None for v in (self.kodex, self.kospi, self.kosdaq))


@dataclass(frozen=True)
class CrashPlan:
    action: str  # noop | shadow | live
    trigger: str
    index_change_pct: float | None
    listing: str


def plan_kr_crash(
    mode: str,
    *,
    listing: str,
    kodex: float | None,
    kospi: float | None,
    kosdaq: float | None,
    threshold: float = KR_CRASH_EXIT_THRESHOLD_PCT,
) -> CrashPlan:
    """모드×임계 분기. ``off`` 또는 미발동은 ``noop`` (파일·주문 없음)."""
    normalized = normalize_rule_mode(mode)
    board = normalize_listing(listing)
    triggered, label, change = crash_trigger_detail(board, kodex, kospi, kosdaq, threshold)
    if normalized == "off" or not triggered:
        return CrashPlan("noop", "", None, board)
    if normalized == "shadow":
        return CrashPlan("shadow", label, change, board)
    return CrashPlan("live", label, change, board)


def format_shadow_krcrash_log(ticker: str, qty: int, price: float, pnl_pct: float | None) -> str:
    px = f"{price:.0f}" if abs(price - round(price)) < 1e-6 else f"{price:.2f}"
    pnl = "n/a" if pnl_pct is None else f"{float(pnl_pct):+.2f}%"
    return f"[SHADOW-KRCRASH] would sell {ticker} {int(qty)} at {px}, {pnl}"


def _listing_cache_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "kr_listing_cache.json"


def _load_listing_cache() -> dict[str, Any]:
    path = _listing_cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_listing_cache(data: dict[str, Any]) -> None:
    path = _listing_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"  ⚠️ [KR-CRASH] 상장시장 캐시 저장 실패: {exc}")


def _fetch_naver_basic(code: str) -> dict[str, Any]:
    import requests

    sym = "".join(ch for ch in str(code or "") if ch.isdigit()).zfill(6)
    if not sym or sym == "000000":
        return {}
    url = f"https://m.stock.naver.com/api/stock/{sym}/basic"
    try:
        res = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://m.stock.naver.com/",
            },
            timeout=8.0,
        )
        if res.status_code >= 400:
            return {}
        data = res.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def kr_listing_market(
    code: str,
    *,
    fetch_fn: Callable[[str], dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> str:
    """KOSPI/KOSDAQ. KIS 를 치지 않고 네이버 basic + 디스크 캐시. 실패 시 ``\"\"``."""
    sym = "".join(ch for ch in str(code or "") if ch.isdigit()).zfill(6)
    if not sym or sym == "000000":
        return ""
    fetch = fetch_fn or _fetch_naver_basic
    now = float(now_ts if now_ts is not None else datetime.now().timestamp())
    with _LISTING_LOCK:
        cache = _load_listing_cache()
        row = cache.get(sym) if isinstance(cache.get(sym), dict) else None
        if row:
            try:
                age = now - float(row.get("fetched_at") or 0.0)
            except (TypeError, ValueError):
                age = 1e12
            market = normalize_listing(row.get("market"))
            ttl = _LISTING_TTL_SEC if market else _LISTING_MISS_TTL_SEC
            if 0 <= age < ttl:
                return market
        try:
            payload = fetch(sym)
        except Exception:
            payload = {}
        market = parse_listing_payload(payload)
        cache[sym] = {"market": market, "fetched_at": now}
        _save_listing_cache(cache)
        return market


def load_kr_index_snapshot(
    *,
    fetch_change: Callable[[str], float | None] | None = None,
) -> KrIndexSnapshot:
    """KODEX200·KOSPI·KOSDAQ 전일 대비 %. 심볼별 실패는 None. 예외를 올리지 않는다."""
    if fetch_change is None:
        from strategy.market_benchmark import index_daily_change_pct

        fetch_change = index_daily_change_pct

    def _one(symbol: str) -> float | None:
        try:
            return fetch_change(symbol)
        except Exception:
            return None

    return KrIndexSnapshot(
        kodex=_one("069500.KS"),
        kospi=_one("^KS11"),
        kosdaq=_one("^KQ11"),
    )
