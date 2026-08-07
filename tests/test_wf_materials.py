# -*- coding: utf-8 -*-
"""Regression tests for lib/wf_materials.py.

wf_materials.py is pure Python with zero Revit/pyRevit dependency (the one
Revit-touching function, warn_unresolved_steel_dims, is written to degrade
to a no-op when pyrevit isn't importable), so this suite runs with a plain
CPython interpreter -- no Revit, no pyRevit, no IronPython required:

    python -m unittest discover -s tests -v

or, if pytest is available:

    pytest tests/

This is the repo's only automated test coverage. It exists to catch
regressions in the wood/steel dimension-resolution logic specifically,
since that logic silently drives real framing geometry and is otherwise
only verifiable by manually running the plugin inside Revit.
"""

import os
import sys
import unittest

_LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import wf_materials as m  # noqa: E402  (path setup must run first)


class DecodeSsmaDesignationTests(unittest.TestCase):
    def test_exact_hundredths_code(self):
        # "350" -> 3.50" needs no eighth-inch correction.
        self.assertEqual(m.decode_ssma_designation("350S162-33"), (1.625, 3.5))

    def test_traditional_rounded_code_recovers_true_eighth(self):
        # "162" is a traditional label for 1-5/8" (1.625"), not literally
        # 1.62" -- this is the bug fixed after the initial implementation.
        flange, depth = m.decode_ssma_designation("362S162-33")
        self.assertEqual(flange, 1.625)
        self.assertEqual(depth, 3.625)

    def test_track_shape_letter(self):
        self.assertEqual(m.decode_ssma_designation("600T125-54"), (1.25, 6.0))

    def test_case_insensitive(self):
        self.assertEqual(m.decode_ssma_designation("350s162-33"), (1.625, 3.5))

    def test_non_designation_returns_none(self):
        self.assertIsNone(m.decode_ssma_designation("not-a-code"))
        self.assertIsNone(m.decode_ssma_designation(""))
        self.assertIsNone(m.decode_ssma_designation(None))

    def test_decodes_designations_outside_the_static_catalog(self):
        # Any valid SSMA-shaped token should decode even if it isn't one of
        # the specific sizes hard-coded in STEEL_ACTUAL.
        self.assertNotIn("400T200-97", m.STEEL_ACTUAL)
        self.assertEqual(m.decode_ssma_designation("400T200-97"), (2.0, 4.0))


class ActualDimsFromTextTests(unittest.TestCase):
    def test_steel_catalog_hit(self):
        self.assertEqual(m.actual_dims_from_text("350S162-33"), (1.625, 3.5))
        self.assertEqual(
            m.actual_dims_from_text("Steel Stud Framing : 600S200-54"),
            (2.0, 6.0),
        )

    def test_steel_generic_decode_when_not_in_catalog(self):
        self.assertEqual(m.actual_dims_from_text("362S162-33"), (1.625, 3.625))

    def test_wood_nominal_size(self):
        self.assertEqual(
            m.actual_dims_from_text("Wood Dimension Lumber-Framing 2x6"),
            (1.5, 5.5),
        )
        self.assertEqual(m.actual_dims_from_text("2x4"), (1.5, 3.5))

    def test_empty_and_none_return_none(self):
        self.assertIsNone(m.actual_dims_from_text(""))
        self.assertIsNone(m.actual_dims_from_text(None))
        self.assertIsNone(m.actual_dims_from_text("Nothing Recognizable"))

    def test_no_false_positive_on_steel_section_names(self):
        # Regression coverage: an earlier version used an unbounded
        # substring check ("is '2x2' in text") that silently matched real
        # steel section names as wood 2x2 lumber. None of these are wood.
        false_positive_cases = [
            "HSS2X2X1/4",       # steel tube, actually 2"x2"
            "W12X26",            # wide-flange beam
            "L2x2x1/4 Angle",    # steel angle, actually 2"x2" legs
            "C12x20.7",          # C-channel (matches via "...12x20...")
        ]
        for text in false_positive_cases:
            self.assertIsNone(
                m.actual_dims_from_text(text),
                "expected no match for {0!r} (false positive risk)".format(text),
            )

    def test_steel_designation_takes_priority_over_wood_pattern(self):
        # A steel designation should never fall through to wood matching.
        dims = m.actual_dims_from_text("350S162-33 replaces old 2x6")
        self.assertEqual(dims, (1.625, 3.5))


