# -*- coding: utf-8 -*-
"""스캐너 토큰 만료 판별."""
from screener import _is_expired_token_response
from api.kis_parsers import kis_response_token_expired


def test_expired_token_egw00123():
    payload = {
        "rt_cd": "1",
        "msg1": "기간이 만료된 token 입니다.",
        "msg_cd": "EGW00123",
    }
    assert _is_expired_token_response(payload)
    assert kis_response_token_expired(payload)


def test_fresh_ok_response_not_expired():
    assert not _is_expired_token_response({"rt_cd": "0", "output2": []})
    assert not kis_response_token_expired({"rt_cd": "0", "output2": []})
