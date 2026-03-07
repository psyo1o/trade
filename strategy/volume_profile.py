def volume_profile_levels(ohlcv, bins=40):
    """
    간단 근사: 가격 범위를 bins로 나눠서, 각 bin에 거래량을 누적
    return: (poc, supports_sorted_desc)  # POC=최대 매물대 중심
    """
    if len(ohlcv) < 50:
        return None, []

    lows = [r["l"] for r in ohlcv]
    highs = [r["h"] for r in ohlcv]
    vls = [r["v"] for r in ohlcv]

    pmin, pmax = min(lows), max(highs)
    if pmax <= pmin:
        return None, []

    step = (pmax - pmin) / bins
    buckets = [0] * bins

    for r in ohlcv:
        # 대표가격: (H+L+C)/3
        p = (r["h"] + r["l"] + r["c"]) / 3
        b = int((p - pmin) / step)
        b = max(0, min(bins - 1, b))
        buckets[b] += r["v"]

    max_idx = max(range(bins), key=lambda i: buckets[i])
    poc = pmin + (max_idx + 0.5) * step

    # 상위 매물대 후보(상위 5개 정도)
    top = sorted(range(bins), key=lambda i: buckets[i], reverse=True)[:5]
    levels = [pmin + (i + 0.5) * step for i in top]
    return poc, sorted(levels)