class MaterialDefaultsTests(unittest.TestCase):
    def test_wood_defaults(self):
        defaults = m.get_material_defaults(m.MATERIAL_WOOD)
        self.assertEqual(defaults["stud_width_in"], 1.5)
        self.assertEqual(defaults["header_ply_spacer_in"], 0.5)

    def test_steel_defaults(self):
        defaults = m.get_material_defaults(m.MATERIAL_STEEL)
        self.assertEqual(defaults["stud_width_in"], 1.625)
        self.assertEqual(defaults["header_depth_in"], 6.0)
        self.assertEqual(
            defaults["header_ply_spacer_in"], 0.0,
            "steel built-up headers are fastened directly together, no shim",
        )

    def test_unknown_material_falls_back_to_wood(self):
        self.assertEqual(
            m.get_material_defaults("unobtainium"),
            m.get_material_defaults(m.MATERIAL_WOOD),
        )

    def test_fallback_depth_ft_header_vs_stud_differ_for_steel(self):
        stud = m.fallback_depth_ft(m.MATERIAL_STEEL, "stud")
        header = m.fallback_depth_ft(m.MATERIAL_STEEL, "header")
        self.assertNotEqual(
            stud, header,
            "header and stud fallback depths must differ for steel so a "
            "header family missing its depth parameter doesn't silently "
            "size as a stud",
        )

    def test_fallback_width_ft_matches_stud_width_in(self):
        for material in (m.MATERIAL_WOOD, m.MATERIAL_STEEL):
            expected = m.get_material_defaults(material)["stud_width_in"] / 12.0
            self.assertAlmostEqual(m.fallback_width_ft(material), expected)

    def test_header_ply_spacer_ft_units(self):
        self.assertAlmostEqual(m.header_ply_spacer_ft(m.MATERIAL_WOOD), 0.5 / 12.0)
        self.assertEqual(m.header_ply_spacer_ft(m.MATERIAL_STEEL), 0.0)


class CatalogSanityTests(unittest.TestCase):
    def test_lumber_actual_matches_bd_convention(self):
        # (width, depth) -- width is always the 1.5" nominal-lumber
        # thickness; depth varies by nominal size.
        for nominal, (width, depth) in m.LUMBER_ACTUAL.items():
            self.assertEqual(width, 1.5, nominal)
            self.assertGreaterEqual(depth, width, nominal)

    def test_steel_actual_keys_all_decode_consistently(self):
        # Every hard-coded catalog entry must agree with the generic
        # decoder -- otherwise the two paths in actual_dims_from_text()
        # could silently disagree depending on which one fires first.
        for designation, dims in m.STEEL_ACTUAL.items():
            self.assertEqual(
                m.decode_ssma_designation(designation), dims,
                "catalog entry {0!r} disagrees with decode_ssma_designation".format(designation),
            )

    def test_depth_and_width_param_names_are_nonempty_tuples(self):
        self.assertIn("d", m.DEPTH_PARAM_NAMES)
        self.assertIn("b", m.WIDTH_PARAM_NAMES)


class WarnUnresolvedSteelDimsTests(unittest.TestCase):
    def test_never_raises_outside_revit(self):
        # pyrevit is not importable in this environment (no Revit host) --
        # the warning helper must degrade to a no-op, never raise, since a
        # cosmetic warning must never be able to abort a real run.
        try:
            m.warn_unresolved_steel_dims("wall stud", "SomeFamily", "SomeType")
            m.warn_unresolved_steel_dims("wall stud", None, None)
        except Exception as exc:  # pragma: no cover - failure path
            self.fail("warn_unresolved_steel_dims raised: {0!r}".format(exc))


class MmToSpacingInTests(unittest.TestCase):
    def test_400mm_matches_known_inch_equivalent(self):
        self.assertAlmostEqual(m.mm_to_spacing_in(m.SPACING_400MM), 15.748, places=3)

    def test_600mm_matches_known_inch_equivalent(self):
        self.assertAlmostEqual(m.mm_to_spacing_in(m.SPACING_600MM), 23.622, places=3)

    def test_zero_is_zero(self):
        self.assertEqual(m.mm_to_spacing_in(0.0), 0.0)


class PortugueseParamAliasTests(unittest.TestCase):
    def test_depth_param_names_include_portuguese_aliases(self):
        self.assertIn("Profundidade", m.DEPTH_PARAM_NAMES)
        self.assertIn("Altura", m.DEPTH_PARAM_NAMES)

    def test_width_param_names_include_portuguese_alias(self):
        self.assertIn("Largura", m.WIDTH_PARAM_NAMES)


class RevitMaterialHelperTests(unittest.TestCase):
    # None of these touch a real Revit doc -- Autodesk.Revit.DB is not
    # importable outside the Revit host, so every helper here must degrade
    # to a safe, silent no-op rather than raising.

    def test_list_materials_returns_empty_list_outside_revit(self):
        self.assertEqual(m.list_materials(doc=None), [])

    def test_guess_steel_material_id_returns_none_outside_revit(self):
        self.assertIsNone(m.guess_steel_material_id(doc=None))

    def test_set_structural_material_false_on_none_symbol(self):
        self.assertFalse(m.set_structural_material(None, None, "some-id"))

    def test_set_structural_material_false_on_none_material_id(self):
        self.assertFalse(m.set_structural_material(None, object(), None))

    def test_steel_material_name_hints_cover_portuguese_terms(self):
        for term in ("aço", "aco", "galvanizado", "metálico"):
            self.assertIn(term, m.STEEL_MATERIAL_NAME_HINTS)


if __name__ == "__main__":
    unittest.main()
