# -*- coding: utf-8 -*-
"""What the framing tools will actually draw, for a given set of options.

Single source of truth for answering "if I press Frame now, what members
do I get?" -- used to show a live preview in every dialog and to print the
same breakdown after a run.

Three things make this worth having as its own module rather than prose in
each dialog:

  1. The options are spread across four engines that read different
     subsets of the config. A floor ignores everything about openings; a
     ceiling has layout modes a wall does not. Stating that per tool, in
     one place, keeps the dialogs from promising members the engine will
     never create.

  2. Some members are unconditional and some are optional, and which is
     which is not guessable from the dialog. Headers and sills always
     appear; king, jack and cripple studs are switchable.

  3. What is NOT drawn matters as much as what is. Wall diagonal bracing
     is the important case: the tools do not generate it in any material,
     and a user who assumes otherwise would ship an unbraced panel. That
     belongs on screen, not in a manual.

Every statement here was derived by reading the engines. When engine
behaviour changes this module has to change with it, which is why the
tests assert the config fields it names actually exist.

Pure Python -- no Revit or pyRevit imports -- so it can be tested directly.
"""

from wf_materials import MATERIAL_STEEL


# Host kinds, matching the tool that frames them.
HOST_WALL = "wall"
HOST_FLOOR = "floor"
HOST_CEILING = "ceiling"
HOST_ROOF = "roof"
HOST_JOIN = "join"

# Roof framing styles.
ROOF_MODE_TRUSS = "truss"
ROOF_MODE_STICK = "stick"


def _material_terms(config):
    """Member vocabulary for the configured material.

    Steel and timber framing name the same parts differently -- a wall's
    horizontal top member is a plate in timber and a track in steel -- and
    a dialog that used the wrong word for the selected material would read
    as describing some other system.
    """
    if getattr(config, "framing_material", None) == MATERIAL_STEEL:
        return {
            "stud": "stud (montante)",
            "plate": "track (guia)",
            "blocking": "bridging / blocking (travamento horizontal)",
            "joist": "joist (viga)",
            "rim": "rim track (guia de bordo)",
        }
    return {
        "stud": "stud (montante)",
        "plate": "plate (soleira/umbral)",
        "blocking": "blocking (travamento horizontal)",
        "joist": "joist (barrote)",
        "rim": "rim joist (barrote de bordo)",
    }


def _spacing_text(config):
    """Human spacing description in both inch and millimeter terms."""
    spacing_in = getattr(config, "stud_spacing", None)
    if not spacing_in or spacing_in <= 0:
        return "spacing not set"
    return '{0:.3g}" o.c. ({1:.0f} mm)'.format(spacing_in, spacing_in * 25.4)


def _plural(count, singular):
    """Naive pluralisation, with the irregulars this module actually uses.

    The material terms carry a parenthetical gloss ("track (guia)"), so the
    "s" has to land on the head noun rather than the end of the string.
    """
    if count == 1:
        return singular
    irregular = {"ply": "plies"}
    if singular in irregular:
        return irregular[singular]
    if " (" in singular:
        head, gloss = singular.split(" (", 1)
        return "{0}s ({1}".format(head, gloss)
    return singular + "s"


