# -*- coding: utf-8 -*-
"""
실계좌 잔고 ↔ ``bot_state.json`` 의 ``positions`` 동기화.

``run_trading_bot`` (및 GUI의 수동 새로고침)이 매 사이클 맨 앞에서 호출한다.

동작 요약
    1. KIS·업비트 API로 **현재 보유·평균단가·수량 시드**를 모은다 (``_get_live_position_seeds``).
    2. 시드에만 있고 장부에 없으면 → OHLCV·ATR로 손절가를 잡고 ``자동복구(...)`` 티어로 **신규 등록**.
    3. 장부와 실평단이 어긋나면 ``buy_p`` (및 필요 시 ``max_p``) **보정**.
    4. 장부에만 있는 심볼(계좌에 없음)은 **유령으로 간주해 삭제** (단, 최근 ``buy_time`` 만
       있는 수동/봇 주문 직후 등은 짧은 유예 시간 동안 스킵 — 자동복구 티어는 예외).

주의
    API가 전부 성공해야 ``sync_all_positions`` 가 돌아간다. 하나라도 ``None`` 이면
    ``run_bot`` 쪽에서 동기화 전체를 스킵해 **빈 잔고로 유령만 싹 지우는 사고**를 막는다.
"""
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyupbit

from api import upbit_api
from api.kis_api import get_balance_with_retry, get_us_positions_with_retry
from execution.guard import save_state
from strategy.rules import get_ohlcv_yfinance
from utils.helpers import (
    coin_qty_counts_for_position,
    ensure_dict,
    kis_equities_weekend_suppress_window_kst,
    normalize_ticker,
    get_kr_company_name,
    get_us_company_name,
)

_BOT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = _BOT_ROOT / "bot_state.json"


def _to_float(v, default=0.0) -> float:
    try:
        if v is None:
            return float(default)
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        return float(v)
    except (ValueError, TypeError):
        return float(default)


def calculate_atr(ohlcv):
    """주어진 OHLCV 데이터로 ATR(14)을 계산합니다."""
    if not ohlcv or len(ohlcv) < 15:
        return 0
    df = pd.DataFrame(ohlcv)
    df['tr0'] = abs(df['h'] - df['l'])
    df['tr1'] = abs(df['h'] - df['c'].shift())
    df['tr2'] = abs(df['l'] - df['c'].shift())
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    return df['tr'].rolling(14).mean().iloc[-1]


def _get_live_position_seeds():
    """실계좌 보유종목의 진입 기준가(평단)·수량 시드 수집
    - 국장/미장: API 응답의 평균단가·보유수량
    - 코인: avg_buy_price·잔고 수량
    값 형식: ``ticker -> {"avg_p": float, "qty": float}`` (주말 GUI·직전 수량 표시용)
    """
    seeds = {}

    # ===== 🇰🇷 국장 (KIS 점검 구간이면 API 미호출) =====
    if not kis_equities_weekend_suppress_window_kst():
        try:
            kr_bal = ensure_dict(get_balance_with_retry())
            output1 = kr_bal.get('output1', []) if isinstance(kr_bal.get('output1'), list) else []
            for stock in output1:
                code = normalize_ticker(stock.get('pdno', ''))
                qty = _to_float(stock.get('hldg_qty', stock.get('ccld_qty_smtl1', 0)))
                if not code or qty <= 0:
                    continue
                avg_p = _to_float(stock.get('pchs_avg_prc', stock.get('pchs_avg_pric', 0)))
                if avg_p <= 0:
                    avg_p = _to_float(stock.get('prpr', stock.get('stck_prpr', 0)))
                if avg_p > 0:
                    seeds[code] = {"avg_p": float(avg_p), "qty": float(qty)}
        except Exception:
            pass

    # ===== 🇺🇸 미장 (KIS 점검 구간이면 API 미호출) =====
    if not kis_equities_weekend_suppress_window_kst():
        try:
            us_bal = ensure_dict(get_us_positions_with_retry())
            output1 = us_bal.get('output1', []) if isinstance(us_bal.get('output1'), list) else []
            for stock in output1:
                code = normalize_ticker(stock.get('ovrs_pdno', stock.get('pdno', '')))
                qty = _to_float(stock.get('ovrs_cblc_qty', stock.get('ccld_qty_smtl1', stock.get('hldg_qty', 0))))
                if not code or qty <= 0:
                    continue
                avg_p = _to_float(stock.get('ovrs_avg_unpr', stock.get('ovrs_avg_pric', stock.get('avg_unpr3', 0))))
                if avg_p <= 0:
                    avg_p = _to_float(stock.get('ovrs_now_prc2', stock.get('ovrs_nmix_prpr', stock.get('ovrs_now_pric1', 0))))
                if avg_p > 0:
                    seeds[code] = {"avg_p": float(avg_p), "qty": float(qty)}
        except Exception:
            pass

    # ===== 🪙 코인 =====
    try:
        balances = upbit_api.upbit.get_balances() or []
        if isinstance(balances, list):
            for b in balances:
                currency = b.get('currency')
                if currency in ['KRW', 'VTHO']:
                    continue
                qty = _to_float(b.get('balance', 0))
                if qty <= 0.00000001 or not coin_qty_counts_for_position(qty):
                    continue
                ticker = f"KRW-{currency}"
                avg_p = _to_float(b.get('avg_buy_price', 0))
                if avg_p <= 0:
                    avg_p = _to_float(pyupbit.get_current_price(ticker), 0)
                if avg_p > 0:
                    seeds[ticker] = {"avg_p": float(avg_p), "qty": float(qty)}
    except Exception:
        pass

    return seeds


