# -*- coding: utf-8 -*-
"""
업비트 REST 래퍼 — ``pyupbit.Upbit`` 싱글톤.

``kis_api._create_brokers`` 안에서 ``init_upbit(config)`` 가 호출되며,
이후 ``upbit`` 전역으로 잔고·시장가 주문을 수행한다.

설정 키 (``config.json``)
    * ``upbit_access``, ``upbit_secret`` — API 키.
"""
import pyupbit

upbit = None


def init_upbit(config: dict):
    """config['upbit_access'], config['upbit_secret'] 로 클라이언트 생성."""
    global upbit
    upbit = pyupbit.Upbit(config["upbit_access"], config["upbit_secret"])
    return upbit


def get_balances():
    """업비트 전체 잔고 목록."""
    return upbit.get_balances()


def get_balance_ticker(ticker: str):
    """특정 화폐 잔고 (예: 'KRW')."""
    return upbit.get_balance(ticker)


def sell_market_order(ticker: str, qty):
    return upbit.sell_market_order(ticker, qty)


def buy_market_order(ticker: str, budget):
    return upbit.buy_market_order(ticker, budget)