def _wall_spec(config, terms):
    included, optional_off, never = [], [], []

    included.append("Studs / montantes at {0}, full wall height".format(
        _spacing_text(config)))

    bottom = int(getattr(config, "bottom_plate_count", 1) or 1)
    top = int(getattr(config, "top_plate_count", 2) or 1)
    # One gloss for both, so the line does not repeat "(guia)" twice.
    included.append("{0} bottom and {1} top {2}".format(
        bottom, top, _plural(max(bottom, top), terms["plate"])))

    if getattr(config, "include_mid_plates", False):
        interval = float(getattr(config, "mid_plate_interval_ft", 8.0) or 8.0)
        included.append(
            "Horizontal {0}, one row every {1:.3g} ft ({2:.2f} m) of height".format(
                terms["blocking"], interval, interval * 0.3048))
    else:
        optional_off.append(
            "Horizontal {0} -- no mid-height rows will be drawn".format(
                terms["blocking"]))

    # Openings. Headers and sills are unconditional in the assemblies;
    # king, jack and cripple studs are switchable.
    header_plies = int(getattr(config, "header_count", 2) or 1)
    included.append(
        "At every door and window: header in {0} {1}, plus a sill under "
        "each window".format(header_plies, _plural(header_plies, "ply")))

    for field, label in (
        ("include_king_studs", "King studs beside each opening"),
        ("include_jack_studs", "Jack studs carrying the header"),
        ("include_cripple_studs", "Cripple studs above headers and below sills"),
    ):
        if getattr(config, field, True):
            included.append(label)
        else:
            optional_off.append("{0} -- switched off".format(label))

    if not getattr(config, "include_jack_studs", True):
        optional_off.append(
            "WARNING: with jack studs off the header has nothing to bear "
            "on. The option is honoured, but it is not a normal detail.")

    corner_style = getattr(config, "corner_style", "three_stud")
    included.append("At wall corners: {0} assembly".format(
        {"three_stud": "three-stud", "california": "California (two-stud + backing)",
         "two_stud": "two-stud"}.get(corner_style, corner_style)))

    t_style = getattr(config, "t_intersection_style", "three_stud_t")
    included.append("At T intersections: {0} assembly".format(
        {"three_stud_t": "three-stud T", "ladder": "ladder blocking",
         "backing_stud": "backing stud"}.get(t_style, t_style)))

    never.append(
        "Diagonal bracing (contraventamento em X / fita de travamento) is "
        "NOT generated for walls in any material -- add it separately.")
    never.append(
        "Sheathing is not framing: use Split Sheathing for panel take-off.")
    return included, optional_off, never


def _floor_spec(config, terms):
    included = [
        "{0} at {1}, spanning the floor".format(
            terms["joist"].capitalize(), _spacing_text(config)),
        "{0} around the floor perimeter".format(terms["rim"].capitalize()),
    ]
    never = [
        "No openings, trimmers or headers are framed around floor "
        "penetrations -- stair and shaft openings need manual framing.",
        "No diagonal bracing, strongbacks or solid blocking rows between "
        "joists are generated.",
    ]
    return included, [], never


def _ceiling_spec(config, terms):
    direction = getattr(config, "ceiling_direction_mode", "auto")
    direction_text = {
        "auto": "spanning the shorter direction (auto)",
        "x": "along the local X axis",
        "y": "along the local Y axis",
        "both": "in BOTH directions, forming a grid",
    }.get(direction, direction)

    included = [
        "{0} at {1}, {2}".format(
            terms["joist"].capitalize(), _spacing_text(config), direction_text),
        "{0} at the ceiling perimeter".format(terms["rim"].capitalize()),
    ]

    placement = getattr(config, "ceiling_placement_mode", "above")
    included.append(
        "Placed {0}".format(
            "above the ceiling top face" if placement == "above"
            else "centred in the ceiling layer (legacy)"))

    layout = getattr(config, "ceiling_layout_mode", "standard")
    included.append(
        "Layout: {0}".format(
            "centred on the ceiling" if layout == "centered"
            else "standard, running from one edge"))

    never = [
        "No diagonal bracing or hanger/suspension members are generated.",
    ]
    return included, [], never


