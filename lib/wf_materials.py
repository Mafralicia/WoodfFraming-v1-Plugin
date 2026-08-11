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

# Metric on-center spacing options, in MILLIMETERS. 400mm and 600mm are the
# conventional stud spacings for Brazilian Light Steel Framing (NBR 15253 /
# NBR 15980) and are common in metric-unit steel framing generally. These
# are plain numeric presets, not a name-matching scheme -- they just save
# a metric-project user from typing the inch-equivalent (15.748", 23.622")
# into the existing custom-spacing field.
SPACING_400MM = 400.0
SPACING_600MM = 600.0
MM_PER_INCH = 25.4


def mm_to_spacing_in(mm):
    """Convert a millimeter spacing value to the inches unit stud_spacing
    is stored in (Revit internal length is unit-agnostic feet, but this
    codebase's FramingConfig.stud_spacing field is always inches)."""
    return mm / MM_PER_INCH


# Broadened structural-framing profile parameter names to try, in order,
# when reading a member's real cross-section off its Revit FamilySymbol.
# Covers the stock Structural Framing family template ("b"/"d") -- which
# keeps those literal parameter names regardless of the Revit UI language,
# including Brazilian-Portuguese Revit installs -- plus common alternate
# names seen on steel/CFS and custom families, including a few Portuguese
# labels for families authored with renamed/custom parameters instead of
# the stock template.
DEPTH_PARAM_NAMES = (
    "d", "Depth", "Nominal Depth", "Web Depth", "Height",
    "Profundidade", "Altura",
)
WIDTH_PARAM_NAMES = (
    "b", "Width", "Nominal Width", "Flange Width",
    "Largura",
)

_STEEL_DESIGNATION_RE = re.compile(r"\b(\d{3,4})([STUF])(\d{3})-(\d{2,3})\b", re.IGNORECASE)
_WOOD_NOMINAL_RE = re.compile(r"\b2x(2|3|4|6|8|10|12)\b", re.IGNORECASE)

# Brazilian Wood Frame lumber, in MILLIMETERS. Unlike the North American
# "2x" convention -- where the nominal name and the actual milled size
# differ (a "2x4" is really 1-1/2" x 3-1/2") -- Brazilian sections are
# named by their ACTUAL dimensions, so "38x90" really is 38 mm x 90 mm and
# needs no nominal-to-actual translation. 38 mm and 45 mm are the two
# structural thicknesses in common use; the depths below are the standard
# ladder. Sizes are matched generically by the regex, so a section outside
# this list still resolves -- the constants exist to document the range.
WOOD_METRIC_THICKNESSES_MM = (38.0, 45.0)
WOOD_METRIC_DEPTHS_MM = (90.0, 140.0, 190.0, 240.0)

# Guard rails: a plausible structural lumber section, used to reject
# coincidental digit pairs in a family name (a "2x4" must not be read as
# 2 mm x 4 mm, and a year or a code must not be read as a section).
_WOOD_METRIC_MIN_MM = 20.0
_WOOD_METRIC_MAX_MM = 400.0

