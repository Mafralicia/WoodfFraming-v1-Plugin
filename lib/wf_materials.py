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

# Common structural C-shape steel joist/rafter sizes (depth x flange x mil),
# used only to seed sensible defaults -- actual placement always prefers the
# real dimensions of whatever family/type the user selects in the project.
STEEL_JOIST_ACTUAL = {
    "6CS162-43": (1.625, 6.0),
    "8CS162-43": (1.625, 8.0),
    "9.5CS250-54": (2.5, 9.5),
    "9.5CS250-68": (2.5, 9.5),
    "11.5CS250-68": (2.5, 11.5),
    "11.5CS250-97": (2.5, 11.5),
}

STEEL_STUD_DEFAULT = "350S162-33"
STEEL_TRACK_DEFAULT = "350T125-33"
STEEL_JOIST_DEFAULT = "9.5CS250-54"

# Standard on-center spacing options, in inches, by material.
SPACING_12OC = 12.0
SPACING_16OC = 16.0
SPACING_24OC = 24.0

SPACING_OPTIONS_IN = {
    MATERIAL_WOOD: (SPACING_16OC, SPACING_24OC),
    MATERIAL_STEEL: (SPACING_12OC, SPACING_16OC, SPACING_24OC),
}

# Broadened structural-framing profile parameter names to try, in order,
# when reading a member's real cross-section off its Revit FamilySymbol.
# Covers the stock Structural Framing family template ("b"/"d") as well as
# common alternate names seen on steel/CFS and custom families.
DEPTH_PARAM_NAMES = ("d", "Depth", "Nominal Depth", "Web Depth", "Height")
WIDTH_PARAM_NAMES = ("b", "Width", "Nominal Width", "Flange Width")

_STEEL_DESIGNATION_RE = re.compile(r"\b(\d{3,4})([STUF])(\d{3})-(\d{2,3})\b", re.IGNORECASE)
_WOOD_NOMINAL_RE = re.compile(r"\b2x(2|3|4|6|8|10|12)\b", re.IGNORECASE)


def looks_like_steel(text):
    """Heuristic: does a family/type name look like an SSMA steel designation?"""
    return bool(_STEEL_DESIGNATION_RE.search(text or ""))


def looks_like_wood(text):
    """Heuristic: does a family/type name look like nominal dimensional lumber?"""
    return bool(_WOOD_NOMINAL_RE.search(text or "")) or any(
        nominal.lower() in (text or "").lower() for nominal in LUMBER_ACTUAL
    )


def guess_material(text):
    """Best-effort guess of material from a family/type name string."""
    if looks_like_steel(text):
        return MATERIAL_STEEL
    return MATERIAL_WOOD


def decode_ssma_designation(token):
    """Decode a standard SSMA member designation into actual dimensions.

    Format: <depth in 1/100 in><S|T|U|F><flange in 1/100 in>-<mil thickness>
    e.g. "362S162-33" -> depth=3.625", flange=1.625". This lets any valid
    SSMA-style designation resolve correctly even if it isn't in the
    STEEL_ACTUAL catalog above (custom gauges/depths).

    Returns (width_in, depth_in) or None if the token doesn't parse.
    """
    match = _STEEL_DESIGNATION_RE.match((token or "").strip())
    if not match:
        return None
    depth_hundredths = int(match.group(1))
    flange_hundredths = int(match.group(3))
    if depth_hundredths <= 0 or flange_hundredths <= 0:
        return None
    return (flange_hundredths / 100.0, depth_hundredths / 100.0)


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

    lowered = text.lower()
    for nominal, dims in LUMBER_ACTUAL.items():
        if nominal.lower() in lowered:
            return dims

    wood_match = _WOOD_NOMINAL_RE.search(text)
    if wood_match:
        nominal = "2x{0}".format(wood_match.group(1))
        return LUMBER_ACTUAL.get(nominal)

    return None


def get_material_defaults(material):
    """Fallback geometry defaults (in INCHES) for a material, used only
    when no real Revit family/type dimension can be resolved at all.

    Keys:
        stud_width_in   -- member thickness used for spacing/collision math
                            and plate/track stacking (the "b" dimension).
        stud_depth_in   -- default member depth (the "d" dimension).
        header_depth_in -- default header/beam depth.
    """
    if material == MATERIAL_STEEL:
        return {
            "stud_width_in": 1.625,
            "stud_depth_in": 3.5,
            "header_depth_in": 6.0,
        }
    return {
        "stud_width_in": 1.5,
        "stud_depth_in": 3.5,
        "header_depth_in": 3.5,
    }


def spacing_options_in(material):
    """Standard on-center spacing choices (inches) offered for a material."""
    return SPACING_OPTIONS_IN.get(material, SPACING_OPTIONS_IN[MATERIAL_WOOD])


def default_spacing_in(material):
    return SPACING_16OC


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
