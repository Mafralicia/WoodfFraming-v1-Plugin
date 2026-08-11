# -*- coding: utf-8 -*-
"""Tests for lib/wf_takeoff.py -- net-to-purchase quantity arithmetic."""

import os
import sys
import unittest

_LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import wf_takeoff as t  # noqa: E402


class ApplyWasteTests(unittest.TestCase):
    def test_ten_percent(self):
        self.assertAlmostEqual(t.apply_waste(100.0, 10.0), 110.0)

    def test_zero_waste_is_identity(self):
        self.assertEqual(t.apply_waste(100.0, 0.0), 100.0)

    def test_negative_waste_is_ignored_not_applied(self):
        # A negative allowance is meaningless; buying less than measured
        # is never the intent, so it must not silently shrink the order.
        self.assertEqual(t.apply_waste(100.0, -5.0), 100.0)

    def test_none_quantity_stays_none(self):
        self.assertIsNone(t.apply_waste(None, 10.0))

    def test_none_waste_is_identity(self):
        self.assertEqual(t.apply_waste(100.0, None), 100.0)


class BarsRequiredTests(unittest.TestCase):
    def test_exact_multiple_needs_no_extra_bar(self):
        # 54 m net + 0% waste over 6 m bars is exactly 9.
        self.assertEqual(t.bars_required(54.0, 6.0, 0.0), 9)

    def test_rounds_up_on_any_remainder(self):
        self.assertEqual(t.bars_required(54.1, 6.0, 0.0), 10)

    def test_waste_can_push_it_over_a_bar(self):
        # 54 m exactly fills 9 bars; a 10% allowance needs 59.4 m -> 10.
        self.assertEqual(t.bars_required(54.0, 6.0, 10.0), 10)

    def test_zero_and_negative_length_need_no_bars(self):
        self.assertEqual(t.bars_required(0.0), 0)
        self.assertEqual(t.bars_required(-5.0), 0)

    def test_invalid_bar_length_yields_zero_rather_than_dividing_by_zero(self):
        self.assertEqual(t.bars_required(100.0, 0.0), 0)
        self.assertEqual(t.bars_required(100.0, None), 0)

    def test_three_metre_stock_needs_more_bars(self):
        self.assertEqual(t.bars_required(30.0, 6.0, 0.0), 5)
        self.assertEqual(t.bars_required(30.0, 3.0, 0.0), 10)


class PurchaseLineTests(unittest.TestCase):
    def _line(self):
        return t.PurchaseLine("Ue 90x40x12x0,95", length_m=100.0,
                              mass_kg=144.7, volume_m3=0.5, count=40)

    def test_purchase_figures_include_waste(self):
        line = self._line()
        self.assertAlmostEqual(line.purchase_length_m(10.0), 110.0)
        self.assertAlmostEqual(line.purchase_mass_kg(10.0), 159.17)
        self.assertAlmostEqual(line.purchase_volume_m3(10.0), 0.55)

    def test_net_figures_are_untouched_by_the_waste_setting(self):
        # The measurement must never be mutated by a commercial
        # assumption -- that is the whole reason the two are separate.
        line = self._line()
        line.purchase_mass_kg(25.0)
        self.assertEqual(line.mass_kg, 144.7)
        self.assertEqual(line.length_m, 100.0)


class SummarizeTests(unittest.TestCase):
    def _lines(self):
        return [
            t.PurchaseLine("Ue 90x40x12x0,95", length_m=100.0, mass_kg=144.7, count=40),
            t.PurchaseLine("U 90x40x0,95", length_m=50.0, mass_kg=63.4, count=20),
        ]

    def test_totals_add_up(self):
        result = t.summarize(self._lines(), waste_pct=10.0, bar_length_m=6.0)
        self.assertAlmostEqual(result["net_length_m"], 150.0)
        self.assertAlmostEqual(result["net_mass_kg"], 208.1)
        self.assertAlmostEqual(result["purchase_mass_kg"], 228.91)
        self.assertEqual(result["total_count"], 60)

    def test_bars_are_summed_per_profile_not_from_the_grand_total(self):
        # Stock cannot be shared between different profiles: 110 m and
        # 55 m of two profiles is 19 + 10 = 29 bars, not ceil(165/6)=28.
        result = t.summarize(self._lines(), waste_pct=10.0, bar_length_m=6.0)
        self.assertEqual([r["bars"] for r in result["rows"]], [19, 10])
        self.assertEqual(result["total_bars"], 29)
        self.assertNotEqual(result["total_bars"], 28)

    def test_settings_are_echoed_so_a_report_can_state_them(self):
        result = t.summarize(self._lines(), waste_pct=12.5, bar_length_m=3.0)
        self.assertEqual(result["waste_pct"], 12.5)
        self.assertEqual(result["bar_length_m"], 3.0)

    def test_empty_input_is_all_zero(self):
        for empty in ([], None):
            result = t.summarize(empty)
            self.assertEqual(result["rows"], [])
            self.assertEqual(result["total_bars"], 0)
            self.assertEqual(result["net_mass_kg"], 0.0)

    def test_defaults_are_documented_starting_points_not_zero(self):
        # A silently-zero default would make the purchase report identical
        # to the net one and hide the fact that an allowance applies.
        self.assertGreater(t.DEFAULT_WASTE_PCT, 0)
        self.assertGreater(t.DEFAULT_BAR_LENGTH_M, 0)


if __name__ == "__main__":
    unittest.main()
