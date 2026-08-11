# -*- coding: utf-8 -*-
"""Generate the native Revit BOM schedule for wood framing."""

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

from wf_schedule_utils import (
    BOM_SCHEDULE_NAME,
    activate_schedule,
    create_or_update_bom_schedule,
)
from wf_takeoff import (
    DEFAULT_BAR_LENGTH_M,
    DEFAULT_WASTE_PCT,
    PurchaseLine,
    summarize,
)


output = script.get_output()

FT_TO_M = 0.3048


def _shared_double(element, name):
    try:
        parameter = element.LookupParameter(name)
        if parameter is None:
            return 0.0
        return parameter.AsDouble() or 0.0
    except Exception:
        return 0.0


def _shared_text(element, name):
    try:
        parameter = element.LookupParameter(name)
        if parameter is None:
            return ""
        return parameter.AsString() or ""
    except Exception:
        return ""


def _is_generated(element):
    try:
        parameter = element.LookupParameter("WF_IsGenerated")
        return parameter is not None and parameter.AsInteger() == 1
    except Exception:
        return False


def _type_label(element):
    try:
        symbol = getattr(element, "Symbol", None)
        if symbol is None:
            return "(unknown type)"
        family = getattr(symbol, "Family", None)
        family_name = getattr(family, "Name", "") if family is not None else ""
        return "{0} : {1}".format(family_name or "?", symbol.Name or "?")
    except Exception:
        return "(unknown type)"


def _collect_purchase_lines(doc):
    """Group generated members by profile/type and sum their quantities."""
    grouped = {}
    for category in (DB.BuiltInCategory.OST_StructuralFraming,
                     DB.BuiltInCategory.OST_StructuralColumns):
        try:
            collector = (DB.FilteredElementCollector(doc)
                         .OfCategory(category)
                         .WhereElementIsNotElementType())
        except Exception:
            continue
        for element in collector:
            if not _is_generated(element):
                continue
            label = _shared_text(element, "WF_Profile") or _type_label(element)
            line = grouped.get(label)
            if line is None:
                line = PurchaseLine(label)
                grouped[label] = line
            line.count += 1
            line.length_m += _shared_double(element, "WF_MemberLength") * FT_TO_M
            line.mass_kg += _shared_double(element, "WF_TotalMass")
            line.volume_m3 += _shared_double(element, "WF_Volume")
    return [grouped[key] for key in sorted(grouped)]


def _print_purchase_report(doc):
    lines = _collect_purchase_lines(doc)
    if not lines:
        return

    report = summarize(lines, waste_pct=DEFAULT_WASTE_PCT,
                       bar_length_m=DEFAULT_BAR_LENGTH_M)

    output.print_md(
        "\n## Purchase estimate\n"
        "The schedule above is the **net** measurement. This section adds a "
        "waste allowance and rounds to whole stock bars, which is a "
        "commercial estimate rather than a measurement.\n\n"
        "- **Waste allowance:** {0:.1f}%\n"
        "- **Stock bar length:** {1:.2f} m\n\n"
        "Both figures are defaults, not recommendations -- change them in "
        "`lib/wf_takeoff.py` to match your supplier and your crew. Bar "
        "counts assume the waste allowance absorbs offcut loss; a real "
        "nesting plan may need one or two more per profile.".format(
            report["waste_pct"], report["bar_length_m"]))

    table = []
    for row in report["rows"]:
        table.append([
            row["label"],
            str(row["count"]),
            "{0:.2f}".format(row["net_length_m"]),
            "{0:.2f}".format(row["purchase_length_m"]),
            "{0:.2f}".format(row["net_mass_kg"]) if row["net_mass_kg"] else "-",
            "{0:.2f}".format(row["purchase_mass_kg"]) if row["net_mass_kg"] else "-",
            "{0:.4f}".format(row["net_volume_m3"]) if row["net_volume_m3"] else "-",
            str(row["bars"]),
        ])

    output.print_table(
        table_data=table,
        title="Net vs. purchase quantities",
        columns=["Profile / Type", "Pcs", "Net m", "Buy m",
                 "Net kg", "Buy kg", "Net m3", "Bars"])

    output.print_md(
        "**Totals** -- net {0:.2f} m / {1:.2f} kg / {2:.4f} m3; "
        "purchase {3:.2f} m / {4:.2f} kg / {5:.4f} m3; "
        "**{6} bars** across {7} profiles.".format(
            report["net_length_m"], report["net_mass_kg"],
            report["net_volume_m3"], report["purchase_length_m"],
            report["purchase_mass_kg"], report["purchase_volume_m3"],
            report["total_bars"], len(report["rows"])))


def main():
    doc = revit.doc

    with revit.Transaction("WF: Update BOM Schedule"):
        schedule = create_or_update_bom_schedule(doc)

    activate_schedule(schedule)
    output.print_md(
        "## Generate BOM\n"
        "- **Schedule updated:** {0}\n"
        "- **Scope:** generated framing members grouped by host, member role, and family/type".format(
            BOM_SCHEDULE_NAME
        )
    )

    try:
        _print_purchase_report(doc)
    except Exception as exc:
        # The schedule is the deliverable; a failure in the estimate must
        # not make the command look like it failed.
        output.print_md(
            "\n> Purchase estimate could not be produced: {0}".format(exc))


if __name__ == "__main__":
    main()