_WOOD_METRIC_RE = re.compile(
    r"\b(\d{2,3}(?:[.,]\d)?)\s*[x×X]\s*(\d{2,3}(?:[.,]\d)?)\s*(?:mm\b)?",
    re.UNICODE,
)


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

    Tries ABNT/NBR designations first (Brazilian LSF profiles, in mm --
    see decode_nbr_designation), then steel SSMA designations (exact
    catalog hit, then generic decode), then wood nominal dimensional-lumber
    sizes. Returns None if nothing matches any convention.

    ABNT is tried before SSMA because the two notations cannot collide (mm
    dimensions with an explicit "U"/"Ue" prefix vs. hundredths-of-an-inch
    with an infix shape letter), and a Brazilian name must never fall
    through to an American default.
    """
    text = text or ""

    nbr_profile = decode_nbr_designation(text)
    if nbr_profile is not None:
        return nbr_profile.width_depth_in()

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

    # Brazilian Wood Frame sections ("38x90"), tried last so that a steel
    # designation is never mistaken for lumber -- see decode_metric_lumber.
    return decode_metric_lumber(text)


def decode_metric_lumber(text):
    """Decode a Brazilian Wood Frame lumber section into (width_in, depth_in).

    Brazilian sections are named by their ACTUAL milled dimensions in
    millimeters -- "38x90" is 38 mm x 90 mm -- so unlike the North American
    "2x4" convention there is no nominal-to-actual translation step.

    Returns None when the text carries no plausible lumber section.

    On ambiguity: a bare "90x40" could in principle be either a lumber
    section or a steel profile written without its "U"/"Ue" prefix. Callers
    resolve this by ordering -- actual_dims_from_text() tries the steel
    notations first, so a properly designated profile is never mistaken for
    lumber. The dimensional guards below reject the other realistic
    confusions (imperial names like "C12x20.7" or "HSS2X2X1/4", whose
    numbers fall outside any structural-lumber range in millimeters).
    """
    match = _WOOD_METRIC_RE.search(text or "")
    if not match:
        return None

    try:
        thickness_mm = float(match.group(1).replace(",", "."))
        depth_mm = float(match.group(2).replace(",", "."))
    except (TypeError, ValueError):
        return None

    # Structural lumber is never thicker than it is deep, and both
    # dimensions sit in a narrow, well-known band.
    if not (_WOOD_METRIC_MIN_MM <= thickness_mm <= 120.0):
        return None
    if not (40.0 <= depth_mm <= _WOOD_METRIC_MAX_MM):
        return None
    if thickness_mm > depth_mm:
        return None

    return (thickness_mm / MM_PER_INCH, depth_mm / MM_PER_INCH)


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


# ---------------------------------------------------------------------------
# Brazilian (ABNT/NBR) cold-formed steel profiles and mass take-off
#
# Everything above this point resolves member GEOMETRY only. This section
# adds the two things a Brazilian steel quantity take-off ("quantitativo")
# actually needs and that geometry alone cannot give you:
#
#   1. Profile designations in ABNT form. NBR 6355 (padronizacao de perfis
#      formados a frio) and NBR 15253 (perfis para painels reticulados /
#      Light Steel Framing) designate profiles as
#          Ue <alma> x <mesa> x <enrijecedor> x <espessura>   (montante)
#          U  <alma> x <mesa> x <espessura>                    (guia)
#      e.g. "Ue 90x40x12x0,95" and "U 90x40x0,95", in MILLIMETERS and with
#      the Brazilian decimal comma. None of that parses as SSMA, so before
#      this existed every Brazilian profile name silently fell through to a
#      generic American default (1-5/8" x 3-1/2"), which is simply the wrong
#      member for anything but a coincidental 90mm web.
#
#   2. Mass. Steel framing in Brazil is specified, bought and priced by
#      WEIGHT (kg), not by linear meter -- so a take-off that reports only
#      count and total length is not commercially usable. Mass is derived
#      from the flat developed width of the section (the width of the coil
#      strip the profile is roll-formed from) times the base steel
#      thickness times the steel density.
#
# The mass formula is validated in tests/ against published kg/m values
# from Brazilian LSF profile tables; it agrees to within ~0.5%.
# ---------------------------------------------------------------------------

# Density of structural steel. NBR 6355 / NBR 14762 and Brazilian profile
# tables are all built on 7850 kg/m3.
STEEL_DENSITY_KG_M3 = 7850.0

# NBR 15253 sets a minimum BASE STEEL thickness (i.e. excluding the zinc
# coating) for profiles used structurally in light steel framing panels.
# Thinner sections are non-structural (drywall furring and similar), so a
# structural model specifying one is worth flagging.
NBR15253_MIN_STRUCTURAL_THICKNESS_MM = 0.80

# The standard's upper bound. Above this a section is outside the range
# NBR 15253 covers -- it is not "extra safe", it is a different product,
# so it is worth flagging just as an under-thickness section is.
NBR15253_MAX_STRUCTURAL_THICKNESS_MM = 3.00

# Web depths the standard lists for U (guia) and Ue (montante). A section
# outside this ladder is not automatically wrong -- manufacturers offer
# intermediate depths -- so this is documentation and a soft check, never
# a rejection.
NBR15253_STANDARD_WEB_DEPTHS_MM = (90.0, 140.0, 200.0)

# Minimum zinc coating for structural LSF profiles: Z275 = 275 g/m2 over
# both faces combined. Quoted thicknesses -- and the kg/m tables built on
# them -- are BASE STEEL, excluding this coating.
NBR15253_MIN_ZINC_COATING_G_M2 = 275.0

# Section shapes, using the ABNT letters.
SHAPE_LIPPED_CHANNEL = "Ue"   # "U enrijecido" -- montante / stud / joist
SHAPE_PLAIN_CHANNEL = "U"     # simple channel -- guia / track

# Commercial Brazilian LSF naming ("PGC 90" / "PGU 90" -- Perfil Galvanizado
# tipo C / tipo U) carries only the web depth. The flange and lip are not in
# the name because they are effectively fixed across the Brazilian LSF
# market, so they are supplied here as documented ASSUMPTIONS. This only
# ever applies as a fallback: a real dimension read off the Revit family
# always wins. Thickness is deliberately NOT assumed -- see below.
PG_ASSUMED_FLANGE_MM = 40.0
PG_ASSUMED_LIP_MM = 12.0

# Nominal stiffening lip for SSMA lipped sections, in inches. SSMA's
# designation encodes web and flange but not the lip, and the C-shaped stud
# sections ("S") carry a nominal 1/2" lip. Track ("T") and plain channel
# ("U") are unstiffened.
_SSMA_NOMINAL_LIP_IN = {"S": 0.5, "F": 0.5, "T": 0.0, "U": 0.0}

_MM_DECIMAL = r"\d{1,3}(?:[.,]\d{1,2})?"
_NBR_SEP = r"\s*[x×X/]\s*"

# Shape keywords, longest-first so "Montante" is not consumed as "M" and
# "Ue" is not consumed as "U". Real family names in Brazilian projects use
# any of these, in Portuguese or in the ABNT letters, often with a
# manufacturer name in front -- so the pattern searches rather than
# anchors, and everything after the keyword is optional.
_NBR_SHAPE_WORDS = (
    ("montante", SHAPE_LIPPED_CHANNEL),
    ("guia", SHAPE_PLAIN_CHANNEL),
    ("pgc", SHAPE_LIPPED_CHANNEL),
    ("pgu", SHAPE_PLAIN_CHANNEL),
    ("ue", SHAPE_LIPPED_CHANNEL),
    ("u", SHAPE_PLAIN_CHANNEL),
)

_NBR_DESIGNATION_RE = re.compile(
    r"\b(Montante|Guia|PGC|PGU|Ue|U)\s*"
    r"({0})"
    r"(?:{1}({0}))?"
    r"(?:{1}({0}))?"
    r"(?:{1}({0}))?"
    r"\s*(?:mm)?".format(_MM_DECIMAL, _NBR_SEP),
    re.IGNORECASE | re.UNICODE,
)


def _mm(text):
    """Parse a Brazilian-or-international decimal number (mm) to float."""
    return float(str(text).replace(",", "."))


class SteelProfile(object):
    """A cold-formed steel section, dimensioned in MILLIMETERS.

    thickness_mm is the BASE STEEL thickness (espessura da chapa base,
    excluding zinc coating), which is what both NBR 6355 designations and
    published kg/m tables are built on. It may be None when the source
    designation simply does not carry it (e.g. the commercial "PGC 90"
    form) -- in that case mass is reported as None rather than guessed,
    because a fabricated weight in a take-off is worse than a blank one.
    """

    def __init__(self, shape, web_mm, flange_mm, thickness_mm,
                 lip_mm=0.0, designation=None):
        self.shape = shape
        self.web_mm = web_mm
        self.flange_mm = flange_mm
        self.lip_mm = lip_mm or 0.0
        self.thickness_mm = thickness_mm
        self.designation = designation

    @property
    def is_lipped(self):
        return self.shape == SHAPE_LIPPED_CHANNEL

    @property
    def developed_width_mm(self):
        """Flat width of the strip this section is roll-formed from.

        Corner radii shorten the real developed width very slightly; the
        sharp-corner sum used here is the same convention the published
        Brazilian profile tables use, and matches them to within ~0.5%.
        """
        width = self.web_mm + 2.0 * self.flange_mm
        if self.is_lipped:
            width += 2.0 * self.lip_mm
        return width

    @property
    def linear_mass_kg_m(self):
        """Mass per linear meter, in kg/m. None when thickness is unknown."""
        if not self.thickness_mm or self.thickness_mm <= 0:
            return None
        area_m2 = (self.developed_width_mm / 1000.0) * (self.thickness_mm / 1000.0)
        return area_m2 * STEEL_DENSITY_KG_M3

    @property
    def meets_nbr15253_structural_thickness(self):
        """True/False against the NBR 15253 structural minimum, or None if
        the thickness isn't known and so cannot be judged."""
        if not self.thickness_mm or self.thickness_mm <= 0:
            return None
        # Tolerance absorbs float noise on an exact-minimum 0,80 spec.
        return self.thickness_mm >= (NBR15253_MIN_STRUCTURAL_THICKNESS_MM - 1e-9)

    def width_depth_in(self):
        """(flange, web) in INCHES -- the (b, d) convention used by the
        geometry engines and by actual_dims_from_text()."""
        return (self.flange_mm / MM_PER_INCH, self.web_mm / MM_PER_INCH)

    def __repr__(self):
        return "SteelProfile({0!r}, web={1}, flange={2}, lip={3}, t={4})".format(
            self.shape, self.web_mm, self.flange_mm, self.lip_mm, self.thickness_mm
        )


