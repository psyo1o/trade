# -*- coding: utf-8 -*-
"""V8/스윙 타임스탑 임계 — strategy.rules 와 run_bot 동기화."""
import run_bot as rb
from strategy import rules


def test_v8_time_stop_hours_match_rules():
    assert rb.V8_TIME_STOP_HOURS_EQUITY == rules.V8_TIME_STOP_HOURS_EQUITY == 72.0
    assert rb.V8_TIME_STOP_HOURS_COIN == rules.V8_TIME_STOP_HOURS_COIN == 72.0
    assert rb.V8_TIME_STOP_EXEMPT_PROFIT_PCT == rules.V8_TIME_STOP_EXEMPT_PROFIT_PCT == 4.0


def test_swing_time_stop_hours_match_rules():
    assert rb.SWING_TIME_STOP_HOURS_EQUITY == rules.SWING_TIME_STOP_HOURS_EQUITY == 72.0
    assert rb.SWING_TIME_STOP_HOURS_COIN == rules.SWING_TIME_STOP_HOURS_COIN == 72.0


def test_v8_evaluate_time_stop_not_early_at_72h():
    # 71h: 미발동 / 72h+: 발동. (구 336h 임계 시절의 330h 케이스는 이제 발동)
    exit_early, reason_early, exempt_early = rb._evaluate_time_stop(
        market="KR",
        strategy_type="TREND_V8",
        hours_held=71.0,
        profit_rate_now=-15.68,
    )
    assert exit_early is False
    assert reason_early == ""
    assert exempt_early is False

    exit_at_330, reason_330, _ = rb._evaluate_time_stop(
        market="KR",
        strategy_type="TREND_V8",
        hours_held=330.2,
        profit_rate_now=-15.68,
    )
    assert exit_at_330 is True
    assert "V8_TIME_STOP_KR" in reason_330
    assert "≥72h" in reason_330

    exit_coin, _, _ = rb._evaluate_time_stop(
        market="COIN",
        strategy_type="TREND_V8",
        hours_held=48.8,
        profit_rate_now=0.18,
    )
    assert exit_coin is False


def test_v8_evaluate_time_stop_fires_after_72h_without_grace():
    exit_now, reason, exempt = rb._evaluate_time_stop(
        market="KR",
        strategy_type="TREND_V8",
        hours_held=72.0,
        profit_rate_now=-1.0,
    )
    assert exit_now is True
    assert "V8_TIME_STOP_KR" in reason
    assert "≥72h" in reason
    assert exempt is False

    # 유예 +4%
    hold, _, exempt_log = rb._evaluate_time_stop(
        market="COIN",
        strategy_type="TREND_V8",
        hours_held=480.0,
        profit_rate_now=9.52,
    )
    assert hold is False
    assert exempt_log is True
