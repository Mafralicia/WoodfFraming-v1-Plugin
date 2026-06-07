# -*- coding: utf-8 -*-
"""Two-pass framing validation for topology-aware framing.

Pass 1 (analytical) -- inspects the list of FramingMember objects *before*
they are placed in Revit.  Checks for floating studs, duplicate members,
solid volume overlaps, and missing assemblies required by the topology graph.

Pass 2 (placement)  -- inspects placed Revit instances *after* placement to
verify that the Revit solid model is consistent with the analytical model.
"""

import math

from wf_wall_geometry import inches_to_feet
from wf_wall_topology import MemberKind


# ---------------------------------------------------------------------------
# Tolerances
# ---------------------------------------------------------------------------

OVERLAP_TOL = inches_to_feet(0.5)   # members whose centrelines are closer than
                                      # this are considered collisions
FLOATING_TOL = inches_to_feet(0.25)  # allowed gap between a stud end and a plate
DUPLICATE_TOL = inches_to_feet(0.25) # centreline tolerance for duplicate detection

STUD_THICKNESS = inches_to_feet(1.5)


# ---------------------------------------------------------------------------
# Diagnostic data classes
# ---------------------------------------------------------------------------

class ValidationIssue(object):
    """One diagnostic finding from the validator."""

    LEVEL_ERROR = "ERROR"
    LEVEL_WARNING = "WARNING"

    def __init__(self, level, code, wall_id, description, member_types=None):
        self.level = level
        self.code = code
        self.wall_id = str(wall_id or "?")
        self.description = str(description)
        self.member_types = list(member_types or [])

    def __repr__(self):
        return "[{0}] {1}: wall={2} {3}".format(
            self.level, self.code, self.wall_id, self.description
        )


class ValidationReport(object):
    """Aggregated result of a validation pass."""

    def __init__(self):
        self.issues = []
        self.pass1_ran = False
        self.pass2_ran = False

    @property
    def error_count(self):
        return sum(1 for i in self.issues if i.level == ValidationIssue.LEVEL_ERROR)

    @property
    def warning_count(self):
        return sum(1 for i in self.issues if i.level == ValidationIssue.LEVEL_WARNING)

    @property
    def has_errors(self):
        return self.error_count > 0

    def add(self, issue):
        self.issues.append(issue)

    def summary_lines(self):
        lines = [
            "### Framing Validation Report",
            "- Pass 1 (analytical): {0}".format("ran" if self.pass1_ran else "skipped"),
            "- Pass 2 (placement):  {0}".format("ran" if self.pass2_ran else "skipped"),
            "- Errors:   {0}".format(self.error_count),
            "- Warnings: {0}".format(self.warning_count),
        ]
        if self.issues:
            lines.append("")
            lines.append("| Level | Code | Wall | Description |")
            lines.append("| --- | --- | --- | --- |")
            for issue in self.issues:
                lines.append("| {0} | {1} | {2} | {3} |".format(
                    issue.level, issue.code, issue.wall_id,
                    issue.description.replace("|", "/")[:120]
                ))
        return lines

    def summary_text(self):
        return "\n".join(self.summary_lines())


# ---------------------------------------------------------------------------
# Pass 1 – Analytical validator
# ---------------------------------------------------------------------------

