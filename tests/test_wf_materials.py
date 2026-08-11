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


class NbrDesignationDecodeTests(unittest.TestCase):
    """ABNT/NBR 6355 / NBR 15253 profile designations, in millimeters."""

    def test_lipped_channel_montante(self):
        profile = m.decode_nbr_designation("Ue 90x40x12x0,95")
        self.assertEqual(profile.shape, m.SHAPE_LIPPED_CHANNEL)
        self.assertEqual(
            (profile.web_mm, profile.flange_mm, profile.lip_mm, profile.thickness_mm),
            (90.0, 40.0, 12.0, 0.95),
        )

    def test_plain_channel_guia(self):
        profile = m.decode_nbr_designation("U 90x40x0,95")
        self.assertEqual(profile.shape, m.SHAPE_PLAIN_CHANNEL)
        self.assertEqual(profile.lip_mm, 0.0)
        self.assertEqual((profile.web_mm, profile.flange_mm), (90.0, 40.0))

    def test_lipped_is_matched_before_plain(self):
        # "U" is a prefix of "Ue" -- a plain-channel-first implementation
        # would silently read every montante as a guia and lose the lip,
        # under-reporting mass on every stud in the model.
        profile = m.decode_nbr_designation("Ue 140x40x12x1,25")
        self.assertEqual(profile.shape, m.SHAPE_LIPPED_CHANNEL)
        self.assertEqual(profile.lip_mm, 12.0)

    def test_accepts_period_as_well_as_brazilian_comma(self):
        comma = m.decode_nbr_designation("Ue 90x40x12x0,95")
        period = m.decode_nbr_designation("Ue 90x40x12x0.95")
        self.assertEqual(comma.thickness_mm, period.thickness_mm)

    def test_tolerates_spaces_around_separators(self):
        profile = m.decode_nbr_designation("Montante Ue 90 x 40 x 12 x 0,80")
        self.assertEqual(profile.web_mm, 90.0)
        self.assertEqual(profile.thickness_mm, 0.80)

    def test_commercial_pg_naming_has_no_invented_thickness(self):
        # "PGC 90" carries only the web depth. Inventing a thickness would
        # produce a confident, wrong weight -- it must stay unknown.
        profile = m.decode_nbr_designation("PGC 90")
        self.assertEqual(profile.web_mm, 90.0)
        self.assertEqual(profile.shape, m.SHAPE_LIPPED_CHANNEL)
        self.assertIsNone(profile.thickness_mm)
        self.assertIsNone(profile.linear_mass_kg_m)

    def test_pgu_is_a_plain_channel(self):
        self.assertEqual(
            m.decode_nbr_designation("PGU 90").shape, m.SHAPE_PLAIN_CHANNEL
        )

    def test_non_designation_returns_none(self):
        for text in ("", None, "2x6", "Generic Framing", "HSS2X2X1/4"):
            self.assertIsNone(m.decode_nbr_designation(text), repr(text))

    def test_actual_dims_resolves_nbr_instead_of_falling_back(self):
        # The regression this guards: before ABNT decoding existed, every
        # Brazilian profile name returned None here and the engines
        # substituted a generic American default.
        width_in, depth_in = m.actual_dims_from_text("Ue 140x40x12x0,95")
        self.assertAlmostEqual(depth_in, 140.0 / 25.4, places=6)
        self.assertAlmostEqual(width_in, 40.0 / 25.4, places=6)

    def test_wood_still_resolves_after_nbr_was_added(self):
        self.assertEqual(m.actual_dims_from_text("2x6"), (1.5, 5.5))


