def swing_points(ohlcv, left=2, right=2):
    """
    ohlcv: 시간 오름차순 리스트 권장
    return: highs, lows (각각 index 리스트)
    """
    highs, lows = [], []
    n = len(ohlcv)
    for i in range(left, n-right):
        hi = ohlcv[i]["h"]
        lo = ohlcv[i]["l"]
        if all(hi > ohlcv[j]["h"] for j in range(i-left, i)) and all(hi >= ohlcv[j]["h"] for j in range(i+1, i+right+1)):
            highs.append(i)
        if all(lo < ohlcv[j]["l"] for j in range(i-left, i)) and all(lo <= ohlcv[j]["l"] for j in range(i+1, i+right+1)):
            lows.append(i)
    return highs, lows

def hh_hl_trend(ohlcv, lows, highs, lookback=3):
    """
    최근 스윙 저점/고점이 상승(고고저)인지 확인
    """
    if len(lows) < lookback or len(highs) < lookback:
        return False
    last_lows = [ohlcv[i]["l"] for i in lows[-lookback:]]
    last_highs = [ohlcv[i]["h"] for i in highs[-lookback:]]
    return all(last_lows[i] > last_lows[i-1] for i in range(1, lookback)) and \
           all(last_highs[i] > last_highs[i-1] for i in range(1, lookback))

def ll_lh_down(ohlcv, lows, highs, lookback=3):
    if len(lows) < lookback or len(highs) < lookback:
        return False
    last_lows = [ohlcv[i]["l"] for i in lows[-lookback:]]
    last_highs = [ohlcv[i]["h"] for i in highs[-lookback:]]
    return all(last_lows[i] < last_lows[i-1] for i in range(1, lookback)) and \
           all(last_highs[i] < last_highs[i-1] for i in range(1, lookback))
