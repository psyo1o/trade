# -*- coding: utf-8 -*-
"""30분 생존신고 — 스냅샷 조립 + 텔레그램 발송."""
from __future__ import annotations


def run_heartbeat_report() -> None:
    """모든 자산 현황을 종합하여 텔레그램으로 보고."""
    import run_bot as rb
    from services.account_display import build_account_snapshot_for_report
    from services.report_formatter import format_survival_telegram_message

    print("💓 생존 신고 보고서 생성 중...")
    try:
        snap = build_account_snapshot_for_report()
        holdings = snap.get("holdings") or {}
        kr_holdings = list(holdings.get("kr") or [])
        us_holdings = list(holdings.get("us") or [])
        coin_holdings = list(holdings.get("coin") or [])

        print("📊 [생존신고] 보유 (텔레그램 동일)")
        for _hb_line in kr_holdings + us_holdings + coin_holdings:
            print(_hb_line)

        msg = format_survival_telegram_message(
            snap,
            weekend_kis_suppress=bool(rb.kis_equities_weekend_suppress_window_kst()),
        )
        if rb.send_telegram(msg):
            print("  ✅ 텔레그램 보고 완료")
        else:
            print("  ⚠️ 텔레그램 생존신고 미전송 — 네트워크·텔레 API 확인 후 필요 시 재실행")
    except Exception as e:
        print(f"⚠️ 보고 에러: {e}")
        import traceback

        traceback.print_exc()