def decode_nbr_designation(text):
    """Decode an ABNT/NBR profile designation into a SteelProfile.

    Handles the NBR 6355 / NBR 15253 forms in millimeters, with either a
    Brazilian decimal comma or a period:

        "Ue 90x40x12x0,95"  -> montante (lipped channel)
        "U 90x40x0,95"      -> guia (plain channel / track)
        "PGC 90" / "PGU 90" -> commercial naming, thickness unknown

    Returns None if the text carries no recognizable ABNT designation.
    """
    match = _NBR_DESIGNATION_RE.search(text or "")
    if match:
        keyword = match.group(1).lower()
        shape = SHAPE_PLAIN_CHANNEL
        for word, word_shape in _NBR_SHAPE_WORDS:
            if keyword == word:
                shape = word_shape
                break

        numbers = [_mm(g) for g in match.groups()[1:] if g is not None]
        web = numbers[0]
        if web < 20.0:
            return None

        # "Montante" is Portuguese for "stud" in BOTH systems, so a timber
        # section can carry a keyword this pattern accepts -- e.g.
        # "Montante 38x90mm" is 38 mm x 90 mm sawn timber, not a profile.
        # The two are told apart by the order of the pair: a steel section
        # is named web-first and its web is deeper than its flange
        # (90x40), while timber is named thickness-first and is thinner
        # than it is deep (38x90). A two-number name in timber order is
        # handed back so the lumber decoder can claim it.
        if len(numbers) == 2 and numbers[0] < numbers[1]:
            return None

        flange = lip = thickness = None
        if len(numbers) >= 4:
            # web x flange x lip x thickness -- only a lipped section is
            # ever written with four dimensions.
            flange, lip, thickness = numbers[1], numbers[2], numbers[3]
            shape = SHAPE_LIPPED_CHANNEL
        elif len(numbers) == 3:
            # web x flange x thickness; the lip, if this shape has one,
            # is not stated.
            flange, thickness = numbers[1], numbers[2]
        elif len(numbers) == 2:
            flange = numbers[1]

        # Anything the name does not state falls back to the documented
        # market-standard assumption -- except thickness, which is never
        # invented because a wrong weight is worse than a missing one.
        if flange is None or flange <= 0:
            flange = PG_ASSUMED_FLANGE_MM
        if lip is None:
            lip = PG_ASSUMED_LIP_MM if shape == SHAPE_LIPPED_CHANNEL else 0.0

        return SteelProfile(
            shape, web, flange, thickness,
            lip_mm=(lip if shape == SHAPE_LIPPED_CHANNEL else 0.0),
            designation=" ".join(match.group(0).split()),
        )

    return None


