# -*- coding: utf-8 -*-
"""Shared framing-material catalogs and dimension resolution.

Single source of truth for wood dimensional-lumber and cold-formed steel
(CFS) member sizing, used by the wall/floor/ceiling/roof engines so that
geometry math (spacing, plate/track stacking, collision tolerances) and
fallback dimension lookups adapt correctly regardless of which material
the user selects. Family/type *selection* itself was already
material-agnostic (any loaded Revit structural framing/column family can
be picked) -- this module only supplies the sizing knowledge the engines
need when they can't read real geometry off the chosen family/type.
"""

import re


MATERIAL_WOOD = "wood"
MATERIAL_STEEL = "steel"
MATERIALS = (MATERIAL_WOOD, MATERIAL_STEEL)

# Actual dimensions in INCHES: (width, depth) -- matches the (b, d)
# convention used by Revit's structural framing profile parameters.
LUMBER_ACTUAL = {
    "2x2": (1.5, 1.5),
    "2x3": (1.5, 2.5),
    "2x4": (1.5, 3.5),
    "2x6": (1.5, 5.5),
    "2x8": (1.5, 7.25),
    "2x10": (1.5, 9.25),
    "2x12": (1.5, 11.25),
}

# Cold-formed steel stud/track actual dimensions in INCHES, keyed by the
# standard SSMA member designation "<depth><S|T><flange>-<mil>", e.g.
# "350S162-33" = 3-1/2" deep punched stud, 1-5/8" flange, 33 mil (~20 ga).
# Stored as (flange_width_in, web_depth_in) to line up with LUMBER_ACTUAL's
# (width, depth) convention.
STEEL_STUD_ACTUAL = {
    "250S162-33": (1.625, 2.5),
    "250S162-43": (1.625, 2.5),
    "350S162-33": (1.625, 3.5),
    "350S162-43": (1.625, 3.5),
    "350S162-54": (1.625, 3.5),
    "400S162-33": (1.625, 4.0),
    "400S162-43": (1.625, 4.0),
    "600S162-33": (1.625, 6.0),
    "600S162-43": (1.625, 6.0),
    "600S162-54": (1.625, 6.0),
    "600S200-54": (2.0, 6.0),
    "600S200-68": (2.0, 6.0),
    "800S162-43": (1.625, 8.0),
    "800S200-54": (2.0, 8.0),
    "800S200-68": (2.0, 8.0),
    "1000S200-54": (2.0, 10.0),
    "1200S200-68": (2.0, 12.0),
}

STEEL_TRACK_ACTUAL = {
    "250T125-33": (1.25, 2.5),
    "350T125-33": (1.25, 3.5),
    "350T125-43": (1.25, 3.5),
    "350T125-54": (1.25, 3.5),
    "400T125-33": (1.25, 4.0),
    "600T125-33": (1.25, 6.0),
    "600T125-43": (1.25, 6.0),
    "600T200-54": (2.0, 6.0),
    "800T125-43": (1.25, 8.0),
    "800T200-54": (2.0, 8.0),
    "1000T125-54": (1.25, 10.0),
    "1200T200-68": (2.0, 12.0),
}

STEEL_ACTUAL = {}
STEEL_ACTUAL.update(STEEL_STUD_ACTUAL)
STEEL_ACTUAL.update(STEEL_TRACK_ACTUAL)

# Note on joists/rafters: SSMA's "S" shape letter denotes the C-shape
# cross-section itself, independent of whether the member is used as a wall
# stud, floor joist, or rafter -- a structural C-joist named e.g.
# "950S250-97" (9-1/2" deep, 2.5" flange, 97 mil) decodes correctly through
# STEEL_ACTUAL / decode_ssma_designation() below with no separate joist
# table needed. There is no single universal designation format for
# manufacturer-specific joist catalogs (depths like 9-1/2" and 11-7/8"
# don't reduce to a clean 3-4 digit hundredths code), so no attempt is made
# to guess those -- callers fall back to get_material_defaults() instead.

# Standard on-center spacing options, in inches. All four framing tools'
# dialogs offer 12"/16"/24" O.C. regardless of material -- 12" O.C. is
# common for steel but not exclusive to it (heavier wood loads use it too)
# and 16"/24" are common to both materials, so there's no material-specific
# subset to encode here; these are just the shared named constants.
SPACING_12OC = 12.0
SPACING_16OC = 16.0
SPACING_24OC = 24.0

# Broadened structural-framing profile parameter names to try, in order,
# when reading a member's real cross-section off its Revit FamilySymbol.
# Covers the stock Structural Framing family template ("b"/"d") as well as
# common alternate names seen on steel/CFS and custom families.
DEPTH_PARAM_NAMES = ("d", "Depth", "Nominal Depth", "Web Depth", "Height")
WIDTH_PARAM_NAMES = ("b", "Width", "Nominal Width", "Flange Width")

