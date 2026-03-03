def pct_change(a, b):
    if a == 0:
        return 0.0
    return (b - a) / a

def close_series(ohlcv):
    return [r["c"] for r in ohlcv]

def rs_score(stock_d, bench_d):
    """
    상대강도 간단 점수 (스마트 버전):
    데이터가 120일 치가 없어도 에러 내지 않고 알아서 60일 치로 대체하여 계산합니다!
    """
    s = close_series(stock_d)
    b = close_series(bench_d)
    n = min(len(s), len(b))
    
    # 💡 데이터가 61개 미만이면 아예 계산을 포기하고 넘깁니다 (에러 방지 철책)
    if n <= 60:
        return None

    def ret(series, look):
        return pct_change(series[-look-1], series[-1])

    # 20일, 60일 수익률 계산 (이건 데이터가 60개만 넘으면 무조건 계산 가능)
    r20  = ret(s, 20)  - ret(b, 20)
    r60  = ret(s, 60)  - ret(b, 60)
    
    # 💡 120일 데이터가 부족하면 에러를 뿜는 대신, 60일 수익률을 빌려와서 씁니다!
    if n > 120:
        r120 = ret(s, 120) - ret(b, 120)
    else:
        r120 = r60 

    # 가중치 계산
    score = (0.40 * r60) + (0.35 * r20) + (0.25 * r120)
    return score