def decode_ssma_profile(token):
    """Decode an SSMA designation into a SteelProfile (millimeters).

    Unlike decode_ssma_designation(), this keeps the gauge -- the trailing
    mil value, thousandths of an inch -- which is required for mass.
    """
    match = _STEEL_DESIGNATION_RE.search((token or "").strip())
    if not match:
        return None
    dims = decode_ssma_designation(match.group(0))
    if dims is None:
        return None
    flange_in, web_in = dims
    letter = match.group(2).upper()
    thickness_mm = (int(match.group(4)) / 1000.0) * MM_PER_INCH
    return SteelProfile(
        SHAPE_LIPPED_CHANNEL if _SSMA_NOMINAL_LIP_IN.get(letter) else SHAPE_PLAIN_CHANNEL,
        web_in * MM_PER_INCH,
        flange_in * MM_PER_INCH,
        thickness_mm,
        lip_mm=_SSMA_NOMINAL_LIP_IN.get(letter, 0.0) * MM_PER_INCH,
        designation=match.group(0).upper(),
    )


def steel_profile_from_text(text):
    """Resolve a full SteelProfile from a family/type name, trying ABNT/NBR
    designations first and then SSMA. Returns None if neither matches."""
    return decode_nbr_designation(text) or decode_ssma_profile(text)