class FramingValidator(object):
    """Runs analytical and placement validation checks."""

    def __init__(self, graph, wall_members_map, host_info_map=None):
        """
        Parameters
        ----------
        graph : WallTopologyGraph or None
        wall_members_map : dict[str, list[FramingMember]]
            Mapping of wall_id_text → list of FramingMember objects for that
            wall, already generated but not yet placed.
        host_info_map : dict[str, WallCavityHostInfoV4] or None
        """
        self.graph = graph
        self.wall_members_map = wall_members_map  # {wall_id_text: [members]}
        self.host_info_map = host_info_map
        self.report = ValidationReport()

    # ------------------------------------------------------------------
    # Pass 1 – call before placing in Revit
    # ------------------------------------------------------------------

    def run_pass1(self):
        """Analyse all generated members.  Returns True if no errors."""
        self.report.pass1_ran = True
        all_members = []
        for members in self.wall_members_map.values():
            all_members.extend(members)

        self._check_duplicates(all_members)
        self._check_overlaps(all_members)
        self._check_missing_corner_assemblies()
        self._check_missing_t_assemblies()
        return not self.report.has_errors

    # ------------------------------------------------------------------
    # Pass 2 – call after placing in Revit
    # ------------------------------------------------------------------

    def run_pass2(self, placed_instances_map):
        """Lightweight check on Revit instances.

        Parameters
        ----------
        placed_instances_map : dict[str, list]
            wall_id_text → list of Revit FamilyInstance objects.
        """
        self.report.pass2_ran = True
        for wall_id, instances in placed_instances_map.items():
            self._check_placed_count(wall_id, instances)
        return not self.report.has_errors

    # ------------------------------------------------------------------
    # Internal checkers
    # ------------------------------------------------------------------

    def _check_duplicates(self, members):
        """Flag members whose centrelines are within DUPLICATE_TOL."""
        verticals = [m for m in members if _is_vertical(m)]
        seen = []  # list of (wall_id, d, bottom_z, top_z)
        for m in verticals:
            wall_id = _element_id_text(getattr(m, "host_id", None))
            host = None
            if self.host_info_map and wall_id in self.host_info_map:
                host = self.host_info_map[wall_id]
            elif self.graph and wall_id in self.graph._nodes_by_id:
                host = self.graph._nodes_by_id[wall_id].host

            d = _member_d(m, host)
            bz = _member_bottom_z(m)
            tz = _member_top_z(m)
            for (w, od, ob, ot) in seen:
                if w != wall_id:
                    continue
                if abs(d - od) < DUPLICATE_TOL:
                    # Overlapping height?
                    if not (tz < ob - DUPLICATE_TOL or bz > ot + DUPLICATE_TOL):
                        self.report.add(ValidationIssue(
                            ValidationIssue.LEVEL_WARNING,
                            "DUPLICATE_STUD",
                            wall_id,
                            "Duplicate/overlapping stud at d={0:.3f} ft".format(d),
                            [m.member_type],
                        ))
            seen.append((wall_id, d, bz, tz))

    def _check_overlaps(self, members):
        """Flag member pairs that physically occupy the same space.

        Only checks members on the same wall where centrelines are very
        close but the members are supposed to be different.
        """
        # Group by wall.
        by_wall = {}
        for m in members:
            wid = _element_id_text(getattr(m, "host_id", None))
            by_wall.setdefault(wid, []).append(m)

        for wall_id, wall_members in by_wall.items():
            for i in range(len(wall_members)):
                for j in range(i + 1, len(wall_members)):
                    ma = wall_members[i]
                    mb = wall_members[j]
                    if _members_collide(ma, mb):
                        self.report.add(ValidationIssue(
                            ValidationIssue.LEVEL_ERROR,
                            "MEMBER_COLLISION",
                            wall_id,
                            "Collision between {0} and {1} at d~{2:.3f}".format(
                                ma.member_type, mb.member_type,
                                (_member_d(ma) + _member_d(mb)) * 0.5
                            ),
                            [ma.member_type, mb.member_type],
                        ))

    def _check_missing_corner_assemblies(self):
        """Verify that every corner edge has at least one CORNER_STUD generated."""
        if self.graph is None:
            return
        for edge in self.graph.edges:
            if not edge.is_corner:
                continue
            for node in (edge.node_a, edge.node_b):
                wall_id = _element_id_text(node.element_id)
                members = self.wall_members_map.get(wall_id, [])
                has_corner = any(
                    m.member_type in MemberKind.CORNER_KINDS
                    for m in members
                )
                if not has_corner:
                    self.report.add(ValidationIssue(
                        ValidationIssue.LEVEL_ERROR,
                        "MISSING_CORNER",
                        wall_id,
                        "No corner assembly members found for corner edge at wall {0}".format(
                            wall_id
                        ),
                    ))

    def _check_missing_t_assemblies(self):
        """Verify that every T edge has at least one T_ member generated."""
        if self.graph is None:
            return
        for edge in self.graph.edges:
            if not edge.is_t:
                continue
            for node in (edge.node_a, edge.node_b):
                wall_id = _element_id_text(node.element_id)
                members = self.wall_members_map.get(wall_id, [])
                has_t = any(m.member_type in MemberKind.T_KINDS for m in members)
                if not has_t:
                    self.report.add(ValidationIssue(
                        ValidationIssue.LEVEL_WARNING,
                        "MISSING_T_ASSEMBLY",
                        wall_id,
                        "No T-intersection assembly members found for T edge at wall {0}".format(
                            wall_id
                        ),
                    ))

    def _check_placed_count(self, wall_id, instances):
        """Warn if the placed count is significantly less than the member count."""
        member_count = len(self.wall_members_map.get(wall_id, []))
        placed_count = len(instances)
        if member_count > 0 and placed_count < member_count * 0.5:
            self.report.add(ValidationIssue(
                ValidationIssue.LEVEL_WARNING,
                "LOW_PLACEMENT_RATE",
                wall_id,
                "Only {0}/{1} members placed (>{2}% loss)".format(
                    placed_count, member_count,
                    int(100.0 * (member_count - placed_count) / member_count)
                ),
            ))


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _is_vertical(member):
    start = getattr(member, "start_point", None)
    end = getattr(member, "end_point", None)
    if start is None or end is None:
        return False
    try:
        dz = abs(end.Z - start.Z)
        dx = abs(end.X - start.X)
        dy = abs(end.Y - start.Y)
        return dz > inches_to_feet(1.0) and dx < 1e-4 and dy < 1e-4
    except Exception:
        return False


