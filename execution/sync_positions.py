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

    **휴장일 (2026-05):** ``run_bot.is_market_open("KR")`` / ``"US"`` 가 False 인 시장은
    KIS 시드를 **아예 모으지 않고**, 해당 시장 종목에 대해 **평단 보정·유령 삭제·자동복구**를 하지 않는다.
    (공휴일·주말 장외에 브로커 응답이 들쭉날쭉할 때 장부가 날아가 보이는 것을 막기 위함.) 코인은 24시간이므로 기존대로.

    **유령 판단 보강 (2026-05):** ``get_held_stocks_*`` 가 돌려준 ``held_*`` 리스트만으로 지우면,
    KIS 응답에서 **수량 필드 조합이 한쪽만 채워지는 행**(휴장·점검 직후 등) 때문에 ``held`` 가 비고
    ``live_seeds`` 에만 종목이 남는 순간이 생길 수 있다. 그때 장부 전량이 유령 처리된 뒤 다음 틱에
    **자동복구**로 보이는 사고를 막기 위해, 유령 여부는 ``held_*`` 와 ``live_seeds`` 키의 **합집합**
    기준으로 본다. 국장 시드 수량은 ``account_read_facade`` 와 동일하게 ``hldg_qty`` / ``ccld_qty_smtl1`` 를 OR로 본다.

주의
    API가 전부 성공해야 ``sync_all_positions`` 가 돌아간다. 하나라도 ``None`` 이면
    ``run_bot`` 쪽에서 동기화 전체를 스킵해 **빈 잔고로 유령만 싹 지우는 사고**를 막는다.
