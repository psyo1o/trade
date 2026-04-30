# -*- coding: utf-8 -*-
"""코인 거래소 선택 — ``config.json`` 의 ``upbit_enabled`` / ``binance_enabled`` / ``market_preference``."""

_cfg: dict = {}


def configure(config: dict) -> None:
    global _cfg
    _cfg = dict(config or {})


def _pref() -> str:
    return str(_cfg.get("market_preference") or "UPBIT").strip().upper()


def upbit_enabled() -> bool:
    """기본 True (기존 사용자 호환). 명시 False 일 때만 비활성."""
    return _cfg.get("upbit_enabled", True) is not False


def binance_enabled() -> bool:
    return bool(_cfg.get("binance_enabled", False))


def active_exchange() -> str:
    """
    실제 매매·조회에 사용할 거래소 한 곳만 반환.

    * ``market_preference`` 가 BINANCE 이고 ``binance_enabled`` 이면 BINANCE.
    * ``market_preference`` 가 UPBIT 이고 ``upbit_enabled`` 이면 UPBIT.
    * 그 외: 바이낸스만 켜져 있으면 BINANCE, 아니면 UPBIT.
    """
    pref = _pref()
    if pref == "BINANCE" and binance_enabled():
        return "BINANCE"
    if pref == "UPBIT" and upbit_enabled():
        return "UPBIT"
    if binance_enabled() and not upbit_enabled():
        return "BINANCE"
    return "UPBIT"


def is_binance() -> bool:
    return active_exchange() == "BINANCE"


def is_upbit() -> bool:
    return active_exchange() == "UPBIT"


def btc_benchmark_ticker() -> str:
    """날씨·지수용 BTC 벤치마크 티커."""
    return "USDT-BTC" if is_binance() else "KRW-BTC"


def get(key: str, default=None):
    """원본 설정 조회(환율·coin_min_notional_usd 등)."""
    return _cfg.get(key, default)
