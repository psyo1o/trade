import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.kis_parsers import as_row_dict, parse_kr_cash_total, parse_us_cash_fallback, parse_us_qty


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
        cash, total = parse_kr_cash_total([{"prvs_rcdl_excc_amt": "1000", "tot_evlu_amt": "1200"}], _to_float)
        self.assertEqual(cash, 1000)
        self.assertEqual(total, 1200)
        cash2, total2 = parse_kr_cash_total({"prvs_rcdl_excc_amt": "500"}, _to_float)
        self.assertEqual(cash2, 500)
        self.assertEqual(total2, 500)

    def test_parse_us_cash_fallback(self):
        self.assertEqual(parse_us_cash_fallback([{"frcr_dncl_amt_2": "12.5"}], _to_float), 12.5)
        self.assertEqual(parse_us_cash_fallback({"frcr_buy_amt_smtl": "7.25"}, _to_float), 7.25)

    def test_parse_us_qty(self):
        self.assertEqual(parse_us_qty({"ovrs_cblc_qty": "3"}, _to_float), 3.0)
        self.assertEqual(parse_us_qty({"ccld_qty_smtl1": "2"}, _to_float), 2.0)
        self.assertEqual(parse_us_qty({"hldg_qty": "1"}, _to_float), 1.0)


if __name__ == "__main__":
    unittest.main()

