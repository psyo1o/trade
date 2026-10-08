# -*- coding: utf-8 -*-
"""KIS 토큰 재사용 — 기동 시 이중 tokenP / 1분 발급 제한 회피."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import api.kis_api as kis


def test_token_data_is_fresh_recent():
    tok = {"access_token": "abc", "timestamp": datetime.now().timestamp()}
    assert kis._token_data_is_fresh(tok) is True


def test_token_data_is_fresh_expired():
    old = (datetime.now() - timedelta(hours=12)).timestamp()
    tok = {"access_token": "abc", "timestamp": old}
    assert kis._token_data_is_fresh(tok) is False


def test_resolve_reuses_fresh_token_without_issue():
    fresh = {"access_token": "reuse-me", "timestamp": datetime.now().timestamp()}
    with patch.object(kis, "load_kis_token", return_value=fresh), patch.object(
        kis, "issue_new_kis_token"
    ) as issue:
        out = kis._resolve_access_token(force_new=False)
        assert out["access_token"] == "reuse-me"
        issue.assert_not_called()


def test_resolve_force_new_calls_issue():
    with patch.object(kis, "load_kis_token", return_value=None), patch.object(
        kis, "issue_new_kis_token", return_value={"access_token": "new"}
    ) as issue:
        out = kis._resolve_access_token(force_new=True)
        assert out["access_token"] == "new"
        issue.assert_called_once()


def test_resolve_issue_fail_does_not_return_stale():
    """장기 중단 후: 발급 실패 시 만료 kis_token 으로 폴백하지 않음."""
    stale = {
        "access_token": "expired-old",
        "timestamp": (datetime.now() - timedelta(days=3)).timestamp(),
    }
    with patch.object(kis, "load_kis_token", return_value=stale), patch.object(
        kis, "issue_new_kis_token", return_value=None
    ):
        assert kis._resolve_access_token(force_new=False) is None


def test_token_data_is_fresh_future_rejected():
    future = {
        "access_token": "x",
        "timestamp": (datetime.now() + timedelta(days=30)).timestamp(),
    }
    assert kis._token_data_is_fresh(future) is False


def test_create_brokers_harvests_mojito_when_resolve_none():
    """resolve 실패해도 mojito 가 넣은 토큰을 덮어쓰지 않고 흡수."""
    cfg = {
        "kis_key": "k",
        "kis_secret": "s",
        "kis_account": "12345678-01",
    }
    prev_cfg = kis._cfg
    prev_kr, prev_us = kis.broker_kr, kis.broker_us
    kis._cfg = cfg
    try:

        class FakeBroker:
            def __init__(self, *a, **k):
                self.access_token = "Bearer mojito-fresh"

        with patch.object(kis, "_resolve_access_token", return_value=None), patch.object(
            kis.mojito, "KoreaInvestment", FakeBroker
        ), patch(
            "api.coin_config.active_exchange", return_value="NONE"
        ), patch.object(kis, "save_kis_token") as save_tok, patch.object(
            kis, "_seed_mojito_token_dat", return_value=True
        ):
            kis._create_brokers(force_new_token=False)
            assert kis._normalize_access_token(kis.broker_kr.access_token) == "mojito-fresh"
            assert kis.KIS_TOKEN == "mojito-fresh"
            save_tok.assert_called()
    finally:
        kis._cfg = prev_cfg
        kis.broker_kr, kis.broker_us = prev_kr, prev_us


def test_apply_access_token_publishes_everywhere():
    """갱신 토큰은 브로커·KIS_TOKEN·파일·token.dat 에 같이 반영."""
    kr = SimpleNamespace(access_token="old")
    us = SimpleNamespace(access_token="old")
    prev = (kis.broker_kr, kis.broker_us, kis.KIS_TOKEN, kis._cfg)
    kis.broker_kr, kis.broker_us = kr, us
    kis.KIS_TOKEN = "old"
    kis._cfg = {"kis_key": "k", "kis_secret": "s", "kis_account": "12345678-01"}
    data = {"access_token": "brand-new", "timestamp": datetime.now().timestamp(), "expires_in": 86400}
    try:
        with patch.object(kis, "save_kis_token") as save_tok, patch.object(
            kis, "_seed_mojito_token_dat", return_value=True
        ) as seed:
            out = kis._apply_access_token(data, persist=True)
            assert out == "brand-new"
            assert kr.access_token == "brand-new"
            assert us.access_token == "brand-new"
            assert kis.KIS_TOKEN == "brand-new"
            save_tok.assert_called_once()
            seed.assert_called_once()
    finally:
        kis.broker_kr, kis.broker_us, kis.KIS_TOKEN, kis._cfg = prev


def test_get_us_cash_real_reuses_broker_token_no_tokenp():
    broker = SimpleNamespace(
        access_token="broker-tok",
        base_url="https://openapi.koreainvestment.com:9443",
        acc_no="12345678-01",
    )
    prev = kis.KIS_TOKEN
    kis.KIS_TOKEN = None
    try:
        # TTL 캐시 비우기
        if hasattr(kis.get_us_cash_real, "_us_cash_cache"):
            delattr(kis.get_us_cash_real, "_us_cash_cache")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"output": {"ovrs_ord_psbl_amt": "12.5"}}

        with patch.object(kis, "_cfg", {"kis_key": "k", "kis_secret": "s"}), patch(
            "api.kis_api.requests.post"
        ) as post, patch("api.kis_api.requests.get", return_value=mock_resp), patch(
            "api.kis_rate_limit.wait_for_slot"
        ):
            amt = kis.get_us_cash_real(broker, refresh=True)
            assert amt == 12.5
            post.assert_not_called()
            assert kis.KIS_TOKEN == "broker-tok"
    finally:
        kis.KIS_TOKEN = prev