def linear_mass_kg_m_from_text(text):
    """Convenience: kg/m for a family/type name, or None if it can't be
    determined (unrecognized designation, or thickness not in the name)."""
    profile = steel_profile_from_text(text)
    return profile.linear_mass_kg_m if profile is not None else None


# Config field prefixes that carry a selected family/type, with the role
# label used when reporting on them. Both FramingConfig classes
# (wf_config and wf_wall_config) share these field names.
_CONFIG_PROFILE_FIELDS = (
    ("stud", "Stud / Montante"),
    ("bottom_plate", "Bottom track / Guia inferior"),
    ("top_plate", "Top track / Guia superior"),
    ("header", "Header / Verga"),
)


def config_profile_labels(config):
    """Extract (role_label, "Family Type") pairs from a FramingConfig.

    Used to feed the NBR 15253 check without each tool having to restate
    which config fields hold a profile selection.
    """
    pairs = []
    for prefix, role in _CONFIG_PROFILE_FIELDS:
        family = getattr(config, "{0}_family_name".format(prefix), None)
        type_name = getattr(config, "{0}_type_name".format(prefix), None)
        if not family and not type_name:
            continue
        pairs.append((role, "{0} {1}".format(family or "", type_name or "").strip()))
    return pairs


def check_nbr15253_compliance(labeled_types):
    """Check selected profiles against the NBR 15253 structural minimum.

    labeled_types: iterable of (role_label, "Family Type" text) pairs.

    Returns a list of human-readable warning strings -- one per profile
    whose decoded base steel thickness falls below
    NBR15253_MIN_STRUCTURAL_THICKNESS_MM. Profiles whose thickness cannot
    be determined are NOT reported: an unknown thickness is not evidence of
    non-compliance, and warning on it would train users to ignore the
    warning. Returns [] when everything checks out.
    """
    warnings = []
    seen = set()
    for role_label, type_text in labeled_types or ():
        if not type_text or type_text in seen:
            continue
        seen.add(type_text)
        try:
            profile = steel_profile_from_text(type_text)
        except Exception:
            continue
        if profile is None:
            continue
        if profile.meets_nbr15253_structural_thickness is False:
            warnings.append(
                "{0}: '{1}' has a base steel thickness of {2:.2f} mm, below "
                "the NBR 15253 structural minimum of {3:.2f} mm.".format(
                    role_label or "Profile",
                    profile.designation or type_text,
                    profile.thickness_mm,
                    NBR15253_MIN_STRUCTURAL_THICKNESS_MM,
                )
            )
    return warnings


def warn_nbr15253_compliance(labeled_types):
    """Print any NBR 15253 thickness warnings for the selected profiles.

    Best-effort and never raises -- safe to call outside Revit/pyRevit.
    Returns the warning list so callers can react if they want to.
    """
    warnings = check_nbr15253_compliance(labeled_types)
    if not warnings:
        return warnings
    try:
        from pyrevit import script

        body = "\n".join("> - {0}".format(item) for item in warnings)
        script.get_output().print_md(
            "\n> **NBR 15253 check:** one or more selected profiles are "
            "thinner than the standard's structural minimum. Framing was "
            "still generated -- verify the specification.\n"
            "{0}".format(body)
        )
    except Exception:
        pass
    return warnings


