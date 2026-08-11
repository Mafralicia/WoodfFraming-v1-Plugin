# -*- coding: utf-8 -*-
"""Accounting for framing members that were calculated but not placed.

The placement loop has several paths that drop a member and move on: the
family type cannot be found, the member is shorter than the minimum
length, or the Revit API refuses to create the instance. Each of those was
a bare `continue` or a swallowed exception, so a run that asked for 412
members and produced 398 reported success and said nothing about the 14.

That silence is the problem this module exists to end. Missing members are
not a cosmetic issue here -- the take-off is derived from what actually got
placed, so every dropped member is a quantity the schedule under-reports,
and a quiet under-report is worse than a loud failure.

The design keeps the placement loop's behaviour identical: a member that
cannot be placed is still skipped rather than aborting the run, because
one bad family should not cost the user the other 398 members. The only
change is that the skip is now recorded and reported.

Pure Python -- no Revit imports -- so it can be tested directly.
"""

from collections import OrderedDict


# Skip reasons. Kept as named constants so the reporting groups them
# reliably and a typo cannot silently create a new category.
REASON_NO_SYMBOL = "Family type could not be found in the project"
REASON_TOO_SHORT = "Member shorter than the minimum placeable length"
REASON_API_REFUSED = "Revit refused to create the instance"
REASON_NO_LEVEL = "Host has no usable level"

# Reasons in the order a report should list them: the ones the user can
# act on come first.
_REASON_ORDER = (
    REASON_NO_SYMBOL,
    REASON_API_REFUSED,
    REASON_NO_LEVEL,
    REASON_TOO_SHORT,
)


class PlacementReport(object):
    """Tally of what a placement run asked for, produced, and dropped."""

    def __init__(self):
        self.requested = 0
        self.placed = 0
        self._skips = []

    # -- recording ------------------------------------------------------

    def skip(self, reason, detail=None):
        """Record one dropped member.

        detail is free text identifying the specific cause -- a family and
        type name, or an API error message -- and is used to group the
        report so a hundred members failing on one missing family read as
        one line, not a hundred.
        """
        self._skips.append((reason, (detail or "").strip()))

    # -- reading --------------------------------------------------------

    @property
    def skipped(self):
        return len(self._skips)

    @property
    def is_clean(self):
        """True when nothing was dropped."""
        return not self._skips

    @property
    def is_total_failure(self):
        """True when members were requested and none survived.

        Worth separating: a partial loss is a warning, but asking for
        members and getting none back usually means a misconfiguration
        rather than a handful of edge cases.
        """
        return self.requested > 0 and self.placed == 0

    def grouped(self):
        """Skips as an ordered [(reason, detail, count)] list."""
        counter = OrderedDict()
        for reason, detail in self._skips:
            key = (reason, detail)
            counter[key] = counter.get(key, 0) + 1

        def sort_key(item):
            (reason, detail), _count = item
            try:
                rank = _REASON_ORDER.index(reason)
            except ValueError:
                rank = len(_REASON_ORDER)
            return (rank, detail)

        return [(reason, detail, count)
                for (reason, detail), count in sorted(counter.items(),
                                                      key=sort_key)]

    def merge(self, other):
        """Fold another report into this one.

        Tools frame several hosts per run, each producing its own report;
        the user cares about the run, not the host.
        """
        if other is None:
            return self
        self.requested += other.requested
        self.placed += other.placed
        self._skips.extend(other._skips)
        return self

    # -- rendering ------------------------------------------------------

    def summary_line(self):
        return "Requested {0}, placed {1}, skipped {2}.".format(
            self.requested, self.placed, self.skipped)

    def markdown(self, title="Placement"):
        """Report block for the pyRevit output panel, or "" when clean.

        Returns an empty string on a clean run so callers can print it
        unconditionally without adding noise to the normal case.
        """
        if self.is_clean:
            return ""

        lines = ["### {0}: {1} member(s) were not placed".format(
            title, self.skipped)]

        if self.is_total_failure:
            lines.append(
                "\n> **Nothing was placed.** Every calculated member was "
                "dropped, which normally means the selected family types "
                "are missing rather than that the geometry is wrong.")

        lines.append("\n" + self.summary_line())
        lines.append(
            "\n**The take-off reflects what was placed, so these members "
            "are missing from it.**\n")

        for reason, detail, count in self.grouped():
            if detail:
                lines.append("- {0} x {1}: `{2}`".format(count, reason, detail))
            else:
                lines.append("- {0} x {1}".format(count, reason))

        return "\n".join(lines)