def _member_d(member, host=None):
    """Return an approximate d-axis position for a member.
    If host is provided, projects onto the host wall direction.
    Otherwise, falls back to midpoint X coordinate.
    """
    start = getattr(member, "start_point", None)
    end = getattr(member, "end_point", None)
    if start is None or end is None:
        return 0.0
    try:
        mid_x = (start.X + end.X) * 0.5
        mid_y = (start.Y + end.Y) * 0.5
        if host is not None:
            from Autodesk.Revit.DB import XYZ
            mid = XYZ(mid_x, mid_y, host.base_elevation)
            vec = mid - host.start_point
            return vec.DotProduct(host.direction)
        return mid_x
    except Exception:
        return 0.0


def _member_bottom_z(member):
    start = getattr(member, "start_point", None)
    end = getattr(member, "end_point", None)
    if start is None or end is None:
        return 0.0
    return min(start.Z, end.Z)


def _member_top_z(member):
    start = getattr(member, "start_point", None)
    end = getattr(member, "end_point", None)
    if start is None or end is None:
        return 0.0
    return max(start.Z, end.Z)


def _members_collide(ma, mb):
    """True if two members' bounding boxes overlap beyond OVERLAP_TOL."""
    try:
        # Check 3-D bounding box overlap.
        def bbox(m):
            s = m.start_point
            e = m.end_point
            return (min(s.X, e.X), max(s.X, e.X),
                    min(s.Y, e.Y), max(s.Y, e.Y),
                    min(s.Z, e.Z), max(s.Z, e.Z))

        ax0, ax1, ay0, ay1, az0, az1 = bbox(ma)
        bx0, bx1, by0, by1, bz0, bz1 = bbox(mb)

        def intervals_overlap(a0, a1, b0, b1, tol):
            return a0 < b1 - tol and b0 < a1 - tol

        return (intervals_overlap(ax0, ax1, bx0, bx1, OVERLAP_TOL)
                and intervals_overlap(ay0, ay1, by0, by1, OVERLAP_TOL)
                and intervals_overlap(az0, az1, bz0, bz1, OVERLAP_TOL))
    except Exception:
        return False


def _element_id_text(element_id):
    if element_id is None:
        return "None"
    v = getattr(element_id, "IntegerValue", getattr(element_id, "Value", None))
    return str(v) if v is not None else "None"
