# -*- coding: utf-8 -*-
"""Turning net framing quantities into purchase quantities.

The BOM reports NET quantities -- the length, mass and volume actually
modelled. That is the right thing for it to report, because it is a
measurement rather than an estimate, and mixing an assumption into it
would make every number downstream unauditable.

Buying is a separate question. You cannot buy the net length: stock comes
in fixed bars, offcuts are lost, and the trade allowance for that loss is
a commercial decision that varies by contractor and by job. This module
keeps that arithmetic apart from the measurement, with every input stated
rather than buried.

Nothing here is a recommendation. The defaults below are the common
starting points, not a specification -- they are meant to be overridden,
and the report says so wherever it prints them.
"""

import math


# Common starting points, NOT recommendations -- see the module docstring.
# A waste allowance is what covers offcuts, and the right figure depends
# on the job, the panel layout, and how much the crew can nest cuts.
DEFAULT_WASTE_PCT = 10.0

# Stock bar length. 6 m is the usual mill length for Brazilian LSF
# profiles and structural timber alike, but suppliers cut to order and
# some deliver 3 m for guias, so this is an input, not a constant of
# nature.
DEFAULT_BAR_LENGTH_M = 6.0


def apply_waste(quantity, waste_pct=DEFAULT_WASTE_PCT):
    """Scale a net quantity up by a waste allowance percentage."""
    if quantity is None:
        return None
    if waste_pct is None or waste_pct <= 0:
        return quantity
    return quantity * (1.0 + waste_pct / 100.0)


def bars_required(total_length_m, bar_length_m=DEFAULT_BAR_LENGTH_M,
                  waste_pct=DEFAULT_WASTE_PCT):
    """Whole stock bars needed to cover a net length, after waste.

    This divides total length by bar length and rounds up. It is NOT a
    cutting plan: it assumes the waste allowance is what absorbs offcut
    loss. A real nesting optimisation, which accounts for which member
    lengths can share a bar, will usually land on the same figure or one
    or two bars higher -- so treat this as the floor, not the answer.
    """
    if not total_length_m or total_length_m <= 0:
        return 0
    if not bar_length_m or bar_length_m <= 0:
        return 0
    return int(math.ceil(apply_waste(total_length_m, waste_pct) / bar_length_m))


class PurchaseLine(object):
    """One profile's net and purchase quantities."""

    def __init__(self, label, length_m=0.0, mass_kg=0.0, volume_m3=0.0,
                 count=0):
        self.label = label
        self.length_m = length_m
        self.mass_kg = mass_kg
        self.volume_m3 = volume_m3
        self.count = count

    def purchase_length_m(self, waste_pct=DEFAULT_WASTE_PCT):
        return apply_waste(self.length_m, waste_pct)

    def purchase_mass_kg(self, waste_pct=DEFAULT_WASTE_PCT):
        return apply_waste(self.mass_kg, waste_pct)

    def purchase_volume_m3(self, waste_pct=DEFAULT_WASTE_PCT):
        return apply_waste(self.volume_m3, waste_pct)

    def bars(self, bar_length_m=DEFAULT_BAR_LENGTH_M,
             waste_pct=DEFAULT_WASTE_PCT):
        return bars_required(self.length_m, bar_length_m, waste_pct)


def summarize(lines, waste_pct=DEFAULT_WASTE_PCT,
              bar_length_m=DEFAULT_BAR_LENGTH_M):
    """Aggregate PurchaseLines into a report-ready structure.

    Returns a dict with the per-line figures and the totals. Bars are
    summed per line rather than computed from the grand total, because
    stock cannot be shared between different profiles.
    """
    rows = []
    total_net_length = total_net_mass = total_net_volume = 0.0
    total_bars = 0
    total_count = 0

    for line in lines or ():
        bars = line.bars(bar_length_m, waste_pct)
        rows.append({
            "label": line.label,
            "count": line.count,
            "net_length_m": line.length_m,
            "net_mass_kg": line.mass_kg,
            "net_volume_m3": line.volume_m3,
            "purchase_length_m": line.purchase_length_m(waste_pct),
            "purchase_mass_kg": line.purchase_mass_kg(waste_pct),
            "purchase_volume_m3": line.purchase_volume_m3(waste_pct),
            "bars": bars,
        })
        total_net_length += line.length_m or 0.0
        total_net_mass += line.mass_kg or 0.0
        total_net_volume += line.volume_m3 or 0.0
        total_bars += bars
        total_count += line.count or 0

    return {
        "rows": rows,
        "waste_pct": waste_pct,
        "bar_length_m": bar_length_m,
        "total_count": total_count,
        "net_length_m": total_net_length,
        "net_mass_kg": total_net_mass,
        "net_volume_m3": total_net_volume,
        "purchase_length_m": apply_waste(total_net_length, waste_pct),
        "purchase_mass_kg": apply_waste(total_net_mass, waste_pct),
        "purchase_volume_m3": apply_waste(total_net_volume, waste_pct),
        "total_bars": total_bars,
    }
