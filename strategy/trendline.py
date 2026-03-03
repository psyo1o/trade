def trendline_from_lows(ohlcv, lows):
    """
    최근 스윙 저점 2개로 추세선 (단순 직선)
    return: (i1, p1, i2, p2) or None
    """
    if len(lows) < 2:
        return None
    i1, i2 = lows[-2], lows[-1]
    p1, p2 = ohlcv[i1]["l"], ohlcv[i2]["l"]
    if i2 == i1:
        return None
    return (i1, p1, i2, p2)

def price_on_trendline(line, idx):
    i1, p1, i2, p2 = line
    slope = (p2 - p1) / (i2 - i1)
    return p1 + slope * (idx - i1)

def is_above_trendline(ohlcv, line, idx=-1, tolerance=0.002):
    """
    tolerance: 0.2% 정도 아래까지는 허용(살짝 이탈 털기 대응)
    """
    if line is None:
        return False
    if idx < 0:
        idx = len(ohlcv) - 1
    tl = price_on_trendline(line, idx)
    close = ohlcv[idx]["c"]
    return close >= tl * (1 - tolerance)

def touched_trendline_recently(ohlcv, line, bars=10, tolerance=0.003):
    if line is None:
        return False
    n = len(ohlcv)
    start = max(0, n - bars)
    for i in range(start, n):
        tl = price_on_trendline(line, i)
        if ohlcv[i]["l"] <= tl * (1 + tolerance):
            return True
    return False
