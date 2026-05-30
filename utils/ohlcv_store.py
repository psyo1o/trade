# -*- coding: utf-8 -*-
"""일봉 OHLCV 디스크 캐시 — yfinance 401 시에도 최근 200봉 재사용."""
from __future__ import annotations

import json
import time
from pathlib import Path

_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "ohlcv_cache"
_DEFAULT_MAX_AGE_SEC = 72 * 3600  # 3일

# 매매·지표별 최소 일봉 수 (run_bot / strategy.rules 와 동기화)
OHLCV_MIN_BARS = {
    "sell_loop": 14,       # 매도 루프·ATR 등 최소
    "v8_exit": 20,         # V8 get_final_exit_price
    "swing": 60,           # 스윙 진입·피보·구름·매도선
    "v8_entry": 120,       # V8 calculate_pro_signals (120MA)
    "cache_target": 200,   # get_cached_ohlcv 목표·ma200 여유
}


def ohlcv_len_ok(ohlcv: list | None, purpose: str) -> bool:
    """``purpose`` 키에 대해 봉 수가 충분한지."""
    need = int(OHLCV_MIN_BARS.get(purpose, 0))
    if need <= 0:
        return bool(ohlcv)
    return bool(ohlcv) and len(ohlcv) >= need


def _cache_path(ticker: str) -> Path:
    key = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(ticker).strip().upper())
    return _CACHE_DIR / f"{key}.json"


def load_disk_ohlcv(ticker: str, *, max_age_sec: float = _DEFAULT_MAX_AGE_SEC) -> list | None:
    path = _cache_path(ticker)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        saved = float(raw.get("saved_at", 0))
        if max_age_sec > 0 and (time.time() - saved) > max_age_sec:
            return None
        rows = raw.get("ohlcv")
        return rows if isinstance(rows, list) and rows else None
    except Exception:
        return None


def save_disk_ohlcv(ticker: str, ohlcv: list) -> None:
    if not ohlcv or len(ohlcv) < 14:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": time.time(), "ohlcv": ohlcv}
        _cache_path(ticker).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
