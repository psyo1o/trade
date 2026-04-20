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
    2) TR 이상치 차단 (단일 TR > 당일 종가의 30%)
    3) 최소 거래일(기본 14일) 확보
    """
    try:
        if isinstance(df, list):
            wdf = pd.DataFrame(df)
        elif isinstance(df, pd.DataFrame):
            wdf = df.copy()
        else:
            print(f"⚠️ [ATR 검증 실패] {ticker}: 비정상 데이터 감지")
            return None

        col_map = {}
        for src, dst in (("high", "h"), ("low", "l"), ("close", "c"), ("High", "h"), ("Low", "l"), ("Close", "c")):
            if src in wdf.columns and dst not in wdf.columns:
                col_map[src] = dst
        if col_map:
            wdf = wdf.rename(columns=col_map)

        for col in ("h", "l", "c"):
            if col not in wdf.columns:
                print(f"⚠️ [ATR 검증 실패] {ticker}: 비정상 데이터 감지")
                return None

        wdf = wdf[["h", "l", "c"]].copy()
        wdf["h"] = pd.to_numeric(wdf["h"], errors="coerce")
        wdf["l"] = pd.to_numeric(wdf["l"], errors="coerce")
        wdf["c"] = pd.to_numeric(wdf["c"], errors="coerce")

        # 1차 방어: 결측치 / 0 종가 차단
        if wdf[["h", "l", "c"]].isna().any().any() or (wdf["c"] <= 0).any():
            print(f"⚠️ [ATR 검증 실패] {ticker}: 비정상 데이터 감지")
            return None

        # 3차 방어: 최소 거래일
        if len(wdf) < int(period):
            print(f"⚠️ [ATR 검증 실패] {ticker}: 비정상 데이터 감지")
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

        # 2차 방어: 비정상 진폭 차단
        bad_tr = wdf["tr"] > (wdf["c"] * 0.30)
        if bad_tr.any():
            print(f"⚠️ [ATR 검증 실패] {ticker}: 비정상 데이터 감지")
            return None

        atr = wdf["tr"].rolling(int(period), min_periods=int(period)).mean().iloc[-1]
        if pd.isna(atr) or not np.isfinite(float(atr)) or float(atr) <= 0:
            print(f"⚠️ [ATR 검증 실패] {ticker}: 비정상 데이터 감지")
            return None
        return float(atr)
    except Exception:
        print(f"⚠️ [ATR 검증 실패] {ticker}: 비정상 데이터 감지")
        return None
