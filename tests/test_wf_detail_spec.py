# -*- coding: utf-8 -*-
"""Tests for lib/wf_detail_spec.py.

The spec module tells the user what the engines will draw, so its value
depends entirely on staying true to them. Two failure modes matter:

  - naming a config field that does not exist, so an option silently reads
    as its default and the preview describes framing nobody asked for;
  - promising members a tool never creates.

The tests below pin both: every config field the spec reads is asserted to
exist on the real FramingConfig, and the tool-specific claims are checked
against what each engine actually supports.
"""

import os
import sys
import unittest

_LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import wf_detail_spec as spec  # noqa: E402
from wf_config import FramingConfig  # noqa: E402
from wf_materials import MATERIAL_STEEL, MATERIAL_WOOD  # noqa: E402


def _wall_config(**overrides):
    config = FramingConfig()
    config.stud_spacing = 16.0
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class ConfigFieldsExistTests(unittest.TestCase):
    """Guards against the spec describing a field the config never had.

    This is the exact defect class that made the roof "Include Diagonal
    Kickers" checkbox inert: the engine read a field name the config did
    not define, so getattr silently returned the default forever.
    """

    READ_FIELDS = (
        "framing_material",
        "stud_spacing",
        "bottom_plate_count",
        "top_plate_count",
        "include_mid_plates",
        "mid_plate_interval_ft",
        "header_count",
        "include_king_studs",
        "include_jack_studs",
        "include_cripple_studs",
        "corner_style",
        "t_intersection_style",
        "ceiling_direction_mode",
        "ceiling_placement_mode",
        "ceiling_layout_mode",
        "include_collar_ties",
        "include_ceiling_joists",
        "include_roof_kickers",
        "include_king_posts",
    )

    def test_every_field_the_spec_reads_exists_on_the_config(self):
        config = FramingConfig()
        for field in self.READ_FIELDS:
            self.assertTrue(
                hasattr(config, field),
                "wf_detail_spec reads '{0}', which FramingConfig does not "
                "define -- the option would silently do nothing".format(field),
            )


class WallSpecTests(unittest.TestCase):
    def test_lists_studs_plates_and_openings(self):
        included, _off, _never = spec.describe(spec.HOST_WALL, _wall_config())
        joined = " ".join(included).lower()
        for expected in ("stud", "plate", "header", "corner", "t intersection"):
            self.assertIn(expected, joined)

    def test_spacing_is_shown_in_both_inches_and_millimeters(self):
        included, _off, _never = spec.describe(
            spec.HOST_WALL, _wall_config(stud_spacing=400.0 / 25.4))
        self.assertIn("400 mm", included[0])

    def test_blocking_moves_between_drawn_and_switched_off(self):
        on, off, _ = spec.describe(spec.HOST_WALL,
                                   _wall_config(include_mid_plates=True))
        self.assertTrue(any("Horizontal" in i for i in on))
        self.assertFalse(any("Horizontal" in i for i in off))

        on, off, _ = spec.describe(spec.HOST_WALL,
                                   _wall_config(include_mid_plates=False))
        self.assertFalse(any("Horizontal" in i for i in on))
        self.assertTrue(any("Horizontal" in i for i in off))

    def test_optional_studs_move_between_sections(self):
        for field, needle in (("include_king_studs", "King studs"),
                              ("include_jack_studs", "Jack studs"),
                              ("include_cripple_studs", "Cripple studs")):
            on, _off, _ = spec.describe(spec.HOST_WALL,
                                        _wall_config(**{field: True}))
            self.assertTrue(any(needle in i for i in on), field)

            _on, off, _ = spec.describe(spec.HOST_WALL,
                                        _wall_config(**{field: False}))
            self.assertTrue(any(needle in i for i in off), field)

    def test_absent_jack_studs_raise_a_structural_warning(self):
        _on, off, _never = spec.describe(
            spec.HOST_WALL, _wall_config(include_jack_studs=False))
        self.assertTrue(any("bear on" in i for i in off))

    def test_wall_diagonal_bracing_is_declared_absent(self):
        # The single most important statement in the module: no wall tool
        # generates diagonal bracing, in either material.
        for material in (MATERIAL_WOOD, MATERIAL_STEEL):
            _on, _off, never = spec.describe(
                spec.HOST_WALL, _wall_config(framing_material=material))
            self.assertTrue(
                any("iagonal bracing" in i for i in never),
                "missing diagonal bracing disclosure for " + material)

    def test_plate_count_is_reported(self):
        included, _off, _never = spec.describe(
            spec.HOST_WALL, _wall_config(top_plate_count=1))
        self.assertTrue(any("1 top" in i for i in included))

    def test_material_changes_the_member_vocabulary(self):
        wood, _, _ = spec.describe(spec.HOST_WALL,
                                   _wall_config(framing_material=MATERIAL_WOOD))
        steel, _, _ = spec.describe(spec.HOST_WALL,
                                    _wall_config(framing_material=MATERIAL_STEEL))
        self.assertTrue(any("plate" in i for i in wood))
        self.assertTrue(any("track" in i for i in steel))

    def test_plate_terms_are_pluralised_without_breaking_the_gloss(self):
        included, _, _ = spec.describe(
            spec.HOST_WALL,
            _wall_config(framing_material=MATERIAL_STEEL, top_plate_count=2))
        line = [i for i in included if "top" in i][0]
        self.assertIn("tracks (guia)", line)