def _roof_spec(config, terms, mode):
    included, optional_off, never = [], [], []
    is_truss = (mode == ROOF_MODE_TRUSS)

    if is_truss:
        truss_type = getattr(config, "truss_type", "Dynamic")
        included.append("Trusses at {0}".format(_spacing_text(config)))
        included.append(
            "Each truss: top chords, bottom chord, and {0} web bracing "
            "(diagonal members inside the truss)".format(truss_type))
        web_spacing = getattr(config, "web_spacing", None)
        if web_spacing:
            included.append(
                'Web members spaced about {0:.3g}" ({1:.0f} mm) apart'.format(
                    float(web_spacing), float(web_spacing) * 25.4))
    else:
        included.append("Rafters at {0}".format(_spacing_text(config)))
        included.append("Ridge board along the roof ridge, plus hips where present")
        included.append("Continuous purlins across the rafters")

    # Collar ties, ceiling joists, kickers and king posts are built by the
    # stick path only -- truss mode runs a separate branch that never
    # reaches them, so in truss mode these four options have no effect on
    # what is drawn and must not be listed as members the user will get.
    ridge_package = (
        ("include_collar_ties", "Collar ties across the ridge", True),
        ("include_ceiling_joists", "Ceiling joists at the wall plate line", True),
        ("include_roof_kickers",
         "Diagonal kickers / outriggers from ceiling joist to rafter", True),
        ("include_king_posts", "Vertical king posts under the ridge", False),
    )

    if is_truss:
        never.append(
            "Collar ties, ceiling joists, kickers and king posts are part of "
            "the stick-framing package. Truss mode does not draw them, "
            "whatever those options are set to -- the truss's own web "
            "members do that job.")
    else:
        for field, label, default in ridge_package:
            if getattr(config, field, default):
                included.append(label)
            else:
                optional_off.append("{0} -- switched off".format(label))

    never.append(
        "No roof-plane diagonal bracing or wind girder is generated.")
    return included, optional_off, never


def _join_spec(config, terms):
    included = [
        "Replaces generated framing near the selected join with a clean "
        "corner or T assembly",
        "Studs / montantes and blocking for the chosen assembly only",
    ]
    never = [
        "This tool reframes one join. It does not re-run wall framing, and "
        "it adds no diagonal bracing.",
    ]
    return included, [], never


_DISPATCH = {
    HOST_WALL: _wall_spec,
    HOST_FLOOR: _floor_spec,
    HOST_CEILING: _ceiling_spec,
    HOST_JOIN: _join_spec,
}


def describe(host_kind, config, mode=None):
    """Return (included, optional_off, never) description lists.

    included      -- members that WILL be drawn with the current options
    optional_off  -- members the user has switched off
    never         -- what this tool does not draw at all, regardless of
                     options; the honest limits of the output
    """
    if config is None:
        return ([], [], [])
    terms = _material_terms(config)
    if host_kind == HOST_ROOF:
        return _roof_spec(config, terms, mode or ROOF_MODE_TRUSS)
    handler = _DISPATCH.get(host_kind)
    if handler is None:
        return ([], [], [])
    return handler(config, terms)


def describe_text(host_kind, config, mode=None, bullet=u"• "):
    """Same content as describe(), rendered as a plain-text block for a
    dialog TextBlock or a console summary."""
    included, optional_off, never = describe(host_kind, config, mode=mode)
    lines = []
    if included:
        lines.append("WILL BE DRAWN:")
        lines.extend(bullet + item for item in included)
    if optional_off:
        lines.append("")
        lines.append("SWITCHED OFF:")
        lines.extend(bullet + item for item in optional_off)
    if never:
        lines.append("")
        lines.append("NOT GENERATED BY THIS TOOL:")
        lines.extend(bullet + item for item in never)
    return "\n".join(lines)


def describe_markdown(host_kind, config, mode=None):
    """Same content rendered as Markdown, for the pyRevit output panel."""
    included, optional_off, never = describe(host_kind, config, mode=mode)
    parts = []
    if included:
        parts.append("**Drawn:**\n" + "\n".join("- " + i for i in included))
    if optional_off:
        parts.append("**Switched off:**\n" + "\n".join("- " + i for i in optional_off))
    if never:
        parts.append("**Not generated by this tool:**\n"
                     + "\n".join("- " + i for i in never))
    return "\n\n".join(parts)
