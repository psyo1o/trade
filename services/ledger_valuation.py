# -*- coding: utf-8 -*-
"""
장부(positions) + 시세로 예수·총평·보유 목록 추정 — KIS 잔고 API 없이 GUI·매도 루프·Phase5 보조.

봇만 매매할 때 HTS/MTS 실보유와 장부가 일치한다는 전제.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from utils.helpers import is_coin_ticker, normalize_ticker

_LEGACY_CASH_KEYS = {"KR": "last_kr_cash_krw", "US": "last_us_cash_usd"}
# 레거시 스냅샷 키 — 읽을 때 제거 (Phase 2: guard → 규칙 3 급감 거부로 대체)
_LEGACY_BUY_CASH_GUARD_KEY = "_buy_cash_guard"
_RECENT_BUY_MAX_AGE_SEC = 24 * 3600.0
EQUITY_DIVERGENCE_WARN_PCT = 10.0
EQUITY_DIVERGENCE_CONFIRM_PCT = 15.0


def _market_norm(market: str) -> str:
    return str(market or "").strip().upper()


def _kis_snap_bucket(state: dict, market: str) -> dict:
    snap = state.get("last_kis_display_snapshot")
    if not isinstance(snap, dict):
        return {}
    key = "kr" if _market_norm(market) == "KR" else "us"
    part = snap.get(key)
    if not isinstance(part, dict):
        return {}
    if _LEGACY_BUY_CASH_GUARD_KEY in part:
        part = dict(part)
        part.pop(_LEGACY_BUY_CASH_GUARD_KEY, None)
    return part


def _aux_total_looks_like_glitch(aux_total: float, snap_total: float, market: str) -> bool:
    """장부 추정이 스냅샷 대비 급감하면 API/라벨 이상 — 스냅샷을 쓴다."""
    m = _market_norm(market)
    min_snap = 10_000.0 if m == "KR" else 50.0
    return snap_total >= min_snap and aux_total > 0 and aux_total < snap_total * 0.55


def kis_display_total(state: dict, market: str) -> float:
    """국·미 총평 — KIS 스냅샷. 장부+시세 루프는 ``_phase5_aux_sync`` 추정값 우선."""
    m = _market_norm(market)
    part = _kis_snap_bucket(state, market)
    snap_t = float(part.get("total", 0) or 0)
    aux = state.get("_phase5_aux_sync")
    if isinstance(aux, dict) and aux.get("ledger_only"):
        key = "kr_krw" if m == "KR" else "usd_total"
        raw = aux.get(key)
        if raw is not None:
            v = float(raw or 0)
            if v > 0:
                if _aux_total_looks_like_glitch(v, snap_t, m):
                    return snap_t
                return v
    if snap_t > 0:
        return snap_t
    m = _market_norm(market)
    if m == "KR":
        return float(state.get("circuit_aux_last_kr_krw", 0) or state.get("last_kr_cash_krw", 0) or 0)
    return float(state.get("circuit_aux_last_usd_total", 0) or state.get("last_us_cash_usd", 0) or 0)


def _cash_looks_like_total_as_cash(cash: float, total: float) -> bool:
    return cash > 0 and total > 0 and cash >= total * 0.95


def display_cash_from_state(
    state: dict,
    market: str,
    snap_part: dict | None = None,
) -> float:
    """예수 — ``last_kis_display_snapshot`` 만 (옛 키는 스냅샷 비었을 때만 읽기)."""
    m = _market_norm(market)
    part = snap_part if isinstance(snap_part, dict) else _kis_snap_bucket(state, m)
    snap_cash = float(part.get("cash", 0) or 0)
    snap_total = float(part.get("total", 0) or 0)
    no_holdings = (
        (m == "KR" and not held_kr_codes_from_ledger(state))
        or (m == "US" and not held_us_codes_from_ledger(state))
    )
    stale = _cash_looks_like_total_as_cash(snap_cash, snap_total)
    if snap_cash > 0 and not stale:
        return snap_cash
    if stale:
        # 보유 0이면 cash≈total 은 전액현금 — 정상. 레거시 last_*_cash 로 덮지 않음.
        if no_holdings:
            return snap_cash
        legacy = float(state.get(_LEGACY_CASH_KEYS.get(m, ""), 0) or 0)
        legacy_is_real_cash = legacy > 0 and legacy < snap_cash * 0.9
        if legacy_is_real_cash:
            return legacy
        return 0.0
    if not part:
        legacy = float(state.get(_LEGACY_CASH_KEYS.get(m, ""), 0) or 0)
        if legacy > 0:
            return legacy
    return snap_cash


def ledger_holdings_value_native(state: dict, market: str) -> float:
    """장부 보유평가. KR=원, US=USD. 코인은 0."""
    m = _market_norm(market)
    if m not in ("KR", "US"):
        return 0.0
    positions = state.get("positions")
    if not isinstance(positions, dict) or not positions:
        return 0.0
    tot = 0.0
    for code, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        ticker = normalize_ticker(str(code))
        is_kr = ticker.isdigit() and len(ticker) == 6
        if m == "KR" and not is_kr:
            continue
        if m == "US" and (is_kr or is_coin_ticker(ticker)):
            continue
        try:
            qty = float(pos.get("qty") or 0)
            px = float(pos.get("curr_p") or pos.get("buy_p") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0 and px > 0:
            tot += qty * px
    return tot


def _risk_equity_from_ledger_and_snap(
    total: float,
    snap_total: float,
    market: str,
) -> float:
    """ledger ``cash+stock`` vs ``snap_total`` — 5%+ 증발 시 매도 정산 지연으로 snap 신뢰."""
    m = _market_norm(market)
    t = float(total)
    st = float(snap_total)
    if st > 0 and t < st * 0.95:
        unit = "원" if m == "KR" else ""
        print(
            f"  📌 [risk {m}] 매도 정산 지연 방어 — "
            f"ledger {t:,.0f}{unit} < snap {st:,.0f}{unit}×0.95 → snap_total 사용"
        )
        return st
    if t > 0:
        return t
    if st > 0:
        return st
    return 0.0


def market_equity_for_risk(state: dict, market: str) -> float:
    """서킷·MDD 판정 전용 — 스냅샷 예수 + 장부 보유.

    ``cash+stock`` 이 ``snap_total`` 보다 5% 이상 낮으면 매도 대금 미정산으로 보고
    ``snap_total`` 을 반환한다 (Phase5 오발동 방지). 시간 유예·쿨다운은 쓰지 않는다.

    직전 루프 잔고 대비 ``+보유``만큼 점프하면 이중합산으로 보고 직전 루프값을 쓴다.
    """
    m = _market_norm(market)
    if m not in ("KR", "US"):
        return 0.0
    stock = ledger_holdings_value_native(state, m)
    part = _kis_snap_bucket(state, m)
    snap_total = float(part.get("total", 0) or 0)
    snap_cash = float(part.get("cash", 0) or 0)
    min_sold = 25_000.0 if m == "KR" else 25.0
    min_pt = 10_000.0 if m == "KR" else 50.0

    if stock > 0:
        cash = snap_cash if snap_cash > 0 else display_cash_from_state(state, m)
    else:
        cash = display_cash_from_state(state, m)
        if cash <= 0 and snap_cash > 0:
            cash = snap_cash
    total = float(cash) + float(stock)

    # 2차 방어: last_loop 대비 보유분만큼 상방 점프 = 예수에 보유 재가산
    try:
        last_map = state.get("phase5_last_loop_equity_by_market")
        prev_risk = float((last_map or {}).get(m, 0) or 0) if isinstance(last_map, dict) else 0.0
    except (TypeError, ValueError):
        prev_risk = 0.0
    if (
        prev_risk >= min_pt
        and stock >= min_sold
        and total > prev_risk * 1.12
        and abs(total - (prev_risk + stock)) <= max(min_sold, stock * 0.12)
    ):
        return float(prev_risk)

    # 장부 보유가 스냅에 아직 반영 안 됐을 때만 ledger 상향 신뢰
    if stock > 0 and snap_total > 0 and total > snap_total * 1.05:
        return float(total)
    return _risk_equity_from_ledger_and_snap(total, snap_total, m)


def equity_divergence_metrics(state: dict, market: str) -> tuple[float, float, float]:
    """(risk_total, snap_total, diff_pct). diff_pct = |risk−snap| / max(risk,snap) × 100."""
    m = _market_norm(market)
    min_pt = 10_000.0 if m == "KR" else 50.0
    risk = float(market_equity_for_risk(state, m))
    snap = float(kis_display_total(state, m))
    base = max(risk, snap)
    if base < min_pt:
        return risk, snap, 0.0
    return risk, snap, abs(risk - snap) / base * 100.0


def log_equity_divergence_if_needed(
    state: dict,
    markets: tuple[str, ...] = ("KR", "US"),
    *,
    warn_pct: float = EQUITY_DIVERGENCE_WARN_PCT,
) -> None:
    """risk vs display 스냅샷 총평 괴리가 크면 경고 (Phase 3 관측)."""
    for mk in markets:
        risk, snap, pct = equity_divergence_metrics(state, mk)
        if pct < warn_pct:
            continue
        unit = "원" if mk == "KR" else "$"
        suffix = "원" if mk == "KR" else ""
        print(
            f"  ⚠️ [equity_divergence·{mk}] risk {unit}{risk:,.0f}{suffix} "
            f"vs snap {unit}{snap:,.0f}{suffix} — diff {pct:.1f}% "
            f"(>{warn_pct:.0f}%면 라벨·스냅샷 점검)"
        )


def refresh_kis_display_snapshot_for_market(state: dict, market: str, path) -> bool:
    """Phase5 발동 직전 KIS 1회 실조회 → 표시 스냅샷 갱신 (캐시 무시)."""
    from pathlib import Path

    from execution.guard import save_state

    mk = _market_norm(market)
    if mk not in ("KR", "US"):
        return False
    p = Path(path)
    try:
        from execution import balance_read as bal_read

        try:
            bal_read.invalidate(mk)
        except Exception:
            pass
        if mk == "KR":
            bal = bal_read.kr_balance_raw(refresh=True)
            persist_kr_cash_from_balance(bal if isinstance(bal, dict) else {}, state)
        else:
            bal = bal_read.us_balance_raw(refresh=True)
            persist_us_cash_from_balance(bal if isinstance(bal, dict) else {}, state)
        save_state(p, state)
        return True
    except Exception as e:
        print(f"  ⚠️ [equity_divergence·{mk}] KIS 재조회 실패 — {type(e).__name__}: {e}")
        return False


def persist_display_cash_total(
    market: str,
    state: dict,
    cash: float,
    stock: float,
    *,
    force: bool = False,
    roi: Any = None,
) -> tuple[float, float]:
    """표시 전용: sanitize + ``last_kis_display_snapshot`` 저장.

    GUI·텔레·KIS 실조회 persist 경로만 사용. Phase5·5% MDD·매수 배정에는
    ``market_equity_for_risk`` 를 쓴다.
    """
    m = _market_norm(market)
    nc, nt = _sanitize_kis_cash_total_persist(m, state, float(cash), float(stock))
    write_kis_display_snapshot_part(state, m, cash=nc, total=nt, force=force, roi=roi)
    return nc, nt


def write_kis_display_snapshot_part(
    state: dict,
    market: str,
    *,
    cash: float,
    total: float,
    roi: Any = None,
    saved_at: str | None = None,
    force: bool = False,
    allow_total_crash: bool = False,
) -> None:
    """국·미 예수·총평 — ``last_kis_display_snapshot`` 에만 저장 (단일 저장소)."""
    m = _market_norm(market)
    if m not in ("KR", "US"):
        return
    snap = state.get("last_kis_display_snapshot")
    if not isinstance(snap, dict):
        snap = {}
    bucket_key = "kr" if m == "KR" else "us"
    prev = snap.get(bucket_key)
    entry: dict[str, Any] = dict(prev) if isinstance(prev, dict) else {}
    nc = float(cash)
    nt = float(total)
    if isinstance(prev, dict) and not allow_total_crash:
        pc = float(prev.get("cash", 0) or 0)
        pt = float(prev.get("total", 0) or 0)
        min_pt = 10_000.0 if m == "KR" else 50.0
        total_crash = pt >= min_pt and nt < pt * 0.90
        prev_stock = max(0.0, pt - pc)
        new_stock = max(0.0, nt - nc)
        stock_sold = max(0.0, prev_stock - new_stock)
        min_sold = 25_000.0 if m == "KR" else 25.0
        no_material_sell = stock_sold < min_sold
        cash_crash = pc >= min_pt and (nc <= 0 or nc < pc * 0.55)
        if total_crash and no_material_sell and (cash_crash or force):
            print(
                f"  📌 [snapshot {m}] 총평 급감 저장 거부 — "
                f"{'매매 후 ' if force else ''}"
                f"스냅샷 유지 (new total={nt:,.2f} / prev={pt:,.2f})"
            )
            return
        # 상방 급등 가드 — 매수·강제 새로고침 없이 총평 +5%↑ 거부
        if (
            not force
            and pt >= min_pt
            and nt > pt * 1.05
            and no_material_sell
        ):
            recent_spend = _recent_equity_buy_spend(state, m)
            stock_grew = (new_stock - prev_stock) >= min_sold and (
                prev_stock <= min_sold or new_stock > prev_stock * 1.08
            )
            if recent_spend < min_sold and not stock_grew:
                print(
                    f"  📌 [snapshot {m}] 총평 급등 저장 거부 — "
                    f"스냅샷 유지 (new total={nt:,.2f} / prev={pt:,.2f})"
                )
                return
    elif not force and isinstance(prev, dict):
        pc = float(prev.get("cash", 0) or 0)
        pt = float(prev.get("total", 0) or 0)
        min_pt = 10_000.0 if m == "KR" else 50.0
        total_crash = pt >= min_pt and nt < pt * 0.85
        cash_crash = pc >= min_pt and (nc <= 0 or nc < pc * 0.55)
        if total_crash and cash_crash:
            print(
                f"  📌 [snapshot {m}] 잔고 API 예수·총평 급감(일시/점검 추정) — "
                f"저장 스냅샷 유지 (new cash={nc}, total={nt} / prev cash={pc}, total={pt})"
            )
            return
    entry["cash"] = int(nc) if m == "KR" else float(nc)
    entry["total"] = int(nt) if m == "KR" else float(nt)
    entry.pop(_LEGACY_BUY_CASH_GUARD_KEY, None)
    if roi is not None:
        entry["roi"] = roi
    snap[bucket_key] = entry
    if saved_at:
        snap["saved_at"] = saved_at
    else:
        snap["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_kis_display_snapshot"] = snap


def _ledger_qty(pos: dict | None, fallback: float = 0.0) -> float:
    if not isinstance(pos, dict):
        return float(fallback)
    try:
        q = float(pos.get("qty", 0) or 0)
    except (TypeError, ValueError):
        q = 0.0
    return q if q > 0 else float(fallback)


def held_kr_codes_from_ledger(state: dict) -> list[str]:
    out: list[str] = []
    for t in (state.get("positions") or {}):
        c = str(t).strip()
        if c.isdigit() and len(c) == 6:
            out.append(normalize_ticker(c))
    return sorted(set(out))


def held_us_codes_from_ledger(state: dict) -> list[str]:
    out: list[str] = []
    for t in (state.get("positions") or {}):
        c = normalize_ticker(str(t))
        if c and not c.isdigit() and not is_coin_ticker(c):
            out.append(c)
    return sorted(set(out))


def _kr_row_from_position(code: str, pos: dict, curr_p: float) -> dict[str, Any]:
    qty = _ledger_qty(pos, 1.0)
    buy_p = float(pos.get("buy_p", 0) or 0)
    return {
        "pdno": code,
        "hldg_qty": str(int(qty)),
        "ccld_qty_smtl1": str(int(qty)),
        "pchs_avg_prc": str(buy_p),
        "pchs_avg_pric": str(buy_p),
        "prpr": str(int(curr_p)),
        "stck_prpr": str(int(curr_p)),
    }


def synthetic_kr_balance_dict(
    state: dict,
    *,
    resolve_kr_price: Callable[[str, dict, float], float],
) -> dict[str, Any]:
    """KIS 잔고 응답 형태 — ``output1``/``output2`` (장부·시세 기반)."""
    positions = state.get("positions") or {}
    holdings_value = 0.0
    output1: list[dict] = []
    for code, pos in positions.items():
        if not str(code).isdigit():
            continue
        if not isinstance(pos, dict):
            continue
        buy_p = float(pos.get("buy_p", 0) or 0)
        if buy_p <= 0:
            continue
        c = normalize_ticker(str(code))
        curr_p = float(resolve_kr_price(c, pos, buy_p))
        qty = _ledger_qty(pos, 1.0)
        holdings_value += qty * curr_p
        output1.append(_kr_row_from_position(c, pos, curr_p))

    cash = display_cash_from_state(state, "KR")
    total = cash + holdings_value
    snap_total = kis_display_total(state, "KR")
    if holdings_value <= 0 and snap_total > cash * 1.02:
        cash = snap_total
        total = snap_total
    elif total <= 0 and snap_total > 0:
        total = snap_total
        if cash <= 0:
            cash = max(0.0, total - holdings_value)

    output2 = [
        {
            "prvs_rcdl_excc_amt": str(int(cash)),
            "tot_evlu_amt": str(int(total)),
        }
    ]
    return {"rt_cd": "0", "msg1": "ledger_valuation", "output1": output1, "output2": output2}


def synthetic_us_balance_dict(
    state: dict,
    *,
    resolve_us_price: Callable[[str, dict, float], float],
) -> dict[str, Any]:
    positions = state.get("positions") or {}
    holdings_value = 0.0
    output1: list[dict] = []
    for raw, pos in positions.items():
        t = normalize_ticker(str(raw))
        if not t or t.isdigit() or is_coin_ticker(t):
            continue
        if not isinstance(pos, dict):
            continue
        buy_p = float(pos.get("buy_p", 0) or 0)
        if buy_p <= 0:
            continue
        curr_p = float(resolve_us_price(t, pos, buy_p))
        qty = _ledger_qty(pos, 1.0)
        holdings_value += qty * curr_p
        output1.append(
            {
                "ovrs_pdno": t,
                "ovrs_cblc_qty": str(qty),
                "ovrs_avg_unpr": str(buy_p),
                "ovrs_now_prc2": str(curr_p),
            }
        )

    cash = display_cash_from_state(state, "US")
    total = cash + holdings_value
    snap_total = kis_display_total(state, "US")
    if holdings_value <= 0 and snap_total > cash * 1.02:
        cash = snap_total
        total = snap_total
    elif total <= 0 and snap_total > 0:
        total = snap_total
        if cash <= 0:
            cash = max(0.0, total - holdings_value)

    output2 = {"ovrs_stck_evlu_amt": str(holdings_value), "frcr_dncl_amt_2": str(cash)}
    return {"rt_cd": "0", "msg1": "ledger_valuation", "output1": output1, "output2": output2}


def _ensure_kis_snap_part(state: dict, market: str) -> dict:
    """``last_kis_display_snapshot`` 버킷을 보장하고 그 dict 를 반환."""
    m = _market_norm(market)
    snap = state.get("last_kis_display_snapshot")
    if not isinstance(snap, dict):
        snap = {}
        state["last_kis_display_snapshot"] = snap
    key = "kr" if m == "KR" else "us"
    part = snap.get(key)
    if not isinstance(part, dict):
        part = {}
        snap[key] = part
    return part


def _recent_equity_buy_spend(
    state: dict,
    market: str,
    *,
    max_age_sec: float = _RECENT_BUY_MAX_AGE_SEC,
) -> float:
    """최근 매수 포지션의 매입대금 합(국장=원, 미장=USD)."""
    m = _market_norm(market)
    positions = state.get("positions")
    if not isinstance(positions, dict) or not positions:
        return 0.0
    now = time.time()
    spent = 0.0
    for code, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        ticker = normalize_ticker(code)
        if not ticker or is_coin_ticker(ticker):
            continue
        is_kr = ticker.isdigit()
        if m == "KR" and not is_kr:
            continue
        if m == "US" and is_kr:
            continue
        try:
            bt = float(pos.get("buy_time") or 0)
        except (TypeError, ValueError):
            bt = 0.0
        if bt <= 0 or (now - bt) > max_age_sec:
            continue
        try:
            qty = float(pos.get("qty") or 0)
            buy_p = float(pos.get("buy_p") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0 and buy_p > 0:
            spent += qty * buy_p
    return float(spent)


def _stock_matches_recent_buy(
    stock_v: float,
    recent_spend: float,
    prev_stock: float,
    *,
    min_sold: float,
    stock_grew: bool,
) -> bool:
    if recent_spend < min_sold or stock_v <= 0:
        return False
    if abs(stock_v - recent_spend) <= max(min_sold, recent_spend * 0.15):
        return True
    if prev_stock <= min_sold and stock_v >= recent_spend * 0.85:
        return True
    return bool(stock_grew)


def _is_buy_settlement_crash(
    nc: float,
    prev_cash: float,
    prev_total: float,
    stock_v: float,
    recent_spend: float,
    *,
    min_sold: float,
) -> bool:
    """매수 직후 예수 실종·총평=보유만 — Rule 3 급감 거부 대신 Rule 2."""
    if recent_spend < min_sold or stock_v <= 0 or prev_total < min_sold:
        return False
    if nc > 0 and prev_cash > 0 and nc >= prev_cash * 0.15:
        return False
    live = nc + stock_v
    if live >= prev_total * 0.85:
        return False
    return abs(stock_v - recent_spend) <= max(min_sold, recent_spend * 0.15)


def _sanitize_kis_cash_total_persist(
    market: str,
    state: dict,
    cash: float,
    stock: float,
) -> tuple[float, float]:
    """매수·매도 직후 KIS API 지연/이중합산을 **표시 저장 전**에 보정.

    Phase5·MDD·매수 배정에는 쓰지 말 것 — ``market_equity_for_risk`` 사용.

    규칙 (Phase 2 + Rule1b):
      1. **이중합산** — 총평 부풀 + 예수 정체 → ``prev_total`` 기준 역산
      1b. **예수=현금+보유 / 예수=직전총평** — 보유 정체·상방 점프 (마감 후 재발 방지)
      2. **예수 미차감** — 최근 매수 + 예수 안 줄음 / 예수 실종 → 매입대금 역산
      3. **급감 거부** — 매도 없이 총평 10%↓ → 직전 총평 유지 (매수 직후 고정)
    """
    m = _market_norm(market)
    part = _kis_snap_bucket(state, m)
    prev_cash = float(part.get("cash", 0) or 0)
    prev_total = float(part.get("total", 0) or 0)
    min_pt = 10_000.0 if m == "KR" else 50.0
    min_sold = 25_000.0 if m == "KR" else 25.0
    nc = float(cash)
    stock_v = float(stock)
    total = nc + stock_v
    if prev_total < min_pt:
        return nc, total

    prev_stock = max(0.0, prev_total - prev_cash)
    stock_delta = stock_v - prev_stock
    stock_grew = stock_delta >= min_sold and (
        prev_stock <= min_sold or stock_v > prev_stock * 1.08
    )
    stock_sold = max(0.0, prev_stock - stock_v)
    no_material_sell = stock_sold < min_sold
    cash_stale = nc >= prev_cash * 0.85 if prev_cash > 0 else nc > 0
    total_inflated = total > prev_total * 1.03
    recent_spend = _recent_equity_buy_spend(state, m)
    unit = "원" if m == "KR" else ""
    stock_stable = abs(stock_v - prev_stock) <= max(min_sold, prev_stock * 0.08)
    tol = max(min_sold, stock_v * 0.05, prev_cash * 0.02 if prev_cash > 0 else 0.0)

    # Rule 3: 급감 거부 (매수 정산형 급감은 Rule 2로)
    if (
        no_material_sell
        and prev_total >= min_pt
        and total < prev_total * 0.90
        and not _is_buy_settlement_crash(
            nc, prev_cash, prev_total, stock_v, recent_spend, min_sold=min_sold
        )
    ):
        nc = max(0.0, prev_total - stock_v)
        total = prev_total
        print(
            f"  📌 [snapshot {m}] persist Rule3 급감 거부 — "
            f"총평 {prev_total:,.2f} 유지 · 예수 {nc:,.2f}{unit}"
        )
        return nc, total

    # Rule 1: 이중합산 (보유↑ + 예수 정체 + 총평 부풀 / 저예수 스냅 재오염)
    low_cash_rebound = (
        prev_cash > 0
        and prev_cash < prev_total * 0.25
        and nc > max(prev_cash * 2.0, prev_cash + min_sold)
        and stock_v >= prev_stock * 0.92
    )
    if total_inflated and cash_stale and (stock_grew or low_cash_rebound):
        if low_cash_rebound and abs(stock_v - prev_stock) <= max(min_sold, prev_stock * 0.08):
            nc = prev_cash
        else:
            nc = max(0.0, prev_total - stock_v)
        total = nc + stock_v
        print(
            f"  📌 [snapshot {m}] persist Rule1 이중합산 — "
            f"예수 {nc:,.2f}{unit} · 총평 {total:,.2f}"
        )
        return nc, total

    # Rule 1b: 보유 정체인데 예수만 보유분·직전총평만큼 점프 (2026-09-11 KR 마감 후)
    if (
        total_inflated
        and stock_stable
        and stock_v >= min_sold
        and prev_cash >= min_pt
        and recent_spend < min_sold
    ):
        cash_plus_holdings = abs(nc - (prev_cash + stock_v)) <= tol
        cash_eq_prev_total = abs(nc - prev_total) <= tol
        if cash_plus_holdings:
            nc = prev_cash
            total = nc + stock_v
            print(
                f"  📌 [snapshot {m}] persist Rule1b 예수=현금+보유 — "
                f"예수 {nc:,.2f}{unit} · 총평 {total:,.2f}"
            )
            return nc, total
        if cash_eq_prev_total:
            nc = max(0.0, prev_total - stock_v)
            total = nc + stock_v
            print(
                f"  📌 [snapshot {m}] persist Rule1b 예수=직전총평 — "
                f"예수 {nc:,.2f}{unit} · 총평 {total:,.2f}"
            )
            return nc, total

    # Rule 2: 예수 미차감 / 매수 후 예수 실종
    if recent_spend >= min_sold and stock_v > 0:
        cash_matches_snap = prev_cash > 0 and abs(nc - prev_cash) <= max(
            min_sold, prev_cash * 0.05, recent_spend * 0.08
        )
        snap_total_settled = abs((nc + stock_v) - prev_total) <= max(
            min_sold, prev_total * 0.02
        )
        already_deducted = cash_matches_snap and snap_total_settled
        stock_matches = _stock_matches_recent_buy(
            stock_v, recent_spend, prev_stock, min_sold=min_sold, stock_grew=stock_grew
        )
        stock_stable = abs(stock_v - prev_stock) <= max(min_sold, prev_stock * 0.08)
        snap_total_inflated = prev_total > (prev_cash + stock_v) * 1.001

        if (nc <= 0 or nc < prev_cash * 0.15) and total < prev_total * 0.85 and stock_matches:
            recon = max(0.0, prev_cash - recent_spend)
            if recon + stock_v < prev_total * 0.50:
                recon = max(0.0, prev_total - stock_v)
            total = recon + stock_v
            print(
                f"  📌 [snapshot {m}] persist Rule2 예수 실종 — "
                f"매입≈{recent_spend:,.2f} → 예수 {recon:,.2f}{unit} · 총평 {total:,.2f}"
            )
            return recon, total

        low_cash_undeducted = (
            prev_cash > 0
            and prev_cash < prev_total * 0.25
            and nc > recent_spend
            and (nc - recent_spend) <= max(min_sold, recent_spend * 0.15)
            and stock_stable
        )
        if low_cash_undeducted:
            adj_cash = max(0.0, nc - recent_spend)
            adj_total = adj_cash + stock_v
            print(
                f"  📌 [snapshot {m}] persist Rule2 예수 미차감 — "
                f"매입≈{recent_spend:,.2f} → 예수 {adj_cash:,.2f}{unit} · "
                f"총평 {adj_total:,.2f}"
            )
            return adj_cash, adj_total

        if (not already_deducted or snap_total_inflated) and nc >= prev_cash * 0.85 and (
            stock_matches or stock_stable
        ):
            apply_rule2 = False
            adj_cash = nc
            if prev_cash >= stock_v * 1.35 and abs(stock_v - recent_spend) <= max(
                min_sold, recent_spend * 0.25
            ):
                adj_cash = max(0.0, prev_cash - recent_spend)
                apply_rule2 = True
            elif snap_total_inflated and nc >= prev_cash * 0.85:
                adj_cash = max(0.0, nc - recent_spend)
                apply_rule2 = True
            elif total > prev_total * 1.03 or (nc + stock_v) >= prev_cash + stock_v * 0.9:
                base = prev_cash if prev_cash > min_sold else nc
                adj_cash = max(0.0, base - recent_spend) if base > recent_spend else max(
                    0.0, nc - recent_spend
                )
                apply_rule2 = True
            if apply_rule2:
                adj_total = adj_cash + stock_v
                if adj_total < total * 0.97 or total > prev_total * 1.03 or adj_cash < nc * 0.85:
                    print(
                        f"  📌 [snapshot {m}] persist Rule2 예수 미차감 — "
                        f"매입≈{recent_spend:,.2f} → 예수 {adj_cash:,.2f}{unit} · "
                        f"총평 {adj_total:,.2f}"
                    )
                    return adj_cash, adj_total

    # 매도 정산 지연 (Rule 1~3 해당 없을 때)
    if stock_sold >= min_sold and total < prev_total * 0.97 and nc < prev_cash + stock_sold * 0.5:
        if m == "US":
            from run_bot import _reconcile_us_equity_after_sell

            nc, total = _reconcile_us_equity_after_sell(
                prev_cash=prev_cash,
                prev_total=prev_total,
                prev_stock=prev_stock,
                cash=nc,
                stock=stock_v,
            )
        else:
            nc = prev_cash + stock_sold
            total = nc + stock_v
            print(
                f"  📌 [snapshot {m}] persist 매도 정산 지연 추정 — "
                f"예수 {nc:,.0f}{unit} · 총평 {total:,.0f}{unit}"
            )
    elif total < prev_total * 0.97:
        stock_stable = abs(stock_v - prev_stock) < max(min_sold, prev_stock * 0.08)
        cash_dropped = prev_cash > 0 and nc < prev_cash * 0.85
        if stock_stable and cash_dropped:
            if m == "US":
                from run_bot import _reconcile_us_equity_after_sell

                nc, total = _reconcile_us_equity_after_sell(
                    prev_cash=prev_cash,
                    prev_total=prev_total,
                    prev_stock=prev_stock,
                    cash=nc,
                    stock=stock_v,
                )
            else:
                nc = prev_total - stock_v
                total = prev_total
                print(
                    f"  📌 [snapshot {m}] persist 정산 지연 지속 추정 — "
                    f"예수 {nc:,.0f}{unit} · 총평 {total:,.0f}{unit}"
                )
    return nc, total


def coalesce_ledger_kis_labels(
    market: str,
    state: dict,
    kis_snap_part: dict | None,
    holdings_current: float,
    *,
    cash_guess: float = 0.0,
    total_guess: float = 0.0,
) -> tuple[float, float]:
    """장부+시세 GUI 라벨 — 예수에 총평이 섞여 있으면 ``cash+보유`` 이중 합산을 막는다.

  * 예수: ``display_cash_from_state`` (``last_kis_display_snapshot`` 단일 소스)
  * 총평: 정리된 예수 + 장부 보유 평가(표시 시세)
    """
    part = kis_snap_part if isinstance(kis_snap_part, dict) else {}
    snap_total = float(part.get("total", 0) or 0)
    snap_cash_raw = float(part.get("cash", 0) or 0)
    hc = float(holdings_current or 0.0)
    m = _market_norm(market)
    cash = display_cash_from_state(state, m, part)
    if cash <= 0:
        cash = float(cash_guess or 0.0)

    ref_total = snap_total if snap_total > 0 else float(total_guess or 0.0)
    min_sold = 25_000.0 if m == "KR" else 25.0
    if hc > 0 and cash <= 0 and (ref_total <= 0 or ref_total <= hc * 1.05):
        spend = _recent_equity_buy_spend(state, m)
        legacy = float(state.get(_LEGACY_CASH_KEYS.get(m, ""), 0) or 0)
        if spend >= min_sold and legacy > spend:
            cash = legacy - spend
        elif legacy > hc * 1.05:
            cash = max(0.0, legacy - hc)
        if cash > 0:
            if ref_total <= hc * 1.05:
                ref_total = float(cash) + hc
            print(
                f"  📌 [coalesce {m}] 예수 0·총평≈보유 복구 — "
                f"예수 {cash:,.2f}{'원' if m == 'KR' else ''}"
            )
    if hc <= 0 and ref_total > max(cash, 0.0) * 1.02:
        cash = ref_total
        total = ref_total
    elif hc > 0 and ref_total > 0:
        prev_implied_stock = max(0.0, snap_total - snap_cash_raw) if snap_total > 0 else 0.0
        stock_sold = max(0.0, prev_implied_stock - hc)
        live_total = float(cash) + hc
        if (
            prev_implied_stock > 0
            and hc > prev_implied_stock * 1.08
            and cash >= snap_cash_raw * 0.85
        ):
            cash = max(0.0, ref_total - hc)
        elif (
            stock_sold >= min_sold
            and ref_total > live_total * 1.02
            and cash < snap_cash_raw + stock_sold * 0.5
        ):
            cash = max(0.0, snap_cash_raw + stock_sold)
        elif cash >= ref_total * 0.95:
            cash = max(0.0, ref_total - hc)
        elif cash + hc > ref_total * 1.05:
            cash = max(0.0, ref_total - hc)

    total = float(cash) + hc
    if total <= 0 and ref_total > 0:
        total = ref_total
        if hc > 0 and cash <= 0:
            cash = max(0.0, ref_total - hc)

    if m == "KR":
        return float(int(round(cash))), float(int(round(total)))
    return float(cash), float(total)


def persist_kr_cash_from_balance(bal: dict, state: dict) -> None:
    """KIS 국장 잔고 조회 성공 시 ``last_kis_display_snapshot.kr`` 갱신."""
    if not isinstance(bal, dict):
        return
    try:
        from api.kis_parsers import kis_response_rate_limited, parse_kr_cash_total
        from run_bot import _to_float

        if kis_response_rate_limited(bal):
            return
        cash, total_parsed = parse_kr_cash_total(bal.get("output2", []), _to_float)
        from run_bot import _calc_kr_holdings_metrics

        stock = float(_calc_kr_holdings_metrics(bal).get("current", 0.0) or 0.0)
        if stock <= 0 and total_parsed > cash:
            stock = max(0.0, float(total_parsed) - float(cash))
        cash, total = persist_display_cash_total("KR", state, float(cash), stock, force=True)
    except Exception:
        pass


def persist_us_cash_from_balance(bal: dict, state: dict) -> None:
    """KIS 미장 잔고 조회 성공 시 ``last_kis_display_snapshot.us`` 갱신."""
    if not isinstance(bal, dict):
        return
    try:
        from api.kis_parsers import (
            kis_response_rate_limited,
            parse_us_cash_fallback,
        )
        from run_bot import _to_float

        if kis_response_rate_limited(bal):
            return
        from run_bot import (
            _compute_us_stock_value_from_output,
            _recover_us_cash_from_output2_if_needed,
            _resolve_us_display_cash,
            get_us_cash_real,
            kis_api,
        )

        out2 = bal.get("output2", {})
        cash = _resolve_us_display_cash(kis_api.broker_us, out2, refresh=False)
        if cash <= 0:
            cash = float(get_us_cash_real(kis_api.broker_us) or 0.0)
            cash = float(_recover_us_cash_from_output2_if_needed(cash, out2))
        stock = float(_compute_us_stock_value_from_output(bal, out2))
        cash, total = persist_display_cash_total("US", state, cash, stock, force=True)
    except Exception:
        pass


def update_circuit_aux_from_ledger(
    state: dict,
    *,
    resolve_kr_price: Callable[[str, dict, float], float],
    resolve_us_price: Callable[[str, dict, float], float],
    estimate_usdkrw: Callable[[], float],
    coin_equity_krw: float | None = None,
) -> dict[str, Any]:
    """Phase5·표시용 ``circuit_aux_last_*`` — KIS 없이 장부+시세로 갱신.

    매수 후 예수 미반영(스냅샷 예수 높음+장부 보유 증가)이면 ``coalesce`` 로 역산한다.
    """
    kr_bal = synthetic_kr_balance_dict(state, resolve_kr_price=resolve_kr_price)
    us_bal = synthetic_us_balance_dict(state, resolve_us_price=resolve_us_price)

    try:
        from run_bot import _calc_kr_holdings_metrics, _calc_us_holdings_metrics

        kr_h = float(_calc_kr_holdings_metrics(kr_bal).get("current", 0.0) or 0.0)
        us_h = float(_calc_us_holdings_metrics(us_bal).get("current", 0.0) or 0.0)
    except Exception:
        kr_h = 0.0
        us_h = 0.0

    snap = state.get("last_kis_display_snapshot") if isinstance(state.get("last_kis_display_snapshot"), dict) else {}
    kr_part = snap.get("kr") if isinstance(snap.get("kr"), dict) else {}
    us_part = snap.get("us") if isinstance(snap.get("us"), dict) else {}
    kr_cash, kr_total = coalesce_ledger_kis_labels(
        "KR", state, kr_part, kr_h, cash_guess=float(kr_part.get("cash", 0) or 0),
        total_guess=float(kr_part.get("total", 0) or 0),
    )
    us_cash, us_total = coalesce_ledger_kis_labels(
        "US", state, us_part, us_h, cash_guess=float(us_part.get("cash", 0) or 0),
        total_guess=float(us_part.get("total", 0) or 0),
    )
    # 서킷·Phase5 보조: sanitize/coalesce 총평 대신 장부+스냅샷 예수
    kr_risk = market_equity_for_risk(state, "KR")
    us_risk = market_equity_for_risk(state, "US")
    if kr_risk > 0:
        kr_total = kr_risk
    if us_risk > 0:
        us_total = us_risk
    snap_kr_t = float(kr_part.get("total", 0) or 0)
    snap_us_t = float(us_part.get("total", 0) or 0)
    if _aux_total_looks_like_glitch(kr_total, snap_kr_t, "KR"):
        kr_total = snap_kr_t
        kr_cash = float(kr_part.get("cash", 0) or kr_cash)
    if _aux_total_looks_like_glitch(us_total, snap_us_t, "US"):
        us_total = snap_us_t
        us_cash = float(us_part.get("cash", 0) or us_cash)

    coin_k = (
        float(coin_equity_krw)
        if coin_equity_krw is not None
        else float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    )
    # ledger-only 루프: native 미제공 시 기존 원화만 유지(바이낸스는 다음 실조회에서 갱신)
    from api import coin_broker as _cb

    if coin_equity_krw is not None:
        if _cb.coin_equity_quote_unit() == "USDT":
            rate = float(_cb.get_krw_per_usdt() or 0) or 1.0
            _cb.persist_circuit_aux_coin(state, float(coin_k) / rate, krw_per_usdt=rate)
        else:
            _cb.persist_circuit_aux_coin(state, float(coin_k))
    else:
        state["circuit_aux_last_coin_krw"] = float(coin_k)
    state["circuit_aux_last_kr_krw"] = float(kr_total)
    state["circuit_aux_last_usd_total"] = float(us_total)

    rate = float(estimate_usdkrw())
    return {
        "kr_ok": True,
        "us_ok": True,
        "coin_ok": coin_k > 0 or bool(state.get("circuit_aux_last_coin_krw")),
        "weekend_kis_skip": False,
        "ledger_only": True,
        "totals": {
            "kr_krw": float(kr_total),
            "usd_total": float(us_total),
            "coin_krw": float(state.get("circuit_aux_last_coin_krw", 0) or coin_k),
            "coin_native": float(_cb.circuit_aux_coin_native(state) or 0),
            "total_krw_est": float(kr_total)
            + float(state.get("circuit_aux_last_coin_krw", 0) or coin_k)
            + float(us_total) * rate,
        },
    }
