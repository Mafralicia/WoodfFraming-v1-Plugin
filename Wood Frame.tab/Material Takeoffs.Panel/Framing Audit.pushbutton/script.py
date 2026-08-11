# -*- coding: utf-8 -*-
"""Framing Audit -- report how every loaded framing type resolves.

The engines size members from the family type's real parameters when they
can read them, and fall back to decoding the type NAME when they cannot.
That fallback is invisible during a normal run: a name the decoders do not
recognize silently becomes a generic default, and the framing comes out
the wrong size with nothing on screen to say so.

This command makes that visible before it costs anything. It walks every
loaded structural framing and column type, reports which resolution path
each one takes and the section it lands on, and lists the ones that fall
through to a default so they can be renamed or given the missing
parameters.

It reads the model and writes nothing.
"""

import os
import sys

_ext_dir = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(__file__)
)))
while _ext_dir and not _ext_dir.lower().endswith(".extension"):
    _parent = os.path.dirname(_ext_dir)
    if _parent == _ext_dir:
        break
    _ext_dir = _parent
_lib_dir = os.path.join(_ext_dir, "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

from pyrevit import revit, DB, script

from wf_materials import (
    DEPTH_PARAM_NAMES,
    NBR15253_MAX_STRUCTURAL_THICKNESS_MM,
    NBR15253_MIN_STRUCTURAL_THICKNESS_MM,
    NBR15253_STANDARD_WEB_DEPTHS_MM,
    WIDTH_PARAM_NAMES,
    actual_dims_from_text,
    steel_profile_from_text,
)

output = script.get_output()

MM_PER_FT = 304.8

# How a type's section was established, best first.
SOURCE_PARAMETERS = "Family parameters"
SOURCE_NAME = "Type name"
SOURCE_NONE = "NOT RESOLVED"


def _symbol_param_mm(symbol, names):
    """First positive value among these type parameters, in millimeters.

    AsDouble() is always Revit internal units (feet) whatever the family
    was authored in, so a millimeter-authored Brazilian family needs no
    special handling.
    """
    for name in names:
        try:
            parameter = symbol.LookupParameter(name)
            if parameter is None:
                continue
            value = parameter.AsDouble()
            if value and value > 0.0:
                return value * MM_PER_FT
        except Exception:
            continue
    return None


def _type_text(symbol):
    try:
        family = getattr(symbol, "Family", None)
        family_name = getattr(family, "Name", "") if family is not None else ""
        return "{0} {1}".format(family_name or "", symbol.Name or "").strip()
    except Exception:
        return ""


def _collect_symbols(doc):
    symbols = []
    for category in (DB.BuiltInCategory.OST_StructuralFraming,
                     DB.BuiltInCategory.OST_StructuralColumns):
        try:
            collector = (DB.FilteredElementCollector(doc)
                         .OfCategory(category)
                         .OfClass(DB.FamilySymbol))
            symbols.extend(list(collector))
        except Exception:
            continue

    seen, unique = set(), []
    for symbol in symbols:
        try:
            key = symbol.Id.IntegerValue
        except Exception:
            key = id(symbol)
        if key not in seen:
            seen.add(key)
            unique.append(symbol)
    return unique


def _audit_symbol(symbol):
    """Return a dict describing how this type resolves."""
    text = _type_text(symbol)
    row = {
        "name": text or "(unnamed)",
        "width_mm": _symbol_param_mm(symbol, WIDTH_PARAM_NAMES),
        "depth_mm": _symbol_param_mm(symbol, DEPTH_PARAM_NAMES),
        "profile": None,
        "kind": "-",
        "source": SOURCE_NONE,
        "notes": [],
    }

    if row["width_mm"] and row["depth_mm"]:
        row["source"] = SOURCE_PARAMETERS

    try:
        profile = steel_profile_from_text(text)
    except Exception:
        profile = None

    if profile is not None:
        row["profile"] = profile
        row["kind"] = "Steel {0}".format(profile.shape)
        if row["source"] == SOURCE_NONE:
            row["source"] = SOURCE_NAME
            row["width_mm"] = profile.flange_mm
            row["depth_mm"] = profile.web_mm

        if profile.thickness_mm is None:
            row["notes"].append(
                "thickness not in the name, so no mass can be reported")
        else:
            if profile.thickness_mm < NBR15253_MIN_STRUCTURAL_THICKNESS_MM:
                row["notes"].append(
                    "thickness {0:.2f} mm is BELOW the NBR 15253 structural "
                    "minimum of {1:.2f} mm".format(
                        profile.thickness_mm,
                        NBR15253_MIN_STRUCTURAL_THICKNESS_MM))
            elif profile.thickness_mm > NBR15253_MAX_STRUCTURAL_THICKNESS_MM:
                row["notes"].append(
                    "thickness {0:.2f} mm is ABOVE the NBR 15253 range "
                    "(max {1:.2f} mm)".format(
                        profile.thickness_mm,
                        NBR15253_MAX_STRUCTURAL_THICKNESS_MM))
        if profile.web_mm not in NBR15253_STANDARD_WEB_DEPTHS_MM:
            row["notes"].append(
                "web {0:.0f} mm is not one of the NBR 15253 standard depths "
                "({1}) -- fine if your supplier offers it".format(
                    profile.web_mm,
                    ", ".join("%.0f" % d for d in NBR15253_STANDARD_WEB_DEPTHS_MM)))
        return row

    try:
        dims_in = actual_dims_from_text(text)
    except Exception:
        dims_in = None

    if dims_in:
        row["kind"] = "Timber"
        if row["source"] == SOURCE_NONE:
            row["source"] = SOURCE_NAME
            row["width_mm"] = dims_in[0] * 25.4
            row["depth_mm"] = dims_in[1] * 25.4
    elif row["source"] == SOURCE_PARAMETERS:
        # Parameters gave a section, the name told us nothing about which
        # material it is. Sizing still works; only the take-off column is
        # affected.
        row["kind"] = "Unknown material"
        row["notes"].append(
            "section read from parameters, but the name matches no known "
            "designation -- it will be quantified as timber (volume)")
    else:
        row["notes"].append(
            "no readable section: add a width/depth parameter to the family "
            "or rename the type to a recognized designation")
    return row


def _fmt_mm(value):
    return "{0:.1f}".format(value) if value else "-"


def main():
    doc = revit.doc
    symbols = _collect_symbols(doc)

    if not symbols:
        output.print_md(
            "## Framing Audit\n"
            "No structural framing or column family types are loaded.")
        return

    rows = [_audit_symbol(symbol) for symbol in symbols]
    rows.sort(key=lambda item: item["name"].lower())

    resolved = [r for r in rows if r["source"] != SOURCE_NONE]
    unresolved = [r for r in rows if r["source"] == SOURCE_NONE]
    steel = [r for r in rows if r["kind"].startswith("Steel")]
    timber = [r for r in rows if r["kind"] == "Timber"]
    flagged = [r for r in rows if r["notes"]]

    output.print_md(
        "# Framing Audit\n"
        "How each loaded framing type resolves to a cross-section. Types "
        "resolved from **family parameters** are exact. Types resolved from "
        "the **type name** rely on the name matching a known designation. "
        "Anything **NOT RESOLVED** falls back to a generic default at "
        "framing time, which is almost certainly the wrong size.\n\n"
        "- **Types found:** {0}\n"
        "- **Resolved:** {1}  (steel {2}, timber {3})\n"
        "- **Not resolved:** {4}\n"
        "- **With notes:** {5}".format(
            len(rows), len(resolved), len(steel), len(timber),
            len(unresolved), len(flagged)))

    table = [[r["name"], r["kind"], r["source"],
              _fmt_mm(r["width_mm"]), _fmt_mm(r["depth_mm"]),
              ("{0:.3f}".format(r["profile"].linear_mass_kg_m)
               if r["profile"] is not None and r["profile"].linear_mass_kg_m
               else "-")]
             for r in rows]

    output.print_table(
        table_data=table,
        title="Loaded framing types",
        columns=["Family : Type", "Kind", "Section from",
                 "Width mm", "Depth mm", "kg/m"])

    if unresolved:
        output.print_md(
            "\n## Not resolved -- these will be framed at a generic default")
        for row in unresolved:
            output.print_md("- **{0}** -- {1}".format(
                row["name"], "; ".join(row["notes"]) or "no section found"))

    if flagged:
        output.print_md("\n## Notes")
        for row in flagged:
            if row in unresolved:
                continue
            for note in row["notes"]:
                output.print_md("- **{0}**: {1}".format(row["name"], note))

    if not unresolved and not flagged:
        output.print_md(
            "\nEvery loaded type resolves cleanly, with nothing to flag.")


if __name__ == "__main__":
    main()