class RealWorldNamingTests(unittest.TestCase):
    """Naming forms that turn up in actual Brazilian Revit projects.

    Family names are written by whoever built the family, so the decoder
    has to cope with Portuguese words, manufacturer prefixes, missing
    spaces, slash separators and mm suffixes -- not just the textbook
    ABNT form.
    """

    LIPPED_90 = (
        "Ue 90x40x12x0,95",
        "Ue90x40x12x0.95",
        "Ue 90 x 40 x 12 x 0,95",
        "Ue 90/40/12/0,95",
        "Montante 90x40x12x0,95",
        "Perfil Ue 90x40x12x0,95",
        "Perfilor Ue 90x40x12x0,95",
        "LSF Montante Ue 90x40x12x0,95",
        "Ue 90x40x12x0,95mm",
    )

    def test_all_spellings_of_the_same_profile_agree(self):
        for text in self.LIPPED_90:
            profile = m.steel_profile_from_text(text)
            self.assertIsNotNone(profile, text)
            self.assertEqual(profile.shape, m.SHAPE_LIPPED_CHANNEL, text)
            self.assertEqual(
                (profile.web_mm, profile.flange_mm, profile.lip_mm,
                 profile.thickness_mm),
                (90.0, 40.0, 12.0, 0.95), text)

    def test_portuguese_words_carry_the_shape(self):
        self.assertEqual(
            m.decode_nbr_designation("Guia 90x40x0,95").shape,
            m.SHAPE_PLAIN_CHANNEL)
        self.assertEqual(
            m.decode_nbr_designation("Montante 90x40x12x0,95").shape,
            m.SHAPE_LIPPED_CHANNEL)

    def test_web_only_names_resolve_without_inventing_thickness(self):
        for text in ("PGC 90", "PGC90", "Montante 90", "Guia 140"):
            profile = m.decode_nbr_designation(text)
            self.assertIsNotNone(profile, text)
            self.assertIsNone(profile.thickness_mm, text)
            self.assertIsNone(profile.linear_mass_kg_m, text)

    def test_montante_is_disambiguated_between_timber_and_steel(self):
        # "Montante" is Portuguese for "stud" in BOTH systems. The pair
        # order decides: steel is web-first and web-deeper (90x40),
        # timber is thickness-first and thinner than deep (38x90).
        self.assertIsNone(m.steel_profile_from_text("Montante 38x90mm"))
        width_in, depth_in = m.actual_dims_from_text("Montante 38x90mm")
        self.assertAlmostEqual(width_in * 25.4, 38.0, places=6)
        self.assertAlmostEqual(depth_in * 25.4, 90.0, places=6)

        steel = m.steel_profile_from_text("Montante 90x40x12x0,95")
        self.assertIsNotNone(steel)
        self.assertEqual(steel.web_mm, 90.0)

    def test_timber_ordered_pairs_are_never_claimed_as_steel(self):
        for text in ("Montante 38x140", "Montante 45x190", "Guia 38x90"):
            self.assertIsNone(m.steel_profile_from_text(text), text)

    def test_a_bare_wood_size_is_untouched_by_the_shape_words(self):
        self.assertEqual(m.actual_dims_from_text("2x6"), (1.5, 5.5))


class Nbr15253RangeTests(unittest.TestCase):
    def test_thickness_range_constants_bracket_the_standard(self):
        self.assertEqual(m.NBR15253_MIN_STRUCTURAL_THICKNESS_MM, 0.80)
        self.assertEqual(m.NBR15253_MAX_STRUCTURAL_THICKNESS_MM, 3.00)
        self.assertLess(m.NBR15253_MIN_STRUCTURAL_THICKNESS_MM,
                        m.NBR15253_MAX_STRUCTURAL_THICKNESS_MM)

    def test_standard_web_depths_are_the_documented_ladder(self):
        self.assertEqual(m.NBR15253_STANDARD_WEB_DEPTHS_MM, (90.0, 140.0, 200.0))

    def test_standard_webs_all_decode(self):
        for web in m.NBR15253_STANDARD_WEB_DEPTHS_MM:
            text = "Ue {0:.0f}x40x12x0,95".format(web)
            self.assertEqual(m.decode_nbr_designation(text).web_mm, web, text)

    def test_zinc_coating_minimum_is_z275(self):
        self.assertEqual(m.NBR15253_MIN_ZINC_COATING_G_M2, 275.0)


