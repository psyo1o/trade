# -*- coding: utf-8 -*-
"""퀀트용 수학 유틸 — Hurst Exponent(R/S Analysis) 등."""
from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np


def calculate_hurst_exponent(prices: Sequence[float] | Iterable[float]) -> float:
    """종가 시계열의 Hurst Exponent (R/S Analysis).

    * H < 0.5 — 평균회귀·횡보(Random Walk) 성향
    * H ≈ 0.5 — 랜덤워크
    * H > 0.5 — 추세 지속(persistent) 성향

    데이터가 부족하거나 분산이 0이면 중립값 0.5 를 반환한다.
  """
    series = np.asarray(list(prices), dtype=float)
    series = series[np.isfinite(series) & (series > 0)]
    if series.size < 50:
        return 0.5

    log_returns = np.diff(np.log(series))
    m = int(log_returns.size)
    if m < 20:
        return 0.5

    min_chunk = max(10, m // 10)
    max_chunk = min(m // 2, 100)
    if max_chunk < min_chunk:
        return 0.5

    rs_values: list[float] = []
    lens: list[int] = []
    for chunk in range(min_chunk, max_chunk + 1):
        chunk_rs: list[float] = []
        for start in range(0, m - chunk + 1, chunk):
            seg = log_returns[start : start + chunk]
            if seg.size < 2:
                continue
            centered = seg - seg.mean()
            cumsum = np.cumsum(centered)
            r_val = float(cumsum.max() - cumsum.min())
            s_val = float(seg.std(ddof=1))
            if s_val > 0:
                chunk_rs.append(r_val / s_val)
        if chunk_rs:
            rs_values.append(float(np.mean(chunk_rs)))
            lens.append(chunk)

    if len(lens) < 2:
        return 0.5

    x = np.log(np.asarray(lens, dtype=float))
    y = np.log(np.asarray(rs_values, dtype=float))
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        return 0.5

    slope = float(np.polyfit(x, y, 1)[0])
    if not np.isfinite(slope):
        return 0.5
    return float(np.clip(slope, 0.0, 1.0))