class RoofSpecTests(unittest.TestCase):
    def test_truss_mode_describes_web_bracing(self):
        included, _off, _never = spec.describe(
            spec.HOST_ROOF, _wall_config(), mode=spec.ROOF_MODE_TRUSS)
        self.assertTrue(any("web bracing" in i for i in included))

    def test_stick_mode_describes_rafters_and_ridge(self):
        included, _off, _never = spec.describe(
            spec.HOST_ROOF, _wall_config(), mode=spec.ROOF_MODE_STICK)
        joined = " ".join(included).lower()
        self.assertIn("rafters", joined)
        self.assertIn("ridge", joined)

    def test_truss_mode_does_not_promise_the_stick_only_package(self):
        # calculate_members() branches to a separate truss path that never
        # reaches collar ties, ceiling joists, kickers or king posts.
        included, off, never = spec.describe(
            spec.HOST_ROOF,
            _wall_config(include_collar_ties=True, include_roof_kickers=True),
            mode=spec.ROOF_MODE_TRUSS)
        for banned in ("Collar ties", "kickers", "king posts"):
            self.assertFalse(
                any(banned in i for i in included),
                "truss mode must not claim to draw " + banned)
        self.assertTrue(any("stick-framing package" in i for i in never))

    def test_stick_mode_honours_the_kicker_option(self):
        on, _off, _ = spec.describe(
            spec.HOST_ROOF, _wall_config(include_roof_kickers=True),
            mode=spec.ROOF_MODE_STICK)
        self.assertTrue(any("kickers" in i for i in on))

        _on, off, _ = spec.describe(
            spec.HOST_ROOF, _wall_config(include_roof_kickers=False),
            mode=spec.ROOF_MODE_STICK)
        self.assertTrue(any("kickers" in i for i in off))


class FloorCeilingSpecTests(unittest.TestCase):
    def test_floor_declares_unframed_openings(self):
        _on, _off, never = spec.describe(spec.HOST_FLOOR, _wall_config())
        self.assertTrue(any("penetrations" in i for i in never))

    def test_floor_does_not_mention_wall_only_members(self):
        included, _off, _never = spec.describe(spec.HOST_FLOOR, _wall_config())
        joined = " ".join(included).lower()
        for wall_only in ("king stud", "header", "corner"):
            self.assertNotIn(wall_only, joined)

    def test_ceiling_reports_the_selected_direction(self):
        included, _, _ = spec.describe(
            spec.HOST_CEILING, _wall_config(ceiling_direction_mode="both"))
        self.assertTrue(any("BOTH" in i for i in included))


class RenderingTests(unittest.TestCase):
    def test_text_rendering_has_all_three_sections(self):
        text = spec.describe_text(
            spec.HOST_WALL, _wall_config(include_mid_plates=False))
        self.assertIn("WILL BE DRAWN:", text)
        self.assertIn("SWITCHED OFF:", text)
        self.assertIn("NOT GENERATED BY THIS TOOL:", text)

    def test_markdown_rendering_is_non_empty(self):
        markdown = spec.describe_markdown(spec.HOST_WALL, _wall_config())
        self.assertIn("**Drawn:**", markdown)

    def test_none_config_is_handled(self):
        self.assertEqual(spec.describe(spec.HOST_WALL, None), ([], [], []))
        self.assertEqual(spec.describe_text(spec.HOST_WALL, None), "")

    def test_unknown_host_kind_is_handled(self):
        self.assertEqual(spec.describe("nonsense", _wall_config()), ([], [], []))

    def test_missing_spacing_does_not_crash(self):
        config = FramingConfig()
        config.stud_spacing = 0
        included, _off, _never = spec.describe(spec.HOST_WALL, config)
        self.assertIn("spacing not set", included[0])


if __name__ == "__main__":
    unittest.main()
