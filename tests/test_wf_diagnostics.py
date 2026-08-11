# -*- coding: utf-8 -*-
"""Tests for lib/wf_diagnostics.py -- accounting for unplaced members."""

import os
import sys
import unittest

_LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import wf_diagnostics as d  # noqa: E402


class TallyTests(unittest.TestCase):
    def test_clean_run_reports_nothing(self):
        report = d.PlacementReport()
        report.requested = 10
        report.placed = 10
        self.assertTrue(report.is_clean)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(report.markdown(), "")

    def test_skips_are_counted(self):
        report = d.PlacementReport()
        report.requested = 3
        for _ in range(2):
            report.skip(d.REASON_NO_SYMBOL, "LSF : Ue 90")
        report.placed = 1
        self.assertEqual(report.skipped, 2)
        self.assertFalse(report.is_clean)

    def test_total_failure_is_distinguished_from_partial(self):
        partial = d.PlacementReport()
        partial.requested = 10
        partial.placed = 9
        partial.skip(d.REASON_TOO_SHORT)
        self.assertFalse(partial.is_total_failure)

        total = d.PlacementReport()
        total.requested = 10
        total.placed = 0
        for _ in range(10):
            total.skip(d.REASON_NO_SYMBOL, "LSF : Ue 90")
        self.assertTrue(total.is_total_failure)

    def test_nothing_requested_is_not_a_failure(self):
        # A host with no members to frame is a normal outcome, not an error.
        report = d.PlacementReport()
        self.assertFalse(report.is_total_failure)
        self.assertTrue(report.is_clean)


class GroupingTests(unittest.TestCase):
    def test_identical_skips_collapse_to_one_line(self):
        # A hundred members failing on one missing family must read as
        # one line, not a hundred.
        report = d.PlacementReport()
        for _ in range(100):
            report.skip(d.REASON_NO_SYMBOL, "LSF : Ue 90x40x12x0,95")
        grouped = report.grouped()
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0][2], 100)

    def test_different_details_stay_separate(self):
        report = d.PlacementReport()
        report.skip(d.REASON_NO_SYMBOL, "LSF : Ue 90")
        report.skip(d.REASON_NO_SYMBOL, "LSF : U 90")
        self.assertEqual(len(report.grouped()), 2)

    def test_actionable_reasons_are_listed_before_benign_ones(self):
        # A missing family is something the user fixes; an off-cut shorter
        # than the minimum usually is not. The fixable one goes first.
        report = d.PlacementReport()
        report.skip(d.REASON_TOO_SHORT, "STUD")
        report.skip(d.REASON_NO_SYMBOL, "LSF : Ue 90")
        reasons = [row[0] for row in report.grouped()]
        self.assertEqual(reasons[0], d.REASON_NO_SYMBOL)
        self.assertEqual(reasons[-1], d.REASON_TOO_SHORT)


class MergeTests(unittest.TestCase):
    def test_merging_hosts_accumulates(self):
        first = d.PlacementReport()
        first.requested, first.placed = 10, 9
        first.skip(d.REASON_TOO_SHORT, "STUD")

        second = d.PlacementReport()
        second.requested, second.placed = 5, 4
        second.skip(d.REASON_TOO_SHORT, "STUD")

        first.merge(second)
        self.assertEqual(first.requested, 15)
        self.assertEqual(first.placed, 13)
        self.assertEqual(first.skipped, 2)
        # Same reason and detail across hosts collapses to one line.
        self.assertEqual(len(first.grouped()), 1)
        self.assertEqual(first.grouped()[0][2], 2)

    def test_merging_none_is_a_no_op(self):
        report = d.PlacementReport()
        report.requested = 4
        report.merge(None)
        self.assertEqual(report.requested, 4)


class RenderingTests(unittest.TestCase):
    def test_markdown_names_the_take_off_consequence(self):
        # The point of the report is that a dropped member is a quantity
        # missing from the schedule -- that has to be said, not implied.
        report = d.PlacementReport()
        report.requested, report.placed = 10, 8
        report.skip(d.REASON_NO_SYMBOL, "LSF : Ue 90")
        report.skip(d.REASON_NO_SYMBOL, "LSF : Ue 90")
        markdown = report.markdown()
        self.assertIn("take-off", markdown)
        self.assertIn("2 x", markdown)
        self.assertIn("Ue 90", markdown)

    def test_total_failure_gets_an_explicit_callout(self):
        report = d.PlacementReport()
        report.requested, report.placed = 5, 0
        for _ in range(5):
            report.skip(d.REASON_NO_SYMBOL, "LSF : Ue 90")
        self.assertIn("Nothing was placed", report.markdown())

    def test_summary_line_counts_match(self):
        report = d.PlacementReport()
        report.requested, report.placed = 10, 7
        for _ in range(3):
            report.skip(d.REASON_TOO_SHORT)
        self.assertEqual(report.summary_line(),
                         "Requested 10, placed 7, skipped 3.")

    def test_skip_without_detail_still_renders(self):
        report = d.PlacementReport()
        report.requested, report.placed = 1, 0
        report.skip(d.REASON_TOO_SHORT)
        self.assertIn(d.REASON_TOO_SHORT, report.markdown())


if __name__ == "__main__":
    unittest.main()
