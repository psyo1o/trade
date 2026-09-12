# -*- coding: utf-8 -*-
from strategy.market_benchmark import benchmark_label, benchmark_ticker

def test_benchmark_tickers_unified():
    assert benchmark_ticker("KR") == "069500.KS"
    assert benchmark_ticker("US") == "SPY"
    assert benchmark_label("KR") == "KODEX200"
    assert benchmark_label("US") == "SPY"
