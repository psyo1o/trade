# -*- coding: utf-8 -*-
"""
보조지표 유틸 모음.

V8.0 준비: ATR 계산 전 3중 무결성 검증을 수행하는 ``get_safe_atr`` 제공.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def get_safe_atr(ticker: str, df: Any, period: int = 14) -> float | None:
    """
    무결성 검증 기반 ATR 계산.

    검증(실패 시 즉시 None 반환 + 경고 로그):
    1) 결측치/0 종가 차단
    2) TR 이상치 차단 (주식 30% / 코인 60% 동적 임계)
    3) 최소 거래일(기본 14일) 확보
    """
    try:
        if isinstance(df, list):
            wdf = pd.DataFrame(df)
        elif isinstance(df, pd.DataFrame):
            wdf = df.copy()
        else:
            print(f"⚠️ [ATR 검증 실패] {ticker}: 입력 데이터 형식 오류")
            return None

        col_map = {}
        for src, dst in (("high", "h"), ("low", "l"), ("close", "c"), ("High", "h"), ("Low", "l"), ("Close", "c")):
            if src in wdf.columns and dst not in wdf.columns:
                col_map[src] = dst
        if col_map:
            wdf = wdf.rename(columns=col_map)

        for col in ("h", "l", "c"):
            if col not in wdf.columns:
                print(f"⚠️ [ATR 검증 실패] {ticker}: 필수 OHLC 컬럼 누락")
                return None

        wdf = wdf[["h", "l", "c"]].copy()
        wdf["h"] = pd.to_numeric(wdf["h"], errors="coerce")
        wdf["l"] = pd.to_numeric(wdf["l"], errors="coerce")
        wdf["c"] = pd.to_numeric(wdf["c"], errors="coerce")

        # 1차 방어: 결측치 / 0 종가 차단
        if wdf[["h", "l", "c"]].isna().any().any() or (wdf["c"] <= 0).any():
            print(f"⚠️ [ATR 검증 실패] {ticker}: 결측치 또는 0 종가 감지")
            return None

        # 3차 방어: 최소 거래일
        p = int(period)
        if len(wdf) < p:
            print(f"⚠️ [ATR 검증 실패] {ticker}: 데이터 부족 ({len(wdf)} < {p})")
            return None

        wdf["prev_c"] = wdf["c"].shift(1)
        wdf["tr"] = wdf.apply(
            lambda x: max(
                x["h"] - x["l"],
                abs(x["h"] - x["prev_c"]) if pd.notna(x["prev_c"]) else 0.0,
                abs(x["l"] - x["prev_c"]) if pd.notna(x["prev_c"]) else 0.0,
            ),
            axis=1,
        )

        # V8: 검증/계산은 최근 period 구간 기준으로만 수행
        calc = wdf.tail(p).copy()
        # 2차 방어: 비정상 진폭 차단 (최근 구간, 종목군별 동적 임계)
        tr_ratio_limit = 0.60 if str(ticker).startswith("KRW-") else 0.30
        bad_tr = calc["tr"] > (calc["c"] * tr_ratio_limit)
        if bad_tr.any():
            print(
                f"⚠️ [ATR 검증 실패] {ticker}: {tr_ratio_limit * 100:.0f}% 초과 비정상 진폭 감지"
            )
            return None

        atr = calc["tr"].mean()
        if pd.isna(atr) or not np.isfinite(float(atr)) or float(atr) <= 0:
            print(f"⚠️ [ATR 검증 실패] {ticker}: ATR 계산 결과 비정상")
            return None
        return float(atr)
    except Exception:
        print(f"⚠️ [ATR 검증 실패] {ticker}: 예외 발생")
        return None