_STEEL_DESIGNATION_RE = re.compile(r"\b(\d{3,4})([STUF])(\d{3})-(\d{2,3})\b", re.IGNORECASE)
_WOOD_NOMINAL_RE = re.compile(r"\b2x(2|3|4|6|8|10|12)\b", re.IGNORECASE)


def decode_ssma_designation(token):
    """Decode a standard SSMA member designation into actual dimensions.

    Format: <depth in 1/100 in><S|T|U|F><flange in 1/100 in>-<mil thickness>
    e.g. "362S162-33" -> depth=3.625", flange=1.625". This lets any valid
    SSMA-style designation resolve correctly even if it isn't in the
    STEEL_ACTUAL catalog above (custom gauges/depths).

    Returns (width_in, depth_in) or None if the token doesn't parse.

    Note: despite the "hundredths of an inch" convention, several standard
    SSMA codes are traditional roundings rather than literal x100 values --
    e.g. "162" means 1-5/8" (1.625"), not 1.62", and "362" means 3-5/8"
    (3.625"), not 3.62". Every standard CFS profile is dimensioned in
    1/8" increments, so the raw x/100 value is snapped to the nearest
    eighth to recover the true dimension instead of a slightly-short one.
    """
    match = _STEEL_DESIGNATION_RE.match((token or "").strip())
    if not match:
        return None
    depth_hundredths = int(match.group(1))
    flange_hundredths = int(match.group(3))
    if depth_hundredths <= 0 or flange_hundredths <= 0:
        return None
    return (
        _snap_to_eighth_inch(flange_hundredths / 100.0),
        _snap_to_eighth_inch(depth_hundredths / 100.0),
    )


def _snap_to_eighth_inch(value_in):
    """Round a decimal-inch value to the nearest 1/8" increment."""
    return round(value_in * 8.0) / 8.0


def actual_dims_from_text(text):
    """Look up (width_in, depth_in) by matching a nominal size embedded in
    free text (typically a "FamilyName TypeName" string).

    Tries steel SSMA designations first (exact catalog hit, then generic
    decode), then wood nominal dimensional-lumber sizes. Returns None if
    nothing matches either convention.
    """
    text = text or ""

    match = _STEEL_DESIGNATION_RE.search(text)
    if match:
        token = match.group(0).upper()
        if token in STEEL_ACTUAL:
            return STEEL_ACTUAL[token]
        decoded = decode_ssma_designation(token)
        if decoded is not None:
            return decoded

    # Word-boundary-anchored match only -- deliberately NOT a loose
    # substring containment check. An unanchored "is '2x2' in text" test
    # false-positives on real steel section names that happen to contain
    # the digits in sequence without being wood at all, e.g. "HSS2X2X1/4"
    # (a 2"x2" steel tube), "L2x2x1/4" (a steel angle), or "C12x20.7" (a
    # C-channel matches via "...12x20...") -- all of those would otherwise
    # silently resolve to wood's 1.5"x1.5" actual dimensions.
    wood_match = _WOOD_NOMINAL_RE.search(text)
    if wood_match:
        nominal = "2x{0}".format(wood_match.group(1))
        return LUMBER_ACTUAL.get(nominal)

    return None


def get_material_defaults(material):
    """Fallback geometry defaults (in INCHES) for a material, used only
    when no real Revit family/type dimension can be resolved at all.

    Keys:
        stud_width_in       -- member thickness used for spacing/collision
                                math and plate/track stacking (the "b"
                                dimension).
        stud_depth_in       -- default member depth (the "d" dimension).
        header_depth_in     -- default header/beam depth.
        header_ply_spacer_in -- gap left between built-up header plies.
                                Wood commonly sandwiches a 1/2" plywood
                                shim between doubled solid-sawn header
                                plies to build the pair out to the wall's
                                stud depth. Built-up steel headers (back-
                                to-back or boxed C-shapes) are normally
                                fastened directly together with no shim,
                                so this is 0 for steel.
    """
    if material == MATERIAL_STEEL:
        return {
            "stud_width_in": 1.625,
            "stud_depth_in": 3.5,
            "header_depth_in": 6.0,
            "header_ply_spacer_in": 0.0,
        }
    return {
        "stud_width_in": 1.5,
        "stud_depth_in": 3.5,
        "header_depth_in": 3.5,
        "header_ply_spacer_in": 0.5,
    }


def fallback_depth_ft(material, kind="stud"):
    """Material-aware last-resort member depth, in FEET.

    kind: "stud" (or "joist"/"rafter") uses stud_depth_in; "header" uses
    header_depth_in.
    """
    defaults = get_material_defaults(material)
    key = "header_depth_in" if kind == "header" else "stud_depth_in"
    return defaults[key] / 12.0


def fallback_width_ft(material):
    """Material-aware last-resort member width/thickness, in FEET."""
    return get_material_defaults(material)["stud_width_in"] / 12.0


def header_ply_spacer_ft(material):
    """Gap left between built-up header plies for a material, in FEET."""
    return get_material_defaults(material)["header_ply_spacer_in"] / 12.0
