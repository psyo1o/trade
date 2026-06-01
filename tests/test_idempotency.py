# -*- coding: utf-8 -*-
"""주문 멱등성 — 슬라이스 키·in-flight·캐시 재사용."""
from __future__ import annotations

import time
import unittest
from datetime import datetime
from execution import idempotency as idem


class TestOrderKeyAndCycleTag(unittest.TestCase):
    def test_order_key_stable(self):
        k = idem.order_key("KR", "005930", "buy", "202605281430", 0)
        self.assertEqual(k, "KR:005930:buy:202605281430:0")

    def test_cycle_tag_15m_slot(self):
        tz = __import__("pytz").timezone("Asia/Seoul")
        dt = datetime(2026, 5, 28, 14, 37, 0, tzinfo=tz)
        self.assertEqual(idem.cycle_tag_15m_kst(dt), "202605281430")

    def test_binance_client_order_id_length(self):
        key = idem.order_key("COIN", "KRW-BTC", "buy", "202605281400", 1)
        cid = idem.binance_client_order_id(key)
        self.assertTrue(cid.startswith("bot"))
        self.assertLessEqual(len(cid), 36)
        self.assertEqual(cid, idem.binance_client_order_id(key))


class TestKisSliceIdempotent(unittest.TestCase):
    def test_cached_slice_not_reordered(self):
        state: dict = {}
        calls = []

        def place():
            calls.append(1)
            return {"rt_cd": "0", "output": {"ORD_PRIC": "70000"}}

        idem._mark_record(
            state,
            idem.order_key("KR", "005930", "buy", "tag1", 0),
            status="filled",
            qty=10.0,
            price=70000.0,
            market="KR",
            ticker="005930",
            side="buy",
        )
        r1 = idem.run_kis_buy_slice_idempotent(
            state,
            market="KR",
            ticker="005930",
            slice_index=0,
            qty=10,
            cycle_tag="tag1",
            place_order=place,
            fallback_price=70000.0,
        )
        r2 = idem.run_kis_buy_slice_idempotent(
            state,
            market="KR",
            ticker="005930",
            slice_index=0,
            qty=10,
            cycle_tag="tag1",
            place_order=place,
            fallback_price=70000.0,
        )
        self.assertTrue(r1.ok)
        self.assertTrue(r2.reused)
        self.assertEqual(len(calls), 0)

    def test_balance_fill_on_failed_rt_cd(self):
        state: dict = {}

        def bal():
            return 15.0

        def place():
            return {"rt_cd": "1", "msg1": "timeout"}

        r = idem.run_kis_buy_slice_idempotent(
            state,
            market="KR",
            ticker="005930",
            slice_index=0,
            qty=10,
            cycle_tag="tag2",
            place_order=place,
            fallback_price=70000.0,
            balance_qty_fn=bal,
            qty_before=5.0,
            max_retries=1,
        )
        self.assertTrue(r.ok)
        self.assertIn("잔고", r.note)


class TestBuyInflight(unittest.TestCase):
    def test_acquire_and_release(self):
        state: dict = {}
        tag = "202605281400"
        self.assertTrue(idem.try_acquire_buy_inflight(state, "US", "AAPL", tag, ttl_sec=60.0))
        self.assertFalse(idem.try_acquire_buy_inflight(state, "US", "AAPL", tag, ttl_sec=60.0))
        skip, reason = idem.should_skip_new_buy(state, "US", "AAPL", tag)
        self.assertTrue(skip)
        self.assertIn("멱등", reason)
        idem.release_buy_inflight(state, "US", "AAPL", tag)
        self.assertTrue(idem.try_acquire_buy_inflight(state, "US", "AAPL", tag, ttl_sec=60.0))

    def test_prune_old_records(self):
        state = {"order_idempotency": {"old:1": {"updated_at": time.time() - 999999}}}
        idem.ensure_idempotency_state(state)
        n = idem.prune_order_idempotency(state, max_age_hours=1.0)
        self.assertGreaterEqual(n, 1)
        self.assertNotIn("old:1", state["order_idempotency"])


class TestBalanceHelpers(unittest.TestCase):
    def test_balance_suggests_fill(self):
        self.assertTrue(idem.balance_suggests_fill(0.0, 10.0, 10.0))
        self.assertFalse(idem.balance_suggests_fill(0.0, 5.0, 10.0))

    def test_kis_balance_stock_qty(self):
        bal = {
            "output1": [
                {"pdno": "005930", "hldg_qty": "3"},
                {"pdno": "000660", "hldg_qty": "1"},
            ]
        }
        self.assertEqual(idem.kis_balance_stock_qty(bal, "005930"), 3.0)
        self.assertEqual(idem.kis_balance_stock_qty(bal, "999999"), 0.0)


class TestSellHelpers(unittest.TestCase):
    def test_balance_suggests_sell_fill(self):
        self.assertTrue(idem.balance_suggests_sell_fill(20.0, 10.0, 10.0))
        self.assertFalse(idem.balance_suggests_sell_fill(20.0, 18.0, 10.0))

    def test_sell_order_key_lane(self):
        k = idem.sell_order_key("KR", "005930", idem.LANE_SWING_HALF, "tag1", 0)
        self.assertIn("sell:swing_half", k)

    def test_kis_sell_cached(self):
        state: dict = {}
        calls = []

        def place():
            calls.append(1)
            return {"rt_cd": "0"}

        idem._mark_record(
            state,
            idem.sell_order_key("US", "AAPL", idem.LANE_EXIT, "t1", 0),
            status="filled",
            qty=5.0,
            price=100.0,
            market="US",
            ticker="AAPL",
            side="sell",
        )
        r = idem.run_kis_sell_slice_idempotent(
            state,
            market="US",
            ticker="AAPL",
            lane=idem.LANE_EXIT,
            slice_index=0,
            qty=5,
            cycle_tag="t1",
            place_order=place,
            fallback_price=100.0,
        )
        self.assertTrue(r.ok)
        self.assertTrue(r.reused)
        self.assertEqual(len(calls), 0)


class TestLedgerReconcile(unittest.TestCase):
    def test_lane_has_filled_sell(self):
        state: dict = {}
        tag = "202605281430"
        idem._mark_record(
            state,
            idem.sell_order_key("KR", "005930", idem.LANE_SCALE_OUT, tag, 0),
            status="filled",
            qty=5.0,
            price=70000.0,
            market="KR",
            ticker="005930",
            side="sell",
        )
        self.assertTrue(
            idem.lane_has_filled_sell(state, "KR", "005930", idem.LANE_SCALE_OUT, tag)
        )

    def test_aggregate_multi_slice(self):
        state: dict = {}
        tag = "202605281430"
        for i, q in enumerate((3.0, 2.0)):
            idem._mark_record(
                state,
                idem.sell_order_key("KR", "005930", idem.LANE_SCALE_OUT, tag, i),
                status="filled",
                qty=q,
                price=70000.0,
                market="KR",
                ticker="005930",
                side="sell",
            )
        agg = idem.aggregate_filled_sells_cycle(state, tag)
        self.assertEqual(agg[("KR", "005930", "scale_out")]["qty"], 5.0)


if __name__ == "__main__":
    unittest.main()
