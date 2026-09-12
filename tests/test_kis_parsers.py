import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.kis_parsers import (
    as_row_dict,
    compute_us_stock_value_from_output,
    ensure_output1,
    extract_held_kr_codes,
    extract_held_us_codes,
    parse_kr_cash_total,
    parse_kr_holdings_metrics,
    parse_us_cash_fallback,
    parse_us_holdings_metrics,
    parse_us_qty,
    sum_us_output1_stock_value_usd,
)


def _to_float(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return float(d)


class TestKisParsers(unittest.TestCase):
    def test_as_row_dict(self):
        self.assertEqual(as_row_dict([{"a": 1}]).get("a"), 1)
        self.assertEqual(as_row_dict({"a": 2}).get("a"), 2)
        self.assertEqual(as_row_dict(None), {})

    def test_parse_kr_cash_total(self):
        # 보유 필드 없음(scts=0) → 표시 예수=총평=NAV
        cash, total = parse_kr_cash_total(
            [{"prvs_rcdl_excc_amt": "1000", "tot_evlu_amt": "1200"}], _to_float
        )
        self.assertEqual(cash, 1200)
        self.assertEqual(total, 1200)
        cash2, total2 = parse_kr_cash_total({"prvs_rcdl_excc_amt": "500"}, _to_float)
        self.assertEqual(cash2, 500)
        self.assertEqual(total2, 500)

    def test_parse_kr_orderable_cash_is_d2(self):
        from api.kis_parsers import parse_kr_orderable_cash

        self.assertEqual(
            parse_kr_orderable_cash(
                {"prvs_rcdl_excc_amt": "0", "dnca_tot_amt": "447377"},
                _to_float,
            ),
            0,
        )

    def test_parse_kr_cash_post_sell_d2_boosts_total_when_nass_stale(self):
        """매도 후 보유 0·D+2에 대금 → 표시 예수=총평=NAV (MTS 순자산)."""
        cash, total = parse_kr_cash_total(
            {
                "prvs_rcdl_excc_amt": "667000",
                "dnca_tot_amt": "350510",
                "scts_evlu_amt": "0",
                "tot_evlu_amt": "667000",
                "nass_amt": "350510",
            },
            _to_float,
        )
        self.assertEqual(total, 667000)
        self.assertEqual(cash, 667000)

    def test_parse_kr_cash_prefers_dnca_over_d2(self):
        """공식: tot_evlu = 유가+D+2. 당일 매수 후 D+2=0 이면 tot_evlu=보유만."""
        cash, total = parse_kr_cash_total(
            {
                "prvs_rcdl_excc_amt": "0",
                "dnca_tot_amt": "447377",
                "scts_evlu_amt": "209880",
                "tot_evlu_amt": "209880",
                "nass_amt": "657257",
            },
            _to_float,
        )
        self.assertEqual(cash, 447377)
        self.assertEqual(total, 657257)

    def test_parse_kr_cash_prefers_dnca_even_if_d2_leftover(self):
        """D+2가 잔돈만 남아도 표시 예수는 dnca, 총평은 nass."""
        cash, total = parse_kr_cash_total(
            {
                "prvs_rcdl_excc_amt": "500",
                "dnca_tot_amt": "447377",
                "scts_evlu_amt": "209880",
                "tot_evlu_amt": "210380",
                "nass_amt": "657257",
            },
            _to_float,
        )
        self.assertEqual(cash, 447377)
        self.assertEqual(total, 657257)

    def test_parse_kr_cash_total_d2_zero_uses_dnca(self):
        cash, total = parse_kr_cash_total(
            {
                "prvs_rcdl_excc_amt": "0",
                "dnca_tot_amt": "447377",
                "scts_evlu_amt": "209880",
                "tot_evlu_amt": "657257",
            },
            _to_float,
        )
        self.assertEqual(cash, 447377)
        self.assertEqual(total, 657257)

    def test_parse_kr_cash_total_tot_equals_scts_uses_nass(self):
        cash, total = parse_kr_cash_total(
            {
                "prvs_rcdl_excc_amt": "0",
                "dnca_tot_amt": "0",
                "scts_evlu_amt": "209880",
                "tot_evlu_amt": "209880",
                "nass_amt": "657257",
            },
            _to_float,
        )
        self.assertEqual(cash, 447377)
        self.assertEqual(total, 657257)

    def test_parse_kr_cash_stale_dnca_uses_nass_minus_scts(self):
        """매수 직후 dnca가 안 줄고 nass만 맞으면 예수는 nass-유가."""
        cash, total = parse_kr_cash_total(
            {
                "prvs_rcdl_excc_amt": "0",
                "dnca_tot_amt": "658178",
                "scts_evlu_amt": "309045",
                "tot_evlu_amt": "309045",
                "nass_amt": "658178",
            },
            _to_float,
        )
        self.assertEqual(total, 658178)
        self.assertEqual(cash, 658178 - 309045)
        self.assertEqual(parse_us_cash_fallback([{"frcr_dncl_amt_2": "12.5"}], _to_float), 12.5)
        self.assertEqual(parse_us_cash_fallback({"frcr_buy_amt_smtl": "7.25"}, _to_float), 7.25)

    def test_parse_us_qty(self):
        self.assertEqual(parse_us_qty({"ovrs_cblc_qty": "3"}, _to_float), 3.0)
        self.assertEqual(parse_us_qty({"ccld_qty_smtl1": "2"}, _to_float), 2.0)
        self.assertEqual(parse_us_qty({"hldg_qty": "1"}, _to_float), 1.0)

    def test_ensure_output1_empty(self):
        self.assertEqual(ensure_output1(None), [])
        self.assertEqual(ensure_output1({}), [])
        self.assertEqual(ensure_output1({"output1": "bad"}), [])

    def test_parse_kr_holdings_metrics(self):
        bal = {
            "output1": [
                {
                    "hldg_qty": "10",
                    "pchs_avg_prc": "100",
                    "prpr": "110",
                    "pdno": "005930",
                }
            ]
        }
        m = parse_kr_holdings_metrics(bal, _to_float)
        self.assertAlmostEqual(m["invested"], 1000.0)
        self.assertAlmostEqual(m["current"], 1100.0)
        self.assertAlmostEqual(m["profit"], 100.0)
        self.assertAlmostEqual(m["roi"], 10.0)

    def test_parse_us_holdings_metrics_ccld_fallback(self):
        bal = {
            "output1": [
                {
                    "ovrs_cblc_qty": "0",
                    "ccld_qty_smtl1": "2",
                    "ovrs_avg_unpr": "50",
                    "ovrs_now_prc2": "55",
                }
            ]
        }
        m = parse_us_holdings_metrics(bal, _to_float)
        self.assertAlmostEqual(m["invested"], 100.0)
        self.assertAlmostEqual(m["current"], 110.0)

    def test_extract_held_codes(self):
        norm = lambda x: str(x).strip().upper()
        kr = extract_held_kr_codes(
            [{"pdno": "005930", "hldg_qty": "1"}, {"pdno": "000", "hldg_qty": "0"}],
            _to_float,
            norm,
        )
        self.assertEqual(kr, ["005930"])
        us = extract_held_us_codes(
            [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "2"}],
            _to_float,
            norm,
        )
        self.assertEqual(us, ["AAPL"])

    def test_compute_us_stock_value_manual_fallback(self):
        us_bal = {
            "output1": [
                {
                    "frcr_evlu_amt2": "0",
                    "ovrs_now_prc2": "10",
                    "ovrs_cblc_qty": "5",
                }
            ]
        }
        with patch("builtins.print"):
            v = compute_us_stock_value_from_output(us_bal, [], _to_float)
        self.assertAlmostEqual(v, 50.0)
        v2 = compute_us_stock_value_from_output(
            us_bal, [{"ovrs_stck_evlu_amt": "99"}], _to_float
        )
        self.assertAlmostEqual(v2, 99.0)

    def test_sum_us_output1_stock_value_usd(self):
        rows = [{"frcr_evlu_amt2": "12.5"}]
        self.assertAlmostEqual(sum_us_output1_stock_value_usd(rows, _to_float), 12.5)


if __name__ == "__main__":
    unittest.main()

