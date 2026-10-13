# -*- coding: utf-8 -*-
"""섀도우 이벤트 이후 가격 — 1/3/5 거래일 종가 vs 이벤트 가격.

네트워크 없음. 일봉 행은 ``{"date","close"}`` 오름차순이라고 가정하지 않고 정렬한다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

HORIZONS = (1, 3, 5)
_BLOCK_ACTIONS = {"would_block", "block"}
_SELL_ACTIONS = {"would_sell", "sell"}


def load_shadow_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def event_session_day(event: dict[str, Any]) -> str:
    session = str(event.get("session_date") or "").strip()
    if len(session) >= 10:
        return session[:10]
    ts = str(event.get("ts_kst") or "").strip()
    return ts[:10]


def forward_returns(
    rows: Iterable[dict[str, Any]] | None,
    event_day: str,
    ref_price: float,
    horizons: tuple[int, ...] = HORIZONS,
) -> dict[str, Any]:
    """이벤트 **다음날 이후** 거래일 종가 수익률(%).

    1거래일 = 이벤트 날짜보다 늦은 첫 일봉. 당일 봉은 기준가로 쓰지 않는다.
    """
    out: dict[str, Any] = {"ok": False, "note": ""}
    try:
        ref = float(ref_price)
    except (TypeError, ValueError):
        ref = 0.0
    day = str(event_day or "")[:10]
    if ref <= 0 or len(day) < 10:
        out["note"] = "기준가 또는 날짜 없음"
        return out
    ordered = sorted(
        (r for r in (rows or []) if str(r.get("date") or "")[:10]),
        key=lambda r: str(r.get("date"))[:10],
    )
    after = [r for r in ordered if str(r.get("date"))[:10] > day]
    if not after:
        out["note"] = "이후 일봉 없음"
        return out
    found = False
    for h in horizons:
        if h <= 0 or h > len(after):
            continue
        try:
            close = float(after[h - 1].get("close") or 0.0)
        except (TypeError, ValueError):
            close = 0.0
        if close <= 0:
            continue
        out[f"ret_{h}d"] = round((close - ref) / ref * 100.0, 2)
        out[f"close_{h}d"] = close
        out[f"date_{h}d"] = str(after[h - 1].get("date"))[:10]
        found = True
    out["ok"] = found
    if not found:
        out["note"] = "1/3/5거래일 종가 아직 없음"
    return out


def interpret(rule: str, action: str, rets: dict[str, Any]) -> str:
    """한 줄 해석. 숫자 없으면 노트."""
    if not rets.get("ok"):
        return str(rets.get("note") or "")
    ret5 = rets.get("ret_5d")
    sample = ret5 if ret5 is not None else rets.get("ret_3d", rets.get("ret_1d"))
    if sample is None:
        return ""
    act = str(action or "")
    if act in _BLOCK_ACTIONS or str(rule) == "earnings_block":
        if float(sample) > 0:
            return "가상매수 이후 상승 — 차단은 기회비용"
        if float(sample) < 0:
            return "가상매수 이후 하락 — 차단이 손실을 피함"
        return "가상매수 이후 보합"
    if act in _SELL_ACTIONS or str(rule) == "kr_crash_exit":
        if float(sample) < 0:
            return "가상매도 이후 하락 — 청산이 유리"
        if float(sample) > 0:
            return "가상매도 이후 상승 — 청산이 이름"
        return "가상매도 이후 보합"
    return ""


def is_outcome_event(event: dict[str, Any]) -> bool:
    action = str(event.get("action") or "")
    return action in _BLOCK_ACTIONS or action in _SELL_ACTIONS