"""
import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from api import coin_broker, coin_config
from api.kis_api import get_balance_with_retry, get_us_positions_with_retry
from execution.guard import save_state
from strategy.rules import get_ohlcv_yfinance
from utils.helpers import (
    ensure_dict,
    is_coin_ticker,
    kis_equities_weekend_suppress_window_kst,
    normalize_ticker,
    get_kr_company_name,
    get_us_company_name,
)

_BOT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = _BOT_ROOT / "bot_state.json"
DEFAULT_TRADE_HISTORY_PATH = _BOT_ROOT / "trade_history.json"


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


def _last_buy_timestamp_from_trade_history(
    ticker: str,
    market: str,
    *,
    history_path: Path | None = None,
) -> str | None:
    """
    ``trade_history.json`` 에서 해당 티커의 **가장 최근 BUY** ``timestamp`` 를 찾는다.
    자동복구 시 ``buy_date`` 추정에 사용 (없으면 None).
    """
    path = history_path if history_path is not None else DEFAULT_TRADE_HISTORY_PATH
    if not path.is_file():
        return None
    key = normalize_ticker(ticker)
    m = (market or "").strip().upper()
    try:
        raw = path.read_text(encoding="utf-8")
        rows = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(rows, list):
        return None
    last_ts: str | None = None
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if str(row.get("side", "")).upper() != "BUY":
            continue
        if str(row.get("market", "")).strip().upper() != m:
            continue
        t = normalize_ticker(str(row.get("ticker", "") or ""))
        if t != key:
            continue
        ts = row.get("timestamp")
        if isinstance(ts, str) and ts.strip():
            last_ts = ts.strip()
            break
    return last_ts


def _get_live_position_seeds(*, skip_kr: bool = False, skip_us: bool = False):
    """실계좌 보유종목의 진입 기준가(평단)·수량 시드 수집
    - 국장/미장: API 응답의 평균단가·보유수량
    - 코인: avg_buy_price·잔고 수량
    값 형식: ``ticker -> {"avg_p": float, "qty": float}`` (주말 GUI·직전 수량 표시용)

    ``skip_kr`` / ``skip_us`` True 이면 해당 시장 KIS 블록을 생략 (휴장일 장부 비갱신).
    """
    seeds = {}

    # ===== 🇰🇷 국장 (KIS 점검 구간이면 API 미호출) =====
    if (not skip_kr) and (not kis_equities_weekend_suppress_window_kst()):
        try:
            kr_bal = ensure_dict(get_balance_with_retry())
            output1 = kr_bal.get('output1', []) if isinstance(kr_bal.get('output1'), list) else []
            for stock in output1:
                code = normalize_ticker(stock.get('pdno', ''))
                if not code:
                    continue
                # account_read_facade.get_held_stocks_kr 와 동일: 두 수량 필드를 각각 본 뒤 OR
                hldg_qty = _to_float(stock.get("hldg_qty", 0))
                ccld_qty = _to_float(stock.get("ccld_qty_smtl1", 0))
                if hldg_qty <= 0.0001 and ccld_qty <= 0.0001:
                    continue
                qty = float(hldg_qty if hldg_qty > 0.0001 else ccld_qty)
                avg_p = _to_float(stock.get('pchs_avg_prc', stock.get('pchs_avg_pric', 0)))
                if avg_p <= 0:
                    avg_p = _to_float(stock.get('prpr', stock.get('stck_prpr', 0)))
                if avg_p > 0:
                    seeds[code] = {"avg_p": float(avg_p), "qty": float(qty)}
        except Exception:
            pass

    # ===== 🇺🇸 미장 (KIS 점검 구간이면 API 미호출) =====
    if (not skip_us) and (not kis_equities_weekend_suppress_window_kst()):
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
        balances = coin_broker.get_balances() or []
        if isinstance(balances, list):
            for b in balances:
                currency = b.get('currency')
                if currency in ['KRW', 'VTHO']:
                    continue
                if coin_config.is_binance() and str(currency).upper() == 'USDT':
                    continue
                qty = _to_float(b.get('balance', 0))
                if qty <= 0.00000001 or not coin_broker.should_include_coin_balance_row(b):
                    continue
                ticker = coin_broker.held_ticker_row(b)
                if not ticker:
                    continue
                avg_p = _to_float(b.get('avg_buy_price', 0))
                if avg_p <= 0:
                    cp = coin_broker.get_current_price(ticker)
                    avg_p = _to_float(cp, 0)
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

    import run_bot as _rb

    kr_open = bool(_rb.is_market_open("KR"))
    us_open = bool(_rb.is_market_open("US"))
    skip_kr_sync = not kr_open
    skip_us_sync = not us_open
    if skip_kr_sync or skip_us_sync:
        parts = []
        if skip_kr_sync:
            parts.append("국장")
        if skip_us_sync:
            parts.append("미장")
        print(
            f"  💤 [휴장] {'·'.join(parts)} 휴장 — KIS 시드·평단보정·유령삭제·주식 자동복구 생략 "
            f"(장부 유지). 코인은 동기화 계속."
        )

    # -----------------------------------------------------------------
    # 1) 자동 복구 및 평단가 동기화: 실보유 중인 종목의 정보 최신화
    # -----------------------------------------------------------------
    live_seeds = _get_live_position_seeds(skip_kr=skip_kr_sync, skip_us=skip_us_sync)

    # held_* 만으로 유령 삭제 시, KIS 필드 불일치로 held 가 비고 seeds 에만 남는 순간
    # 장부가 통째로 지워진 뒤 다음 사이클에 '자동복구'로 보이는 것을 방지한다.
    held_kr_set = set(held_kr or []) | {k for k in live_seeds if str(k).isdigit()}
    held_us_set = set(held_us or []) | {
        k for k in live_seeds if (not str(k).isdigit()) and (not is_coin_ticker(str(k)))
    }
    held_coins_set = set(held_coins or []) | {k for k in live_seeds if is_coin_ticker(str(k))}
    if len(held_kr_set) > len(set(held_kr or [])) or len(held_us_set) > len(set(held_us or [])) or len(
        held_coins_set
    ) > len(set(held_coins or [])):
        print(
            "  📌 [장부 동기화] 유령 판단용 보유키 = API held 와 live_seeds 의 합집합 "
            "(held만 비고 seeds에만 종목이 있을 때 장부 일괄 유령 처리 방지)"
        )

    for ticker, seed in live_seeds.items():
        real_avg_p = float(seed["avg_p"])
        live_qty = float(seed["qty"])
        if ticker not in current_positions:
            # 신규 복구 로직: 코인과 주식 데이터 수집 분리
            if is_coin_ticker(ticker):
                ohlcv = coin_broker.fetch_ohlcv(ticker, "day", 30)
            else:
                # 주식(국장/미장)은 기존대로 yfinance 사용
                ohlcv = get_ohlcv_yfinance(ticker)
                
            atr = calculate_atr(ohlcv)
            sl_p = real_avg_p - (atr * 2.5) if atr > 0 else real_avg_p * 0.90
            tier = "자동복구(V5.0손절-매수가)" if atr > 0 else "자동복구(-10%손절)"

            mkt = "KR" if str(ticker).isdigit() else ("COIN" if is_coin_ticker(ticker) else "US")
            buy_from_hist = _last_buy_timestamp_from_trade_history(ticker, mkt)
            buy_date_val = buy_from_hist if buy_from_hist else datetime.now().isoformat()

            row = {
                "buy_p": float(real_avg_p),
                "sl_p": float(sl_p),
                "max_p": float(real_avg_p),
                "tier": tier,
                "buy_date": buy_date_val,
                "scale_out_done": False,
            }
            if live_qty > 0:
                row["qty"] = float(live_qty)
            current_positions[ticker] = row
            name = get_kr_company_name(ticker) if ticker.isdigit() else (ticker if is_coin_ticker(ticker) else get_us_company_name(ticker))
            bd_note = f"매수일=trade_history" if buy_from_hist else "매수일=복구시각(히스토리 없음)"
            print(f"  -> 🚨 [자동복구] {name}({ticker}) 장부 등록 완료 (평단={real_avg_p:,.2f}, {bd_note})")
            changes_made = True
            recovered_count += 1
        else:
            # 🔄 [핵심 수정] 이미 장부에 있는 경우, 실제 계좌 평단가와 일치시키기
            pos = current_positions[ticker]
            if str(ticker).isdigit() and skip_kr_sync:
                continue
            if (not str(ticker).isdigit()) and (not is_coin_ticker(str(ticker))) and skip_us_sync:
                continue
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
            if skip_kr_sync:
                continue
            if ticker not in held_kr_set:
                to_delete.append(ticker)
        elif is_coin_ticker(ticker):
            if ticker not in held_coins_set:
                to_delete.append(ticker)
        else:
            if skip_us_sync:
                continue
            if ticker not in held_us_set:
                to_delete.append(ticker)

    for t in to_delete:
        # 종목명 가져오기
        if t.isdigit():
            name = get_kr_company_name(t)
        elif is_coin_ticker(t):
            name = t
        else:
            name = get_us_company_name(t)
        print(f"  -> 🧹 [통합 장부정리] 계좌에 없는 {name}({t}) 발견! 메모장에서 삭제했습니다.")
        del state["positions"][t]
        changes_made = True

    if recovered_count > 0:
        print(
            "  ⚠️ [자동복구 해설] 실계좌 잔고에는 있는데 장부(positions)에 키가 없어서 **새 행을 추가**한 것입니다. "
            "전체 초기화·유령 일괄삭제와 다릅니다. `buy_date`는 가능하면 **trade_history.json 의 마지막 BUY timestamp** 를 쓰고, "
            "없을 때만 복구 시각을 씁니다. `bot_state.json` 경로·백업·수동 편집도 확인하세요."
        )

    if changes_made:
        save_state(path, state)
        print(f"  -> ✅ 장부 동기화 완료 (복구 {recovered_count} / 유령정리 {len(to_delete)})")
        return True
    return False