class LinearMassTests(unittest.TestCase):
    """Mass is what a Brazilian steel take-off is priced on.

    These reference values come from published Brazilian LSF profile
    tables; agreement to well under 1% is what makes the derived take-off
    trustworthy. Corner radii make the real developed width a hair shorter
    than the sharp-corner sum, which is the entire residual error.
    """

    PUBLISHED_KG_PER_M = (
        ("Ue 90x40x12x0,80", 1.22),
        ("Ue 90x40x12x0,95", 1.45),
        ("Ue 140x40x12x1,25", 2.39),
        ("Ue 200x40x12x1,25", 2.98),
        ("U 90x40x0,95", 1.27),
    )

    def test_matches_published_profile_tables(self):
        for designation, published in self.PUBLISHED_KG_PER_M:
            calculated = m.linear_mass_kg_m_from_text(designation)
            self.assertIsNotNone(calculated, designation)
            error_pct = abs(calculated - published) / published * 100.0
            self.assertLess(
                error_pct, 1.0,
                "{0}: calculated {1:.3f} kg/m vs published {2:.2f} kg/m "
                "({3:.2f}% off)".format(designation, calculated, published, error_pct),
            )

    def test_developed_width_includes_lips_only_when_lipped(self):
        lipped = m.decode_nbr_designation("Ue 90x40x12x0,95")
        plain = m.decode_nbr_designation("U 90x40x0,95")
        self.assertEqual(lipped.developed_width_mm, 90 + 2 * 40 + 2 * 12)
        self.assertEqual(plain.developed_width_mm, 90 + 2 * 40)

    def test_mass_is_none_when_thickness_unknown(self):
        self.assertIsNone(m.linear_mass_kg_m_from_text("PGC 90"))

    def test_mass_is_none_for_unrecognized_text(self):
        self.assertIsNone(m.linear_mass_kg_m_from_text("Generic Framing"))

    def test_mass_scales_linearly_with_thickness(self):
        thin = m.linear_mass_kg_m_from_text("Ue 90x40x12x0,80")
        thick = m.linear_mass_kg_m_from_text("Ue 90x40x12x1,60")
        self.assertAlmostEqual(thick / thin, 2.0, places=6)


class SsmaProfileTests(unittest.TestCase):
    def test_ssma_profile_now_carries_thickness(self):
        # The gauge was parsed by the regex and then discarded, which made
        # any weight take-off impossible for SSMA profiles.
        profile = m.decode_ssma_profile("350S162-33")
        self.assertAlmostEqual(profile.thickness_mm, 0.033 * 25.4, places=6)
        self.assertIsNotNone(profile.linear_mass_kg_m)

    def test_track_decodes_as_plain_channel(self):
        self.assertEqual(
            m.decode_ssma_profile("350T125-33").shape, m.SHAPE_PLAIN_CHANNEL
        )

    def test_stud_decodes_as_lipped_channel(self):
        self.assertEqual(
            m.decode_ssma_profile("350S162-33").shape, m.SHAPE_LIPPED_CHANNEL
        )

    def test_dims_agree_with_the_legacy_tuple_decoder(self):
        profile = m.decode_ssma_profile("600S162-54")
        legacy_width_in, legacy_depth_in = m.decode_ssma_designation("600S162-54")
        self.assertAlmostEqual(profile.web_mm / 25.4, legacy_depth_in, places=6)
        self.assertAlmostEqual(profile.flange_mm / 25.4, legacy_width_in, places=6)

    def test_non_designation_returns_none(self):
        self.assertIsNone(m.decode_ssma_profile("Generic Framing"))


class Nbr15253ComplianceTests(unittest.TestCase):
    def test_thickness_at_the_minimum_is_compliant(self):
        profile = m.decode_nbr_designation("Ue 90x40x12x0,80")
        self.assertTrue(profile.meets_nbr15253_structural_thickness)

    def test_thickness_below_the_minimum_is_flagged(self):
        profile = m.decode_nbr_designation("Ue 90x40x12x0,50")
        self.assertFalse(profile.meets_nbr15253_structural_thickness)

    def test_unknown_thickness_is_undetermined_not_false(self):
        profile = m.decode_nbr_designation("PGC 90")
        self.assertIsNone(profile.meets_nbr15253_structural_thickness)

    def test_check_reports_only_genuinely_thin_profiles(self):
        warnings = m.check_nbr15253_compliance([
            ("Montante", "Ue 90x40x12x0,50"),
            ("Guia", "U 90x40x0,95"),
        ])
        self.assertEqual(len(warnings), 1)
        self.assertIn("Montante", warnings[0])

    def test_unknown_thickness_is_not_reported_as_non_compliant(self):
        # An undeterminable thickness is not evidence of a violation;
        # warning on it would train users to ignore the warning.
        self.assertEqual(m.check_nbr15253_compliance([("Montante", "PGC 90")]), [])

    def test_duplicate_types_reported_once(self):
        warnings = m.check_nbr15253_compliance([
            ("Montante", "Ue 90x40x12x0,50"),
            ("Montante", "Ue 90x40x12x0,50"),
        ])
        self.assertEqual(len(warnings), 1)

    def test_clean_selection_yields_no_warnings(self):
        self.assertEqual(m.check_nbr15253_compliance([
            ("Montante", "Ue 90x40x12x0,95"),
            ("Guia", "U 90x40x0,95"),
        ]), [])

    def test_handles_empty_and_none_input(self):
        self.assertEqual(m.check_nbr15253_compliance([]), [])
        self.assertEqual(m.check_nbr15253_compliance(None), [])

    def test_warn_never_raises_outside_revit(self):
        try:
            m.warn_nbr15253_compliance([("Montante", "Ue 90x40x12x0,50")])
        except Exception as exc:  # pragma: no cover - failure path
            self.fail("warn_nbr15253_compliance raised: {0!r}".format(exc))