def warn_unresolved_steel_dims(kind_label, family_name, type_name):
    """Best-effort warning when a steel member's real dimensions couldn't be
    read off its family/type and a generic material default had to be
    substituted instead.

    Steel stud/track/joist dimensions vary far more than wood's fairly
    narrow "2x_" convention (1-5/8" to 12"+ deep, several gauges per
    depth), so silently guessing a generic default risks mis-sized framing
    going unnoticed -- unlike wood, where the generic fallback is very
    likely correct anyway, so it stays silent. Only call this for
    MATERIAL_STEEL. Safe to call from anywhere (including outside
    pyRevit/Revit) -- failures to emit the warning are swallowed so this
    can never block a run.
    """
    try:
        from pyrevit import script

        label = "{0} ({1} : {2})".format(
            kind_label, family_name or "?", type_name or "?"
        )
        script.get_output().print_md(
            "\n> **Warning: Steel dimensions could not be read for {0}.** "
            "The family/type may be missing, or missing a depth parameter "
            "('d' / 'Depth' / 'Web Depth' / ...). A generic default was used "
            "-- verify the generated framing size, or set the parameter on "
            "the family and re-run.".format(label)
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Revit "Material" asset assignment (STRUCTURAL_MATERIAL_PARAM)
#
# Everything above this point only affects placement GEOMETRY. It never
# touches the Revit Material asset (used for rendering, unit weight,
# thermal/structural analysis, and schedules) -- a placed instance's
# Material is whatever the loaded family/type already has configured,
# regardless of which framing_material the engine used for sizing. These
# functions let a caller who wants steel-sized geometry AND a steel
# Material asset explicitly assign one, instead of silently inheriting
# whatever the family happened to be authored with (which could still read
# as "Wood" in schedules/renders even though the geometry is steel-sized).
# ---------------------------------------------------------------------------

STEEL_MATERIAL_NAME_HINTS = (
    "steel", "galvanized", "galvanised", "cfs", "metal stud",
    "aço", "aco", "galvanizado", "galvanizada", "metálico", "metalico",
)


def list_materials(doc):
    """List every Material loaded in the project as (name, ElementId) pairs,
    sorted by name. Returns an empty list outside Revit or on any failure.
    """
    try:
        from Autodesk.Revit.DB import FilteredElementCollector, Material
    except Exception:
        return []
    try:
        materials = list(FilteredElementCollector(doc).OfClass(Material))
    except Exception:
        return []
    pairs = [(m.Name, m.Id) for m in materials if getattr(m, "Name", None)]
    pairs.sort(key=lambda pair: pair[0].lower())
    return pairs


def guess_steel_material_id(doc):
    """Best-effort: find a loaded Revit Material whose name suggests steel
    (English or Portuguese). Returns its ElementId, or None if nothing in
    the project matches -- the caller should fall back to asking the user
    or leaving the Material untouched, not to guessing further.
    """
    for name, material_id in list_materials(doc):
        lowered = name.lower()
        if any(hint in lowered for hint in STEEL_MATERIAL_NAME_HINTS):
            return material_id
    return None


def set_structural_material(doc, symbol, material_id):
    """Set a structural framing/column TYPE's Structural Material parameter
    to the given Material ElementId.

    This mutates the family TYPE (STRUCTURAL_MATERIAL_PARAM is a type
    parameter, shared by every instance of that type in the project), not
    just the instance being placed right now -- callers should only invoke
    this once per unique symbol per run, not once per placed instance, both
    to avoid redundant API calls and because setting it repeatedly to the
    same value is a no-op anyway.

    Returns True if the parameter was found, writable, and changed. Returns
    False (never raises) if the symbol has no such parameter, it's
    read-only (some custom families lock it), or it already points at the
    requested material -- all of those are legitimate, silent no-ops, not
    errors, since not every structural family exposes this parameter.
    """
    if symbol is None or material_id is None:
        return False
    try:
        from Autodesk.Revit.DB import BuiltInParameter
    except Exception:
        return False
    try:
        param = symbol.get_Parameter(BuiltInParameter.STRUCTURAL_MATERIAL_PARAM)
        if param is None or param.IsReadOnly:
            return False
        if param.AsElementId() == material_id:
            return False
        return bool(param.Set(material_id))
    except Exception:
        return False