def sync_all_positions(state, held_kr, held_us, held_coins, state_path=None):
    """국장/미장/코인 통합 장부 정리
    1) 실보유인데 장부에 없는 종목은 즉시 등록
    2) 장부에만 있고 실보유가 아닌 유령종목 삭제
    """
    path = state_path if state_path is not None else DEFAULT_STATE_PATH
    if kis_equities_weekend_suppress_window_kst():
        print("💤 [주말 점검] 증권사 API 통신을 건너뛰고 기존 장부를 유지합니다.")
    print(f"🔄 [장부 점검] 실제 잔고 (국장:{len(held_kr)} / 미장:{len(held_us)} / 코인:{len(held_coins)}) 대조 중...")
    if "positions" not in state:
        state["positions"] = {}

    current_positions = state["positions"]
    changes_made = False
    recovered_count = 0

    # -----------------------------------------------------------------
    # 1) 자동 복구 및 평단가 동기화: 실보유 중인 종목의 정보 최신화
    # -----------------------------------------------------------------
    live_seeds = _get_live_position_seeds()
    for ticker, seed in live_seeds.items():
        real_avg_p = float(seed["avg_p"])
        live_qty = float(seed["qty"])
        if ticker not in current_positions:
            # 신규 복구 로직: 코인과 주식 데이터 수집 분리
            if ticker.startswith("KRW-"):
                # 코인은 pyupbit 전용으로 데이터 수집
                df_coin = pyupbit.get_ohlcv(ticker, interval="day", count=30)
                if df_coin is not None and not df_coin.empty:
                    ohlcv = [{'o': row['open'], 'h': row['high'], 'l': row['low'], 'c': row['close'], 'v': row['volume']} for _, row in df_coin.iterrows()]
                else:
                    ohlcv = []
            else:
                # 주식(국장/미장)은 기존대로 yfinance 사용
                ohlcv = get_ohlcv_yfinance(ticker)
                
            atr = calculate_atr(ohlcv)
            sl_p = real_avg_p - (atr * 2.5) if atr > 0 else real_avg_p * 0.90
            tier = "자동복구(V5.0손절-매수가)" if atr > 0 else "자동복구(-10%손절)"

            row = {
                "buy_p": float(real_avg_p),
                "sl_p": float(sl_p),
                "max_p": float(real_avg_p),
                "tier": tier,
                "buy_date": datetime.now().isoformat() # 자동복구 시 buy_date 기록
            }
            if live_qty > 0:
                row["qty"] = float(live_qty)
            current_positions[ticker] = row
            name = get_kr_company_name(ticker) if ticker.isdigit() else (ticker if ticker.startswith("KRW-") else get_us_company_name(ticker))
            print(f"  -> 🚨 [자동복구] {name}({ticker}) 장부 등록 완료 (평단={real_avg_p:,.2f})")
            changes_made = True
            recovered_count += 1
        else:
            # 🔄 [핵심 수정] 이미 장부에 있는 경우, 실제 계좌 평단가와 일치시키기
            pos = current_positions[ticker]
            if abs(float(pos.get('buy_p', 0)) - float(real_avg_p)) > 0.0001:
                old_p = pos.get('buy_p')
                pos['buy_p'] = float(real_avg_p)
                # 만약 최고가가 매수가보다 낮아져 있다면 함께 보정
                if pos.get('max_p', 0) < real_avg_p:
                    pos['max_p'] = float(real_avg_p)
                
                print(f"  -> ⚙️ [평단 보정] {ticker}: 장부({old_p:,.2f}) ➔ 실계좌({real_avg_p:,.2f}) 일치화 완료")
                changes_made = True
            if live_qty > 0:
                pq = _to_float(pos.get("qty", 0), 0.0)
                if abs(pq - live_qty) > 1e-8:
                    pos["qty"] = float(live_qty)
                    changes_made = True

    # -----------------------------------------------------------------
    # 2) 유령 제거: 장부에만 있고 계좌에 없는 종목은 삭제
    # -----------------------------------------------------------------
    to_delete = []

    for ticker in list(current_positions.keys()):
        pos_info = current_positions.get(ticker, {}) if isinstance(current_positions, dict) else {}
        buy_time = _to_float(pos_info.get("buy_time", 0), 0.0) if isinstance(pos_info, dict) else 0.0
        tier = str(pos_info.get("tier", "")) if isinstance(pos_info, dict) else ""
        is_auto_registered = ("자동등록" in tier) or ("자동복구" in tier)
        if buy_time > 0 and (time.time() - buy_time) < 900 and not is_auto_registered:
            continue

        if ticker.isdigit():
            if ticker not in held_kr:
                to_delete.append(ticker)
        elif ticker.startswith("KRW-"):
            if ticker not in held_coins:
                to_delete.append(ticker)
        else:
            if ticker not in held_us:
                to_delete.append(ticker)

    for t in to_delete:
        # 종목명 가져오기
        if t.isdigit():
            name = get_kr_company_name(t)
        elif t.startswith("KRW-"):
            name = t
        else:
            name = get_us_company_name(t)
        print(f"  -> 🧹 [통합 장부정리] 계좌에 없는 {name}({t}) 발견! 메모장에서 삭제했습니다.")
        del state["positions"][t]
        changes_made = True

    if changes_made:
        save_state(path, state)
        print(f"  -> ✅ 장부 동기화 완료 (복구 {recovered_count} / 유령정리 {len(to_delete)})")
        return True
    return False