class MetricLumberTests(unittest.TestCase):
    """Brazilian Wood Frame sections, named by actual size in millimeters."""

    BRAZILIAN_SECTIONS = (
        ("38x90", 38.0, 90.0),
        ("38x140", 38.0, 140.0),
        ("38x190", 38.0, 190.0),
        ("38x240", 38.0, 240.0),
        ("45x90", 45.0, 90.0),
        ("45x140", 45.0, 140.0),
    )

    def test_standard_sections_decode_to_actual_millimeters(self):
        for text, thickness_mm, depth_mm in self.BRAZILIAN_SECTIONS:
            width_in, depth_in = m.decode_metric_lumber(text)
            self.assertAlmostEqual(width_in * 25.4, thickness_mm, places=6, msg=text)
            self.assertAlmostEqual(depth_in * 25.4, depth_mm, places=6, msg=text)

    def test_resolves_through_actual_dims_from_text(self):
        width_in, depth_in = m.actual_dims_from_text("Montante 38x90mm")
        self.assertAlmostEqual(width_in * 25.4, 38.0, places=6)
        self.assertAlmostEqual(depth_in * 25.4, 90.0, places=6)

    def test_tolerates_spaces_around_separator(self):
        self.assertIsNotNone(m.decode_metric_lumber("Pinus 38 x 140"))

    def test_steel_designations_are_never_read_as_lumber(self):
        # The ordering guarantee in actual_dims_from_text: a designated
        # steel profile must resolve as steel, not as a 90x40 timber.
        for designation in ("Ue 90x40x12x0,95", "U 90x40x0,95", "PGC 90"):
            self.assertIsNotNone(m.steel_profile_from_text(designation), designation)
            expected = m.steel_profile_from_text(designation).width_depth_in()
            self.assertEqual(m.actual_dims_from_text(designation), expected, designation)

    def test_imperial_steel_section_names_are_rejected(self):
        # These carry digit pairs that would look like a millimeter section
        # if the dimensional guards were not applied.
        for text in ("HSS2X2X1/4", "C12x20.7", "W12X26", "L2x2x1/4"):
            self.assertIsNone(m.decode_metric_lumber(text), text)

    def test_us_nominal_lumber_still_wins_for_2x_names(self):
        self.assertEqual(m.actual_dims_from_text("2x6"), (1.5, 5.5))
        self.assertEqual(m.actual_dims_from_text("2x10"), (1.5, 9.25))

    def test_rejects_sections_outside_structural_range(self):
        self.assertIsNone(m.decode_metric_lumber("10x20"))     # far too small
        self.assertIsNone(m.decode_metric_lumber("500x900"))   # far too large

    def test_rejects_thickness_greater_than_depth(self):
        # Lumber is never named deeper-face-first.
        self.assertIsNone(m.decode_metric_lumber("140x38"))

    def test_empty_and_none_return_none(self):
        for text in ("", None, "Generic Framing"):
            self.assertIsNone(m.decode_metric_lumber(text), repr(text))

    def test_documented_size_ladder_is_self_consistent(self):
        for thickness in m.WOOD_METRIC_THICKNESSES_MM:
            for depth in m.WOOD_METRIC_DEPTHS_MM:
                text = "{0:.0f}x{1:.0f}".format(thickness, depth)
                self.assertIsNotNone(m.decode_metric_lumber(text), text)


class ConfigProfileLabelTests(unittest.TestCase):
    class _Config(object):
        stud_family_name = "LSF"
        stud_type_name = "Ue 90x40x12x0,95"
        bottom_plate_family_name = "LSF"
        bottom_plate_type_name = "U 90x40x0,95"
        top_plate_family_name = None
        top_plate_type_name = None
        header_family_name = None
        header_type_name = None

    def test_extracts_only_populated_selections(self):
        pairs = m.config_profile_labels(self._Config())
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0][1], "LSF Ue 90x40x12x0,95")
        self.assertEqual(pairs[1][1], "LSF U 90x40x0,95")

    def test_result_feeds_the_compliance_check(self):
        self.assertEqual(
            m.check_nbr15253_compliance(m.config_profile_labels(self._Config())), []
        )


if __name__ == "__main__":
    unittest.main()
