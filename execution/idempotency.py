# -*- coding: utf-8 -*-
"""
주문 멱등성 — TWAP 슬라이스·사이클 매수/매도 in-flight·장부 키 보존.

* 동일 ``order_key`` 로 이미 체결 기록이 있으면 **재주문하지 않음**.
* KIS ``rt_cd`` 실패·타임아웃이어도 잔고 증감으로 **체결 여부 보정**(옵션).
* ``buy_inflight`` / ``sell_inflight`` 로 같은 15분 사이클·티커 **중복 시도** 완화.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

try:
    import pytz  # type: ignore
except Exception:  # pragma: no cover
    pytz = None  # noqa: N816

_IDEMPOTENCY_KEY = "order_idempotency"
_BUY_INFLIGHT_KEY = "buy_inflight"
_SELL_INFLIGHT_KEY = "sell_inflight"
_DEFAULT_TTL_HOURS = 168.0
_SUBMITTED_STALE_SEC = 90.0

# 매도 lane — order_key 의 side 를 ``sell:{lane}`` 형태로 만든다.
LANE_SWING_HALF = "swing_half"
LANE_SWING_FULL = "swing_full"
LANE_SCALE_OUT = "scale_out"
LANE_EXIT = "exit"
LANE_MANUAL = "manual"
LANE_PHASE5 = "phase5"


@dataclass(frozen=True)
class SliceFillResult:
    """TWAP 슬라이스 1회 실행 결과."""

    ok: bool
    qty: float
    price: float
    reused: bool = False
    note: str = ""


def ensure_idempotency_state(state: dict) -> None:
    if not isinstance(state, dict):
        return
    state.setdefault(_IDEMPOTENCY_KEY, {})
    state.setdefault(_BUY_INFLIGHT_KEY, {})
    state.setdefault(_SELL_INFLIGHT_KEY, {})


def cycle_tag_15m_kst(now: datetime | None = None) -> str:
    """KST 15분 슬롯 ID (``run_bot`` 매매 사이클과 정렬)."""
    if now is None:
        if pytz is not None:
            now = datetime.now(pytz.timezone("Asia/Seoul"))
        else:
            now = datetime.now()
    slot_min = (int(now.minute) // 15) * 15
    return now.strftime(f"%Y%m%d%H{slot_min:02d}")


def order_key(
    market: str,
    ticker: str,
    side: str,
    cycle_tag: str,
    slice_index: int,
    *,
    lane: str = "",
) -> str:
    m = str(market or "").strip().upper()
    t = str(ticker or "").strip().upper()
    s = str(side or "").strip().lower()
    ln = str(lane or "").strip().lower()
    if ln:
        s = f"{s}:{ln}"
    c = str(cycle_tag or "").strip()
    return f"{m}:{t}:{s}:{c}:{int(slice_index)}"


def sell_order_key(
    market: str,
    ticker: str,
    lane: str,
    cycle_tag: str,
    slice_index: int,
) -> str:
    """매도 전용 — ``sell:{lane}`` 키."""
    return order_key(market, ticker, "sell", cycle_tag, slice_index, lane=lane)


def _records(state: dict) -> dict:
    ensure_idempotency_state(state)
    raw = state.get(_IDEMPOTENCY_KEY)
    return raw if isinstance(raw, dict) else {}


def get_order_record(state: dict, key: str) -> dict | None:
    rec = _records(state).get(str(key))
    return dict(rec) if isinstance(rec, dict) else None


def pop_order_record(state: dict, key: str) -> None:
    """토큰 갱신 후 재주문 등 — 실패·제출 기록 제거."""
    ensure_idempotency_state(state)
    state[_IDEMPOTENCY_KEY].pop(str(key), None)


def put_order_record(state: dict, key: str, record: dict) -> None:
    ensure_idempotency_state(state)
    state[_IDEMPOTENCY_KEY][str(key)] = dict(record)


def persist_idempotency(state: dict, state_path: str | Any) -> bool:
    """``order_idempotency``·in-flight 변경을 디스크에 반영(실패 기록 보존용)."""
    ensure_idempotency_state(state)
    if not state_path:
        return False
    try:
        from execution.guard import save_state

        save_state(str(state_path), state)
        return True
    except Exception:
        return False


def prune_order_idempotency(state: dict, *, max_age_hours: float = _DEFAULT_TTL_HOURS) -> int:
    """오래된 멱등 기록 삭제. 반환: 삭제 건수."""
    ensure_idempotency_state(state)
    cutoff = time.time() - max(1.0, float(max_age_hours)) * 3600.0
    removed = 0
    for k, rec in list(_records(state).items()):
        if not isinstance(rec, dict):
            _records(state).pop(k, None)
            removed += 1
            continue
        try:
            ts = float(rec.get("updated_at", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0 or ts < cutoff:
            _records(state).pop(k, None)
            removed += 1
    return removed


def kis_response_success(resp: Any) -> bool:
    return isinstance(resp, dict) and str(resp.get("rt_cd", "")) == "0"


def extract_kis_odno(resp: Any) -> str:
    """KIS 주문 응답에서 주문번호 후보 추출."""
    if not isinstance(resp, dict):
        return ""
    out = resp.get("output")
    if isinstance(out, dict):
        for k in (
            "ODNO",
            "odno",
            "ORD_NO",
            "ord_no",
            "KRX_FWDG_ORD_NO",
            "ODNO1",
        ):
            v = str(out.get(k, "") or "").strip()
            if v:
                return v
    for k in ("ODNO", "odno"):
        v = str(resp.get(k, "") or "").strip()
        if v:
            return v
    return ""


def extract_kis_order_price(resp: Any, fallback: float) -> float:
    if not isinstance(resp, dict):
        return float(fallback)
    out = resp.get("output")
    if isinstance(out, dict):
        for k in ("ORD_PRIC", "ord_pric", "ORD_UNPR", "ord_unpr"):
            try:
                v = float(out.get(k, 0) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                return v
    return float(fallback)


def kis_balance_stock_qty(balance: Any, ticker: str) -> float | None:
    """KIS 잔고 ``output1`` 에서 종목 수량. 조회 실패 시 None."""
    if not isinstance(balance, dict):
        return None
    code = str(ticker or "").strip()
    rows = balance.get("output1")
    if not isinstance(rows, list):
        return None
    total = 0.0
    found = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        pdno = str(row.get("pdno", row.get("PDNO", "")) or "").strip()
        if pdno != code:
            continue
        found = True
        for k in ("hldg_qty", "HLDG_QTY", "ord_psbl_qty", "qty"):
            try:
                q = float(row.get(k, 0) or 0)
            except (TypeError, ValueError):
                q = 0.0
            if q > 0:
                total += q
                break
    if not found:
        return 0.0
    return float(total)


def balance_suggests_fill(
    qty_before: float | None,
    qty_after: float | None,
    expected_qty: float,
    *,
    tolerance_ratio: float = 0.85,
) -> bool:
    """주문 실패 응답이어도 보유 수량이 ``expected_qty`` 만큼 늘었는지."""
    if qty_before is None or qty_after is None:
        return False
    try:
        b0 = float(qty_before)
        b1 = float(qty_after)
        need = float(expected_qty)
    except (TypeError, ValueError):
        return False
    if need <= 0:
        return False
    return (b1 - b0) >= need * float(tolerance_ratio)


def balance_suggests_sell_fill(
    qty_before: float | None,
    qty_after: float | None,
    expected_qty: float,
    *,
    tolerance_ratio: float = 0.85,
) -> bool:
    """매도 — 보유 수량이 ``expected_qty`` 만큼 줄었는지."""
    if qty_before is None or qty_after is None:
        return False
    try:
        b0 = float(qty_before)
        b1 = float(qty_after)
        need = float(expected_qty)
    except (TypeError, ValueError):
        return False
    if need <= 0:
        return False
    return (b0 - b1) >= need * float(tolerance_ratio)


def _mark_record(
    state: dict,
    key: str,
    *,
    status: str,
    qty: float,
    price: float,
    market: str,
    ticker: str,
    side: str,
    odno: str = "",
    note: str = "",
) -> None:
    put_order_record(
        state,
        key,
        {
            "status": str(status),
            "qty": float(qty),
            "price": float(price),
            "market": str(market),
            "ticker": str(ticker),
            "side": str(side),
            "odno": str(odno or ""),
            "note": str(note or ""),
            "updated_at": time.time(),
        },
    )


def slice_fill_from_record(state: dict, key: str) -> SliceFillResult | None:
    rec = get_order_record(state, key)
    if not rec or str(rec.get("status")) != "filled":
        return None
    try:
        q = float(rec.get("qty", 0) or 0)
        p = float(rec.get("price", 0) or 0)
    except (TypeError, ValueError):
        return None
    if q <= 0:
        return None
    return SliceFillResult(
        ok=True,
        qty=q,
        price=p if p > 0 else 0.0,
        reused=True,
        note=str(rec.get("note") or "멱등 캐시"),
    )


def run_kis_buy_slice_idempotent(
    state: dict,
    *,
    market: str,
    ticker: str,
    slice_index: int,
    qty: int,
    cycle_tag: str,
    place_order: Callable[[], Any],
    fallback_price: float,
    balance_qty_fn: Callable[[], float | None] | None = None,
    qty_before: float | None = None,
    max_retries: int = 3,
    test_mode: bool = False,
) -> SliceFillResult:
    """
    KIS 국·미 매수 슬라이스 1회 — 멱등 키·잔고 검증·재시도 제한.

    ``place_order`` 는 인자 없이 호출되며 KIS 응답 dict 를 반환해야 한다.
    """
    q = int(qty)
    fp = float(fallback_price)
    if q <= 0 or fp <= 0:
        return SliceFillResult(False, 0.0, 0.0, note="수량·가격 무효")

    key = order_key(market, ticker, "buy", cycle_tag, slice_index)
    cached = slice_fill_from_record(state, key)
    if cached is not None:
        return cached

    if test_mode:
        _mark_record(
            state,
            key,
            status="filled",
            qty=float(q),
            price=fp,
            market=market,
            ticker=ticker,
            side="buy",
            note="TEST_MODE",
        )
        return SliceFillResult(True, float(q), fp, note="TEST_MODE")

    rec = get_order_record(state, key)
    if rec and str(rec.get("status")) == "submitted":
        try:
            age = time.time() - float(rec.get("updated_at", 0) or 0)
        except (TypeError, ValueError):
            age = 999.0
        if age < _SUBMITTED_STALE_SEC:
            if balance_qty_fn is not None:
                q_now = balance_qty_fn()
                if balance_suggests_fill(qty_before, q_now, q):
                    fill_p = extract_kis_order_price({}, fp)
                    _mark_record(
                        state,
                        key,
                        status="filled",
                        qty=float(q),
                        price=fill_p,
                        market=market,
                        ticker=ticker,
                        side="buy",
                        note="잔고검증(제출후)",
                    )
                    return SliceFillResult(
                        True,
                        float(q),
                        fill_p,
                        note="잔고검증(제출후)",
                    )

    resp: Any = None
    attempts = max(1, int(max_retries))
    for attempt in range(attempts):
        _mark_record(
            state,
            key,
            status="submitted",
            qty=float(q),
            price=fp,
            market=market,
            ticker=ticker,
            side="buy",
            note=f"attempt {attempt + 1}/{attempts}",
        )
        try:
            resp = place_order()
        except Exception as exc:
            resp = {"rt_cd": "1", "msg1": str(exc)}

        if kis_response_success(resp):
            fill_p = extract_kis_order_price(resp, fp)
            odno = extract_kis_odno(resp)
            _mark_record(
                state,
                key,
                status="filled",
                qty=float(q),
                price=fill_p,
                market=market,
                ticker=ticker,
                side="buy",
                odno=odno,
                note="rt_cd=0",
            )
            return SliceFillResult(True, float(q), fill_p, note="체결")

        if balance_qty_fn is not None:
            q_now = balance_qty_fn()
            if balance_suggests_fill(qty_before, q_now, q):
                fill_p = extract_kis_order_price(resp, fp)
                _mark_record(
                    state,
                    key,
                    status="filled",
                    qty=float(q),
                    price=fill_p,
                    market=market,
                    ticker=ticker,
                    side="buy",
                    odno=extract_kis_odno(resp),
                    note="잔고검증(실패응답)",
                )
                return SliceFillResult(
                    True,
                    float(q),
                    fill_p,
                    note="잔고검증(실패응답)",
                )

        if attempt < attempts - 1:
            time.sleep(1.0)

    _mark_record(
        state,
        key,
        status="failed",
        qty=0.0,
        price=0.0,
        market=market,
        ticker=ticker,
        side="buy",
        note=str((resp or {}).get("msg1", "실패") if isinstance(resp, dict) else "실패"),
    )
    return SliceFillResult(
        False,
        0.0,
        0.0,
        note=str((resp or {}).get("msg1", "") if isinstance(resp, dict) else ""),
    )


def binance_client_order_id(order_key_str: str) -> str:
    """바이낸스 ``newClientOrderId`` (영숫자, 36자 이내)."""
    digest = hashlib.sha256(str(order_key_str).encode("utf-8")).hexdigest()[:28]
    return f"bot{digest}"


def run_binance_buy_idempotent(
    state: dict,
    *,
    market: str,
    ticker: str,
    slice_index: int,
    cycle_tag: str,
    spend_usdt: float,
    place_order: Callable[[str], Any],
    fallback_price: float,
    test_mode: bool = False,
) -> SliceFillResult:
    """바이낸스 USDT 매수 1슬라이스 — clientOrderId 멱등."""
    spend = float(spend_usdt)
    fp = float(fallback_price)
    if spend <= 0:
        return SliceFillResult(False, 0.0, 0.0, note="USDT 무효")

    key = order_key(market, ticker, "buy", cycle_tag, slice_index)
    cached = slice_fill_from_record(state, key)
    if cached is not None:
        return cached

    if test_mode:
        _mark_record(
            state,
            key,
            status="filled",
            qty=spend / fp if fp > 0 else 0.0,
            price=fp,
            market=market,
            ticker=ticker,
            side="buy",
            note="TEST_MODE",
        )
        return SliceFillResult(True, spend / fp if fp > 0 else 0.0, fp, note="TEST_MODE")

    cid = binance_client_order_id(key)
    try:
        order = place_order(cid)
    except Exception as exc:
        _mark_record(
            state,
            key,
            status="failed",
            qty=0.0,
            price=0.0,
            market=market,
            ticker=ticker,
            side="buy",
            note=str(exc),
        )
        return SliceFillResult(False, 0.0, 0.0, note=str(exc))

    if not order:
        _mark_record(
            state,
            key,
            status="failed",
            qty=0.0,
            price=0.0,
            market=market,
            ticker=ticker,
            side="buy",
            note="order empty",
        )
        return SliceFillResult(False, 0.0, 0.0, note="order empty")

    try:
        from api import binance_api

        avg, filled = binance_api.order_avg_fill_usdt(order if isinstance(order, dict) else {})
    except Exception:
        avg, filled = 0.0, 0.0

    if filled <= 0 and spend > 0 and fp > 0:
        filled = spend / fp
    fill_p = avg if avg > 0 else fp
    _mark_record(
        state,
        key,
        status="filled",
        qty=float(filled),
        price=float(fill_p),
        market=market,
        ticker=ticker,
        side="buy",
        odno=cid,
        note="binance",
    )
    return SliceFillResult(True, float(filled), float(fill_p), note="체결")


def run_upbit_buy_slice_idempotent(
    state: dict,
    *,
    market: str,
    ticker: str,
    slice_index: int,
    pay_krw: float,
    cycle_tag: str,
    place_order: Callable[[], Any],
    fallback_price: float,
    balance_qty_fn: Callable[[], float | None] | None = None,
    qty_before: float | None = None,
    test_mode: bool = False,
) -> SliceFillResult:
    """업비트 KRW 시장가 매수 1슬라이스 — 장부 키·잔고 검증 멱등."""
    pay = float(pay_krw)
    fp = float(fallback_price)
    if pay <= 0 or fp <= 0:
        return SliceFillResult(False, 0.0, 0.0, note="KRW·가격 무효")

    key = order_key(market, ticker, "buy", cycle_tag, slice_index)
    cached = slice_fill_from_record(state, key)
    if cached is not None:
        return cached

    est_qty = pay / fp if fp > 0 else 0.0

    if test_mode:
        _mark_record(
            state,
            key,
            status="filled",
            qty=float(est_qty),
            price=fp,
            market=market,
            ticker=ticker,
            side="buy",
            note="TEST_MODE",
        )
        return SliceFillResult(True, float(est_qty), fp, note="TEST_MODE")

    rec = get_order_record(state, key)
    if rec and str(rec.get("status")) == "submitted":
        try:
            age = time.time() - float(rec.get("updated_at", 0) or 0)
        except (TypeError, ValueError):
            age = 999.0
        if age < _SUBMITTED_STALE_SEC and balance_qty_fn is not None:
            q_now = balance_qty_fn()
            if balance_suggests_fill(qty_before, q_now, est_qty):
                _mark_record(
                    state,
                    key,
                    status="filled",
                    qty=float(est_qty),
                    price=fp,
                    market=market,
                    ticker=ticker,
                    side="buy",
                    note="잔고검증(제출후)",
                )
                return SliceFillResult(True, float(est_qty), fp, note="잔고검증(제출후)")

    _mark_record(
        state,
        key,
        status="submitted",
        qty=float(est_qty),
        price=fp,
        market=market,
        ticker=ticker,
        side="buy",
        note="upbit",
    )
    try:
        resp = place_order()
    except Exception as exc:
        resp = None
        note = str(exc)
    else:
        note = "체결" if resp else "응답 없음"

    if resp:
        _mark_record(
            state,
            key,
            status="filled",
            qty=float(est_qty),
            price=fp,
            market=market,
            ticker=ticker,
            side="buy",
            note="upbit",
        )
        return SliceFillResult(True, float(est_qty), fp, note="체결")

    if balance_qty_fn is not None:
        q_now = balance_qty_fn()
        if balance_suggests_fill(qty_before, q_now, est_qty):
            _mark_record(
                state,
                key,
                status="filled",
                qty=float(est_qty),
                price=fp,
                market=market,
                ticker=ticker,
                side="buy",
                note="잔고검증(실패응답)",
            )
            return SliceFillResult(True, float(est_qty), fp, note="잔고검증(실패응답)")

    _mark_record(
        state,
        key,
        status="failed",
        qty=0.0,
        price=0.0,
        market=market,
        ticker=ticker,
        side="buy",
        note=str(note),
    )
    return SliceFillResult(False, 0.0, 0.0, note=str(note))


def _inflight_key(market: str, ticker: str, cycle_tag: str) -> str:
    return f"{str(market).upper()}:{str(ticker).upper()}:{str(cycle_tag)}"


def try_acquire_buy_inflight(
    state: dict,
    market: str,
    ticker: str,
    cycle_tag: str,
    *,
    ttl_sec: float = 900.0,
) -> bool:
    """
    같은 사이클·티커 매수 TWAP 진행 중 표시.

    Returns:
        True — 획득 성공(이번에 매수 시도 가능)
        False — 이미 다른 TWAP 가 진행 중
    """
    ensure_idempotency_state(state)
    k = _inflight_key(market, ticker, cycle_tag)
    now = time.time()
    raw = state.get(_BUY_INFLIGHT_KEY)
    if not isinstance(raw, dict):
        raw = {}
        state[_BUY_INFLIGHT_KEY] = raw
    rec = raw.get(k)
    if isinstance(rec, dict):
        try:
            until = float(rec.get("until", 0) or 0)
        except (TypeError, ValueError):
            until = 0.0
        if until > now:
            return False
    raw[k] = {"until": now + max(60.0, float(ttl_sec)), "cycle": str(cycle_tag)}
    return True


def release_buy_inflight(state: dict, market: str, ticker: str, cycle_tag: str) -> None:
    ensure_idempotency_state(state)
    k = _inflight_key(market, ticker, cycle_tag)
    raw = state.get(_BUY_INFLIGHT_KEY)
    if isinstance(raw, dict):
        raw.pop(k, None)


def is_buy_inflight(state: dict, market: str, ticker: str, cycle_tag: str) -> bool:
    ensure_idempotency_state(state)
    k = _inflight_key(market, ticker, cycle_tag)
    raw = state.get(_BUY_INFLIGHT_KEY)
    if not isinstance(raw, dict):
        return False
    rec = raw.get(k)
    if not isinstance(rec, dict):
        return False
    try:
        return float(rec.get("until", 0) or 0) > time.time()
    except (TypeError, ValueError):
        return False


def coin_base_qty_from_balances(balances: Any, internal_ticker: str) -> float | None:
    """업비트/바이낸스 ``get_balances()`` 형식에서 base 수량."""
    if not balances:
        return None
    try:
        from api import coin_broker

        cur = coin_broker.held_ticker_row(
            {"currency": str(internal_ticker).split("-", 1)[-1]}
        )
        if not cur:
            return 0.0
        for b in balances:
            if not isinstance(b, dict):
                continue
            if coin_broker.held_ticker_row(b) == internal_ticker:
                return float(b.get("balance", 0) or 0)
        return 0.0
    except Exception:
        return None


def should_skip_new_buy(
    state: dict,
    market: str,
    ticker: str,
    cycle_tag: str,
    *,
    held_tickers: set[str] | list[str] | None = None,
) -> tuple[bool, str]:
    """
    스캔 루프용 — 보유·in-flight 멱등 게이트.

    Returns:
        (skip, reason)
    """
    t = str(ticker or "").strip().upper()
    held = {str(x).strip().upper() for x in (held_tickers or [])}
    if t in held:
        return True, "이미 보유"
    if is_buy_inflight(state, market, t, cycle_tag):
        return True, "매수 TWAP 진행 중(멱등)"
    return False, ""


def _sell_inflight_key(market: str, ticker: str, lane: str, cycle_tag: str) -> str:
    return (
        f"{str(market).upper()}:{str(ticker).upper()}:"
        f"{str(lane).strip().lower()}:{str(cycle_tag)}"
    )


def try_acquire_sell_inflight(
    state: dict,
    market: str,
    ticker: str,
    lane: str,
    cycle_tag: str,
    *,
    ttl_sec: float = 600.0,
) -> bool:
    """동일 사이클·티커·lane 매도 단발/스윙 — 중복 API 호출 방지."""
    ensure_idempotency_state(state)
    k = _sell_inflight_key(market, ticker, lane, cycle_tag)
    now = time.time()
    raw = state.get(_SELL_INFLIGHT_KEY)
    if not isinstance(raw, dict):
        raw = {}
        state[_SELL_INFLIGHT_KEY] = raw
    rec = raw.get(k)
    if isinstance(rec, dict):
        try:
            until = float(rec.get("until", 0) or 0)
        except (TypeError, ValueError):
            until = 0.0
        if until > now:
            return False
    raw[k] = {"until": now + max(60.0, float(ttl_sec)), "cycle": str(cycle_tag), "lane": str(lane)}
    return True


def release_sell_inflight(
    state: dict,
    market: str,
    ticker: str,
    lane: str,
    cycle_tag: str,
) -> None:
    ensure_idempotency_state(state)
    k = _sell_inflight_key(market, ticker, lane, cycle_tag)
    raw = state.get(_SELL_INFLIGHT_KEY)
    if isinstance(raw, dict):
        raw.pop(k, None)


def is_sell_inflight(state: dict, market: str, ticker: str, lane: str, cycle_tag: str) -> bool:
    ensure_idempotency_state(state)
    k = _sell_inflight_key(market, ticker, lane, cycle_tag)
    raw = state.get(_SELL_INFLIGHT_KEY)
    if not isinstance(raw, dict):
        return False
    rec = raw.get(k)
    if not isinstance(rec, dict):
        return False
    try:
        return float(rec.get("until", 0) or 0) > time.time()
    except (TypeError, ValueError):
        return False


def run_kis_sell_slice_idempotent(
    state: dict,
    *,
    market: str,
    ticker: str,
    lane: str,
    slice_index: int,
    qty: int,
    cycle_tag: str,
    place_order: Callable[[], Any],
    fallback_price: float,
    balance_qty_fn: Callable[[], float | None] | None = None,
    qty_before: float | None = None,
    max_retries: int = 3,
    test_mode: bool = False,
) -> SliceFillResult:
    """KIS 국·미 매도 1회 — 멱등 키·잔고 감소 검증."""
    q = int(qty)
    fp = float(fallback_price)
    if q <= 0 or fp <= 0:
        return SliceFillResult(False, 0.0, 0.0, note="수량·가격 무효")

    key = sell_order_key(market, ticker, lane, cycle_tag, slice_index)
    cached = slice_fill_from_record(state, key)
    if cached is not None:
        return cached

    if test_mode:
        _mark_record(
            state,
            key,
            status="filled",
            qty=float(q),
            price=fp,
            market=market,
            ticker=ticker,
            side="sell",
            note="TEST_MODE",
        )
        return SliceFillResult(True, float(q), fp, note="TEST_MODE")

    rec = get_order_record(state, key)
    if rec and str(rec.get("status")) == "submitted":
        try:
            age = time.time() - float(rec.get("updated_at", 0) or 0)
        except (TypeError, ValueError):
            age = 999.0
        if age < _SUBMITTED_STALE_SEC and balance_qty_fn is not None:
            q_now = balance_qty_fn()
            if balance_suggests_sell_fill(qty_before, q_now, q):
                _mark_record(
                    state,
                    key,
                    status="filled",
                    qty=float(q),
                    price=fp,
                    market=market,
                    ticker=ticker,
                    side="sell",
                    note="잔고검증(제출후)",
                )
                return SliceFillResult(True, float(q), fp, note="잔고검증(제출후)")

    resp: Any = None
    attempts = max(1, int(max_retries))
    for attempt in range(attempts):
        _mark_record(
            state,
            key,
            status="submitted",
            qty=float(q),
            price=fp,
            market=market,
            ticker=ticker,
            side="sell",
            note=f"attempt {attempt + 1}/{attempts}",
        )
        try:
            resp = place_order()
        except Exception as exc:
            resp = {"rt_cd": "1", "msg1": str(exc)}

        if kis_response_success(resp):
            fill_p = extract_kis_order_price(resp, fp)
            _mark_record(
                state,
                key,
                status="filled",
                qty=float(q),
                price=fill_p,
                market=market,
                ticker=ticker,
                side="sell",
                odno=extract_kis_odno(resp),
                note="rt_cd=0",
            )
            return SliceFillResult(True, float(q), fill_p, note="체결")

        if balance_qty_fn is not None:
            q_now = balance_qty_fn()
            if balance_suggests_sell_fill(qty_before, q_now, q):
                fill_p = extract_kis_order_price(resp, fp)
                _mark_record(
                    state,
                    key,
                    status="filled",
                    qty=float(q),
                    price=fill_p,
                    market=market,
                    ticker=ticker,
                    side="sell",
                    odno=extract_kis_odno(resp),
                    note="잔고검증(실패응답)",
                )
                return SliceFillResult(True, float(q), fill_p, note="잔고검증(실패응답)")

        if attempt < attempts - 1:
            time.sleep(1.0)

    _mark_record(
        state,
        key,
        status="failed",
        qty=0.0,
        price=0.0,
        market=market,
        ticker=ticker,
        side="sell",
        note=str((resp or {}).get("msg1", "실패") if isinstance(resp, dict) else "실패"),
    )
    return SliceFillResult(
        False,
        0.0,
        0.0,
        note=str((resp or {}).get("msg1", "") if isinstance(resp, dict) else ""),
    )


def run_binance_sell_idempotent(
    state: dict,
    *,
    market: str,
    ticker: str,
    lane: str,
    slice_index: int,
    cycle_tag: str,
    qty: float,
    place_order: Callable[[str], Any],
    fallback_price: float,
    balance_qty_fn: Callable[[], float | None] | None = None,
    qty_before: float | None = None,
    test_mode: bool = False,
) -> SliceFillResult:
    """바이낸스 base 매도 — clientOrderId 멱등."""
    q = float(qty)
    fp = float(fallback_price)
    if q <= 0:
        return SliceFillResult(False, 0.0, 0.0, note="수량 무효")

    key = sell_order_key(market, ticker, lane, cycle_tag, slice_index)
    cached = slice_fill_from_record(state, key)
    if cached is not None:
        return cached

    if test_mode:
        _mark_record(
            state,
            key,
            status="filled",
            qty=q,
            price=fp,
            market=market,
            ticker=ticker,
            side="sell",
            note="TEST_MODE",
        )
        return SliceFillResult(True, q, fp, note="TEST_MODE")

    cid = binance_client_order_id(key)
    try:
        order = place_order(cid)
    except Exception as exc:
        _mark_record(
            state,
            key,
            status="failed",
            qty=0.0,
            price=0.0,
            market=market,
            ticker=ticker,
            side="sell",
            note=str(exc),
        )
        return SliceFillResult(False, 0.0, 0.0, note=str(exc))

    if not order:
        if balance_qty_fn is not None:
            q_now = balance_qty_fn()
            if balance_suggests_sell_fill(qty_before, q_now, q):
                _mark_record(
                    state,
                    key,
                    status="filled",
                    qty=q,
                    price=fp,
                    market=market,
                    ticker=ticker,
                    side="sell",
                    note="잔고검증(빈응답)",
                )
                return SliceFillResult(True, q, fp, note="잔고검증(빈응답)")
        _mark_record(
            state,
            key,
            status="failed",
            qty=0.0,
            price=0.0,
            market=market,
            ticker=ticker,
            side="sell",
            note="order empty",
        )
        return SliceFillResult(False, 0.0, 0.0, note="order empty")

    try:
        from api import binance_api

        avg, filled = binance_api.order_avg_fill_usdt(order if isinstance(order, dict) else {})
    except Exception:
        avg, filled = 0.0, 0.0

    if filled <= 0:
        filled = q
    fill_p = avg if avg > 0 else fp
    _mark_record(
        state,
        key,
        status="filled",
        qty=float(filled),
        price=float(fill_p),
        market=market,
        ticker=ticker,
        side="sell",
        odno=cid,
        note="binance",
    )
    return SliceFillResult(True, float(filled), float(fill_p), note="체결")


def run_upbit_sell_slice_idempotent(
    state: dict,
    *,
    market: str,
    ticker: str,
    lane: str,
    slice_index: int,
    cycle_tag: str,
    qty: float,
    place_order: Callable[[], Any],
    fallback_price: float,
    balance_qty_fn: Callable[[], float | None] | None = None,
    qty_before: float | None = None,
    test_mode: bool = False,
) -> SliceFillResult:
    """업비트 시장가 매도 1회."""
    q = float(qty)
    fp = float(fallback_price)
    if q <= 0 or fp <= 0:
        return SliceFillResult(False, 0.0, 0.0, note="수량·가격 무효")

    key = sell_order_key(market, ticker, lane, cycle_tag, slice_index)
    cached = slice_fill_from_record(state, key)
    if cached is not None:
        return cached

    if test_mode:
        _mark_record(
            state,
            key,
            status="filled",
            qty=q,
            price=fp,
            market=market,
            ticker=ticker,
            side="sell",
            note="TEST_MODE",
        )
        return SliceFillResult(True, q, fp, note="TEST_MODE")

    _mark_record(
        state,
        key,
        status="submitted",
        qty=q,
        price=fp,
        market=market,
        ticker=ticker,
        side="sell",
        note="upbit",
    )
    try:
        resp = place_order()
    except Exception as exc:
        resp = None
        note = str(exc)
    else:
        note = "체결" if resp else "응답 없음"

    if resp:
        _mark_record(
            state,
            key,
            status="filled",
            qty=q,
            price=fp,
            market=market,
            ticker=ticker,
            side="sell",
            note="upbit",
        )
        return SliceFillResult(True, q, fp, note="체결")

    if balance_qty_fn is not None:
        q_now = balance_qty_fn()
        if balance_suggests_sell_fill(qty_before, q_now, q):
            _mark_record(
                state,
                key,
                status="filled",
                qty=q,
                price=fp,
                market=market,
                ticker=ticker,
                side="sell",
                note="잔고검증(실패응답)",
            )
            return SliceFillResult(True, q, fp, note="잔고검증(실패응답)")

    _mark_record(
        state,
        key,
        status="failed",
        qty=0.0,
        price=0.0,
        market=market,
        ticker=ticker,
        side="sell",
        note=str(note),
    )
    return SliceFillResult(False, 0.0, 0.0, note=str(note))


def _parse_sell_lane_key(key: str) -> tuple[str, str, str, str, int] | None:
    """``MARKET:TICKER:sell:LANE:CYCLE:SLICE`` → (market, ticker, lane, cycle, slice)."""
    parts = str(key or "").split(":")
    if len(parts) < 6 or parts[2] != "sell":
        return None
    try:
        return parts[0], parts[1], parts[3], parts[4], int(parts[5])
    except (TypeError, ValueError):
        return None


def aggregate_filled_sells_cycle(state: dict, cycle_tag: str) -> dict[tuple[str, str, str], dict]:
    """현재 ``cycle_tag`` 의 filled 매도 레코드를 (market, ticker, lane) 별로 합산."""
    out: dict[tuple[str, str, str], dict] = {}
    tag = str(cycle_tag or "").strip()
    for key, rec in _records(state).items():
        if not isinstance(rec, dict) or rec.get("status") != "filled":
            continue
        parsed = _parse_sell_lane_key(key)
        if not parsed:
            continue
        mkt, tk, lane, cyc, _sl = parsed
        if cyc != tag:
            continue
        sold = float(rec.get("qty", 0) or 0)
        if sold <= 0:
            continue
        px = float(rec.get("price", 0) or 0)
        bucket = out.setdefault((mkt, tk, lane), {"qty": 0.0, "price": px})
        bucket["qty"] += sold
        if px > 0:
            bucket["price"] = px
    return out


def lane_has_filled_sell(
    state: dict,
    market: str,
    ticker: str,
    lane: str,
    cycle_tag: str,
) -> bool:
    """이번 사이클에 해당 lane 매도가 멱등 장부에 filled 로 남았는지."""
    m = str(market or "").strip().upper()
    t = str(ticker or "").strip().upper()
    ln = str(lane or "").strip().lower()
    tag = str(cycle_tag or "").strip()
    agg = aggregate_filled_sells_cycle(state, tag)
    return float(agg.get((m, t, ln), {}).get("qty", 0) or 0) > 0


def reconcile_positions_for_cycle(
    state: dict,
    cycle_tag: str,
    state_path: str | Any,
    *,
    post_partial_fn: Callable[..., dict] | None = None,
) -> int:
    """
    ``order_idempotency`` filled 매도 ↔ ``positions`` 불일치를 보정.

    체결 성공 후 ``save_state`` 만 실패한 경우 사이클 시작·동기화 직후 호출.
    """
    from execution.ledger_apply import persist_position_remove, persist_position_set
    from execution.scale_out import position_scale_out_done, post_partial_ledger

    post_partial = post_partial_fn or post_partial_ledger
    fixes = 0
    positions = state.get("positions")
    if not isinstance(positions, dict):
        return 0

    for (mkt, tk, lane), agg in aggregate_filled_sells_cycle(state, cycle_tag).items():
        sold = float(agg.get("qty", 0) or 0)
        px = float(agg.get("price", 0) or 0)
        if sold <= 0:
            continue
        pos = positions.get(tk)
        if lane in (LANE_EXIT, LANE_SWING_FULL):
            if tk not in positions:
                continue
            ctx = f"정합 FULL/EXIT {mkt}:{tk}"

            def _mut_exit(st: dict, _t=tk) -> None:
                from execution.guard import set_cooldown

                set_cooldown(st, _t)

            if persist_position_remove(
                state,
                tk,
                context=ctx,
                state_path=state_path,
                mutate_fn=_mut_exit,
            ):
                fixes += 1
                print(f"  🔧 [장부 정합] {ctx} — positions 삭제 반영")
            continue

        if lane not in (LANE_SWING_HALF, LANE_SCALE_OUT):
            continue
        if not isinstance(pos, dict):
            continue
        if lane == LANE_SCALE_OUT and position_scale_out_done(pos):
            continue
        qty_b = float(pos.get("qty", 0) or 0)
        if qty_b <= 0:
            continue
        set_so = lane == LANE_SCALE_OUT
        new_pos = post_partial(pos, sold, px, qty_b, set_scale_out_done=set_so)
        ctx = f"정합 {lane} {mkt}:{tk}"
        if persist_position_set(state, tk, new_pos, context=ctx, state_path=state_path):
            fixes += 1
            print(f"  🔧 [장부 정합] {ctx} — 부분매도 장부 반영 (sold={sold:g})")
    return fixes


def reconcile_ticker_lane(
    state: dict,
    market: str,
    ticker: str,
    lane: str,
    cycle_tag: str,
    state_path: str | Any,
    *,
    post_partial_fn: Callable[..., dict] | None = None,
) -> bool:
    """단일 티커·lane — HALF/Scale-Out 게이트 직전 1회 보정."""
    from execution.ledger_apply import persist_position_remove, persist_position_set
    from execution.scale_out import position_scale_out_done, post_partial_ledger

    post_partial = post_partial_fn or post_partial_ledger
    m = str(market or "").strip().upper()
    t = str(ticker or "").strip().upper()
    ln = str(lane or "").strip().lower()
    tag = str(cycle_tag or "").strip()
    bucket = aggregate_filled_sells_cycle(state, tag).get((m, t, ln))
    if not bucket or float(bucket.get("qty", 0) or 0) <= 0:
        return False
    sold = float(bucket.get("qty", 0) or 0)
    px = float(bucket.get("price", 0) or 0)
    positions = state.get("positions")
    if not isinstance(positions, dict):
        return False
    if ln in (LANE_EXIT, LANE_SWING_FULL):
        if t not in positions:
            return False
        ctx = f"정합 {ln} {m}:{t}"
        ok = persist_position_remove(state, t, context=ctx, state_path=state_path)
        if ok:
            print(f"  🔧 [장부 정합] {ctx}")
        return ok
    pos = positions.get(t)
    if not isinstance(pos, dict):
        return False
    if ln == LANE_SCALE_OUT and position_scale_out_done(pos):
        return False
    qty_b = float(pos.get("qty", 0) or 0)
    if qty_b <= 0:
        return False
    new_pos = post_partial(pos, sold, px, qty_b, set_scale_out_done=(ln == LANE_SCALE_OUT))
    ctx = f"정합 {ln} {m}:{t}"
    ok = persist_position_set(state, t, new_pos, context=ctx, state_path=state_path)
    if ok:
        print(f"  🔧 [장부 정합] {ctx} (sold={sold:g})")
    return ok
