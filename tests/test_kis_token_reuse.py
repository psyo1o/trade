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
