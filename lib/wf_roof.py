# -*- coding: utf-8 -*-
"""Roof framing engine -- stick-frame rafters and truss placement.

Supports gable, hip, shed/mono-slope, dutch-gable, and flat roofs.

Stick-frame construction sequence (real-world order):
  1. Ridge board(s) -- the beam at the peak where rafters meet
  2. Common rafters -- sloping members from ridge to wall plate at OC spacing
  3. Collar ties -- horizontal ties connecting opposing rafters (gable)
  4. Ceiling joists -- horizontal members eave-to-eave at plate elevation
  5. Kickers / outriggers -- diagonal braces from ceiling joist to rafter
  6. Sub-fascia -- board along the LOW eave edge (NOT rake edges)
  7. Ledger / header beam -- the high-side connection on shed roofs

Edge classification:
  - Ridge edges: shared boundary between two sloped faces (highest Z)
  - Eave edges: parallel to ridge at LOWER elevation -- gets fascia
  - Rake edges: perpendicular to ridge (gable-end slope edges)
  - Ledger edges: parallel to ridge at HIGHER elevation (shed roof high side)
"""

import math
import re

from wf_geometry import FramingMember, inches_to_feet
from wf_config import LUMBER_ACTUAL
from wf_host import (
    PlanarHostInfo,
    _extract_face_loops,
    _face_normal,
    _scanline_intervals,
    _to_local,
    analyze_roof_host,
)
from wf_placement import BaseFramingEngine
from wf_tracking import get_tracking_data


MIN_MEMBER_LENGTH = inches_to_feet(1.0)
RIDGE_TOL = inches_to_feet(3.0)
EDGE_TOL = inches_to_feet(1.0)
COLLAR_TIE_FRACTION = 1.0 / 3.0
MAX_COLLAR_TIE_SPACING = inches_to_feet(48.0)
PLATE_THICKNESS = inches_to_feet(1.5)
KICKER_FRACTION = 0.25
PROFILE_MATCH_TOL = inches_to_feet(0.125)

# Only faces within ~1 degree of horizontal are considered flat.
FLAT_THRESHOLD = 0.9998

# Edges whose direction dot product with the ridge exceeds this are
# considered "parallel" (eave/ledger).  Below this = rake.
PARALLEL_DOT_THRESHOLD = 0.5


# ======================================================================
#  Helpers
# ======================================================================

def _sloped_planes(planes):
    return [p for p in planes if p.normal.Z < FLAT_THRESHOLD]

def _classify_roof(planes):
    """Classify roof shape from its analyzed planes."""
    if not planes:
        return "flat"
    sloped = _sloped_planes(planes)
    flat_p = [p for p in planes if p.normal.Z >= FLAT_THRESHOLD]
    if not sloped:
        return "flat"
    if len(sloped) == 1:
        return "shed"
    if len(sloped) == 2 and not flat_p:
        return "gable"
    if len(sloped) == 4 and not flat_p:
        return "hip"
    if len(sloped) >= 2 and flat_p:
        return "dutch"
    return "complex"


def _single_slope_support_status(planes):
    """Return whether the current roof can use the stable single-slope path."""
    sloped = _sloped_planes(planes)
    roof_type = _classify_roof(planes)

    if len(sloped) == 1:
        return True, None, roof_type

    if not sloped:
        return (
            False,
            "Single-Slope Roof Framing requires exactly one sloped roof plane. "
            "This roof has no sloped planes.",
            roof_type,
        )

    return (
        False,
        "Single-Slope Roof Framing requires exactly one sloped roof plane. "
        "This roof has {0} sloped planes ({1}).".format(
            len(sloped),
            roof_type,
        ),
        roof_type,
    )


def _pt_key(pt, decimals=4):
    return (round(pt.X, decimals), round(pt.Y, decimals),
            round(pt.Z, decimals))


def _dist(a, b):
    return a.DistanceTo(b)


def _normalize(v):
    if v is None:
        return None
    l = v.GetLength()
    if l < 1e-9:
        return None
    return v.Multiply(1.0 / l)


def _midpoint(a, b):
    from Autodesk.Revit.DB import XYZ
    return XYZ((a.X + b.X) / 2.0, (a.Y + b.Y) / 2.0, (a.Z + b.Z) / 2.0)


def _lerp(a, b, t):
    """Linear interpolation between two XYZ points."""
    from Autodesk.Revit.DB import XYZ
    return XYZ(
        a.X + t * (b.X - a.X),
        a.Y + t * (b.Y - a.Y),
        a.Z + t * (b.Z - a.Z),
    )


def _project_perpendicular(vector, axis):
    """Project a vector onto the plane perpendicular to the axis."""
    axis_unit = _normalize(axis)
    if axis_unit is None:
        return None
    return vector - axis_unit.Multiply(vector.DotProduct(axis_unit))


def _beam_reference_up(member_dir):
    """Return Revit's zero-rotation up vector for a beam-like member."""
    from Autodesk.Revit.DB import XYZ

    reference_up = _normalize(_project_perpendicular(XYZ.BasisZ, member_dir))
    if reference_up is not None:
        return reference_up
    return _normalize(_project_perpendicular(XYZ.BasisX, member_dir))


def _signed_angle_about(axis, start_vec, end_vec):
    """Return the signed angle from start_vec to end_vec about axis."""
    axis_unit = _normalize(axis)
    start_unit = _normalize(start_vec)
    end_unit = _normalize(end_vec)
    if axis_unit is None or start_unit is None or end_unit is None:
        return 0.0

    cross = start_unit.CrossProduct(end_unit)
    sin_value = axis_unit.DotProduct(cross)
    cos_value = max(-1.0, min(1.0, start_unit.DotProduct(end_unit)))
    return math.atan2(sin_value, cos_value)


def _rotation_from_up(member_dir, desired_up):
    """Convert a desired member up vector into Revit bend-direction rotation."""
    if desired_up is None:
        return 0.0

    reference_up = _beam_reference_up(member_dir)
    desired_up = _normalize(_project_perpendicular(desired_up, member_dir))
    if reference_up is None or desired_up is None:
        return 0.0
    return _signed_angle_about(member_dir, reference_up, desired_up)


# ======================================================================
#  Ridge / eave / rake detection
# ======================================================================

def _surface_point(plane, lx, ly):
    """World point on the face surface -- NO depth offset."""
    return (plane.origin
            + plane.x_axis.Multiply(lx)
            + plane.y_axis.Multiply(ly))


def _plane_point_at_depth(plane, lx, ly, depth_from_exterior):
    """World point on a plane parallel to the roof face at a given depth."""
    return (
        plane.origin
        + plane.x_axis.Multiply(lx)
        + plane.y_axis.Multiply(ly)
        + plane.normal.Multiply(-depth_from_exterior)
    )


def _points_near(first, second, tolerance=PROFILE_MATCH_TOL):
    """Check whether two XYZ points are within a small modeling tolerance."""
    try:
        return first.DistanceTo(second) <= tolerance
    except Exception:
        return False


def _same_segment(start_a, end_a, start_b, end_b, tolerance=PROFILE_MATCH_TOL):
    """Check whether two segments represent the same geometric edge."""
    return (
        (_points_near(start_a, start_b, tolerance)
         and _points_near(end_a, end_b, tolerance))
        or (_points_near(start_a, end_b, tolerance)
            and _points_near(end_a, start_b, tolerance))
    )


def _line_intersection_2d(line_a, line_b):
    """Return the intersection of two infinite 2D lines, or None."""
    (x1, y1), (x2, y2) = line_a
    (x3, y3), (x4, y4) = line_b

    denom = ((x1 - x2) * (y3 - y4)) - ((y1 - y2) * (x3 - x4))
    if abs(denom) < 1e-9:
        return None

    det_a = (x1 * y2) - (y1 * x2)
    det_b = (x3 * y4) - (y3 * x4)
    x = ((det_a * (x3 - x4)) - ((x1 - x2) * det_b)) / denom
    y = ((det_a * (y3 - y4)) - ((y1 - y2) * det_b)) / denom
    return (x, y)


def _segment_length(start_point, end_point):
    """Return segment length, or 0 on failure."""
    try:
        return (end_point - start_point).GetLength()
    except Exception:
        return 0.0


def _dist_point_to_segment_2d(p, s, e):
    import math
    dx = e[0] - s[0]
    dy = e[1] - s[1]
    l2 = dx*dx + dy*dy
    if l2 < 1e-8:
        return math.sqrt((p[0]-s[0])**2 + (p[1]-s[1])**2)
    t = ((p[0]-s[0])*dx + (p[1]-s[1])*dy) / l2
    t = max(0.0, min(1.0, t))
    proj = (s[0] + t*dx, s[1] + t*dy)
    return math.sqrt((p[0]-proj[0])**2 + (p[1]-proj[1])**2)


def _plane_centroid(plane):
    from Autodesk.Revit.DB import XYZ
    if not plane.outer_loop_local:
        return plane.origin
    pts = []
    for lx, ly in plane.outer_loop_local:
        pts.append(_surface_point(plane, lx, ly))
    if not pts:
        return plane.origin
    xs = [pt.X for pt in pts]
    ys = [pt.Y for pt in pts]
    zs = [pt.Z for pt in pts]
    return XYZ(sum(xs)/len(pts), sum(ys)/len(pts), sum(zs)/len(pts))


def _classify_shared_edge(rs, re, pa, pb):
    from Autodesk.Revit.DB import XYZ
    import math
    mid = _midpoint(rs, re)
    c_a = _plane_centroid(pa)
    c_b = _plane_centroid(pb)
    
    edge_vec = re - rs
    edge_len = edge_vec.GetLength()
    if edge_len < 1e-4:
        return "RIDGE_BOARD"
    E = edge_vec.Multiply(1.0 / edge_len)
    
    t_a = pa.normal.CrossProduct(E)
    if t_a.GetLength() > 1e-4:
        t_a = _normalize(t_a)
        if t_a.DotProduct(c_a - mid) < 0:
            t_a = t_a.Multiply(-1.0)
    else:
        t_a = XYZ(0, 0, 0)
        
    t_b = pb.normal.CrossProduct(E)
    if t_b.GetLength() > 1e-4:
        t_b = _normalize(t_b)
        if t_b.DotProduct(c_b - mid) < 0:
            t_b = t_b.Multiply(-1.0)
    else:
        t_b = XYZ(0, 0, 0)
        
    if t_a.Z > 0.001 or t_b.Z > 0.001:
        return "VALLEY_RAFTER"
        
    if abs(rs.Z - re.Z) < 0.15:
        return "RIDGE_BOARD"
    else:
        return "HIP_RAFTER"


def _find_ridge_edges(planes):
    """Find ridge edges -- shared boundary between two sloped faces.

    Returns list of (start_xyz, end_xyz, plane_a, plane_b).
    """
    sloped = [p for p in planes if p.normal.Z < FLAT_THRESHOLD]
    if len(sloped) < 2:
        return []

    all_edges = []
    for plane in sloped:
        for loop in plane.boundary_loops_local:
            count = len(loop)
            for i in range(count):
                s_local = loop[i]
                e_local = loop[(i + 1) % count]
                s_world = _surface_point(plane, s_local[0], s_local[1])
                e_world = _surface_point(plane, e_local[0], e_local[1])
                all_edges.append((s_world, e_world, plane))

    ridges = []
    used = set()
    for i in range(len(all_edges)):
        if i in used:
            continue
        s1, e1, p1 = all_edges[i]
        for j in range(i + 1, len(all_edges)):
            if j in used:
                continue
            s2, e2, p2 = all_edges[j]
            if p1 is p2:
                continue
            match = False
            if _dist(s1, s2) < EDGE_TOL and _dist(e1, e2) < EDGE_TOL:
                match = True
            elif _dist(s1, e2) < EDGE_TOL and _dist(e1, s2) < EDGE_TOL:
                match = True
            if match:
                avg_z = (s1.Z + e1.Z) / 2.0
                ridges.append((s1, e1, p1, p2, avg_z))
                used.add(i)
                used.add(j)
                break

    if not ridges:
        return []

    # Keep all valid ridge segments (not only the top-most by Z).
    unique = []
    seen = set()
    for s, e, pa, pb, z in ridges:
        key = (_pt_key(s), _pt_key(e))
        rkey = (_pt_key(e), _pt_key(s))
        if key in seen or rkey in seen:
            continue
        seen.add(key)
        unique.append((s, e, pa, pb))

    unique.sort(
        key=lambda r: (((r[0].Z + r[1].Z) / 2.0), _dist(r[0], r[1])),
        reverse=True,
    )
    return unique


def _get_non_ridge_edges(plane, ridge_edges):
    """Return all boundary edges of a plane that are NOT ridge edges."""
    ridge_set = set()
    for rs, re, pa, pb in ridge_edges:
        ridge_set.add((_pt_key(rs), _pt_key(re)))
        ridge_set.add((_pt_key(re), _pt_key(rs)))

    edges = []
    for loop in plane.boundary_loops_local:
        count = len(loop)
        for i in range(count):
            s_local = loop[i]
            e_local = loop[(i + 1) % count]
            s_world = _surface_point(plane, s_local[0], s_local[1])
            e_world = _surface_point(plane, e_local[0], e_local[1])
            sk = _pt_key(s_world)
            ek = _pt_key(e_world)
            if (sk, ek) not in ridge_set:
                edges.append((s_world, e_world))
    return edges


def _classify_boundary_edges(plane, ridge_edges, ridge_segment=None):
    """Classify non-ridge boundary edges into eave, rake, and ledger.

    - ridge_dir: direction along the ridge (or plane.x_axis for sheds)
    - Eave: parallel to ridge AND at lower Z  --> fascia goes here
    - Ledger: parallel to ridge AND at higher Z  --> ledger beam (shed)
    - Rake: perpendicular to ridge  --> barge/rake board

    Returns dict with keys 'eave', 'rake', 'ledger', each a list of
    (start_xyz, end_xyz) tuples.
    """
    non_ridge = _get_non_ridge_edges(plane, ridge_edges)
    if not non_ridge:
        return {"eave": [], "rake": [], "ledger": []}

    # Determine ridge direction for this plane
    if ridge_segment is not None:
        rs, re = ridge_segment
        ridge_dir = _normalize(re - rs)
    else:
        plane_ridges = [(rs, re) for rs, re, pa, pb in ridge_edges
                        if pa is plane or pb is plane]
        if plane_ridges:
            plane_ridges.sort(
                key=lambda edge: _dist(edge[0], edge[1]),
                reverse=True,
            )
            rs, re = plane_ridges[0]
            ridge_dir = _normalize(re - rs)
        else:
            # Shed / no ridge: use plane x_axis (runs along the "ridge" direction)
            ridge_dir = _normalize(plane.x_axis)

    if ridge_dir is None:
        return {"eave": non_ridge, "rake": [], "ledger": []}

    # Separate parallel vs perpendicular edges
    parallel = []  # (start, end, avg_z)
    perpendicular = []
    for s, e in non_ridge:
        edge_dir = _normalize(e - s)
        if edge_dir is None:
            continue
        dot = abs(edge_dir.DotProduct(ridge_dir))
        avg_z = (s.Z + e.Z) / 2.0
        if dot >= PARALLEL_DOT_THRESHOLD:
            parallel.append((s, e, avg_z))
        else:
            perpendicular.append((s, e))

    # Split parallel edges into low (eave) and high (ledger)
    eave = []
    ledger = []
    if parallel:
        z_vals = [z for _, _, z in parallel]
        z_min = min(z_vals)
        z_max = max(z_vals)
        z_mid = (z_min + z_max) / 2.0

        if z_max - z_min < EDGE_TOL:
            # All at same elevation -- all are eaves (flat case)
            eave = [(s, e) for s, e, _ in parallel]
        else:
            for s, e, z in parallel:
                if z <= z_mid:
                    eave.append((s, e))
                else:
                    ledger.append((s, e))

    return {"eave": eave, "rake": perpendicular, "ledger": ledger}


def _classify_profile_boundary_edges(
    plane,
    loops_local,
    depth_from_exterior,
    ridge_edges=None,
    ridge_segment=None,
):
    """Classify boundary edges from an explicit roof profile loop set."""
    if not loops_local:
        return {"eave": [], "rake": [], "ledger": []}

    if ridge_segment is not None:
        rs, re = ridge_segment
        ridge_dir = _normalize(re - rs)
    else:
        ridge_dir = None
        if ridge_edges:
            plane_ridges = [(rs, re) for rs, re, pa, pb in ridge_edges
                            if pa is plane or pb is plane]
            if plane_ridges:
                plane_ridges.sort(
                    key=lambda edge: _dist(edge[0], edge[1]),
                    reverse=True,
                )
                rs, re = plane_ridges[0]
                ridge_dir = _normalize(re - rs)
        if ridge_dir is None:
            ridge_dir = _normalize(plane.x_axis)

    if ridge_dir is None:
        return {"eave": [], "rake": [], "ledger": []}

    parallel = []
    perpendicular = []
    for loop in loops_local:
        count = len(loop)
        for index in range(count):
            start_local = loop[index]
            end_local = loop[(index + 1) % count]
            start_world = _plane_point_at_depth(
                plane,
                start_local[0],
                start_local[1],
                depth_from_exterior,
            )
            end_world = _plane_point_at_depth(
                plane,
                end_local[0],
                end_local[1],
                depth_from_exterior,
            )
            edge_dir = _normalize(end_world - start_world)
            if edge_dir is None:
                continue
            dot = abs(edge_dir.DotProduct(ridge_dir))
            avg_z = (start_world.Z + end_world.Z) / 2.0
            if dot >= PARALLEL_DOT_THRESHOLD:
                parallel.append((start_world, end_world, avg_z))
            else:
                perpendicular.append((start_world, end_world))

    eave = []
    ledger = []
    if parallel:
        z_vals = [z for _, _, z in parallel]
        z_min = min(z_vals)
        z_max = max(z_vals)
        z_mid = (z_min + z_max) / 2.0

        if z_max - z_min < EDGE_TOL:
            eave = [(s, e) for s, e, _ in parallel]
        else:
            for s, e, z in parallel:
                if z <= z_mid:
                    eave.append((s, e))
                else:
                    ledger.append((s, e))

    return {"eave": eave, "rake": perpendicular, "ledger": ledger}


def _lowest_eave_z(eave_edges):
    """Return the lowest Z among all eave edge endpoints."""
    if not eave_edges:
        return None
    z_vals = []
    for s, e in eave_edges:
        z_vals.append(s.Z)
        z_vals.append(e.Z)
    return min(z_vals) if z_vals else None


def _rafter_positions(eave_start, eave_end, spacing):
    """Generate OC positions along an eave edge."""
    edge_len = _dist(eave_start, eave_end)
    if edge_len < MIN_MEMBER_LENGTH or spacing <= 0:
        return []
    positions = []
    d = 0.0
    while d <= edge_len + 1e-9:
        t = min(d / edge_len, 1.0)
        positions.append(t)
        d += spacing
    if abs(positions[-1] - 1.0) > 1e-6:
        positions.append(1.0)
    return positions


def _project_to_ridge(pt, ridge_start, ridge_end):
    """Project a point onto a ridge line."""
    ridge_dir = ridge_end - ridge_start
    ridge_len = ridge_dir.GetLength()
    if ridge_len < 1e-9:
        return ridge_start
    ridge_dir = ridge_dir.Multiply(1.0 / ridge_len)
    t = (pt - ridge_start).DotProduct(ridge_dir)
    t = max(0.0, min(ridge_len, t))
    return ridge_start + ridge_dir.Multiply(t)


def _project_to_edge(pt, edge_start, edge_end):
    """Project a point onto an edge segment and report whether clamping was needed."""
    edge_dir = edge_end - edge_start
    edge_len = edge_dir.GetLength()
    if edge_len < 1e-9:
        return edge_start, False, 0.0

    edge_unit = edge_dir.Multiply(1.0 / edge_len)
    raw_t = (pt - edge_start).DotProduct(edge_unit)
    clamped_t = max(0.0, min(edge_len, raw_t))
    was_clamped = abs(clamped_t - raw_t) > 1e-9
    return edge_start + edge_unit.Multiply(clamped_t), (not was_clamped), raw_t


def _ridge_station_on_segment(point, ridge_start, ridge_end):
    """Return the station of a point along a ridge segment."""
    ridge_dir = ridge_end - ridge_start
    ridge_len = ridge_dir.GetLength()
    if ridge_len < 1e-9:
        return 0.0
    ridge_unit = ridge_dir.Multiply(1.0 / ridge_len)
    return (point - ridge_start).DotProduct(ridge_unit)


def _segment_covers_ridge_station(edge_start, edge_end, ridge_start, ridge_end, ridge_station):
    """Return True when an edge spans the current ridge station."""
    edge_station_start = _ridge_station_on_segment(edge_start, ridge_start, ridge_end)
    edge_station_end = _ridge_station_on_segment(edge_end, ridge_start, ridge_end)
    station_min = min(edge_station_start, edge_station_end)
    station_max = max(edge_station_start, edge_station_end)
    return (station_min - EDGE_TOL) <= ridge_station <= (station_max + EDGE_TOL)


def _project_to_best_eave(ridge_pt, eave_edges, ridge_start=None, ridge_end=None):
    """Project a ridge point to the nearest valid eave edge."""
    best_pt = None
    best_covers_station = False
    best_interior = False
    best_dist = None
    ridge_station = None
    if ridge_start is not None and ridge_end is not None:
        ridge_station = _ridge_station_on_segment(ridge_pt, ridge_start, ridge_end)
    for eave_s, eave_e in eave_edges:
        cand, is_interior, _ = _project_to_edge(ridge_pt, eave_s, eave_e)
        covers_station = False
        if ridge_station is not None:
            covers_station = _segment_covers_ridge_station(
                eave_s,
                eave_e,
                ridge_start,
                ridge_end,
                ridge_station,
            )
        d = _dist(ridge_pt, cand)
        if best_pt is None:
            best_pt = cand
            best_covers_station = covers_station
            best_interior = is_interior
            best_dist = d
            continue

        if covers_station and not best_covers_station:
            best_pt = cand
            best_covers_station = True
            best_interior = is_interior
            best_dist = d
            continue

        if is_interior and not best_interior:
            best_pt = cand
            best_covers_station = covers_station
            best_interior = True
            best_dist = d
            continue

        if (covers_station == best_covers_station
                and is_interior == best_interior
                and d < best_dist):
            best_pt = cand
            best_covers_station = covers_station
            best_interior = is_interior
            best_dist = d
    return best_pt


def _project_to_infinite_edge(pt, edge_start, edge_end):
    """Project a point onto the infinite line containing the edge segment."""
    edge_dir = edge_end - edge_start
    edge_len = edge_dir.GetLength()
    if edge_len < 1e-9:
        return edge_start
    edge_unit = edge_dir.Multiply(1.0 / edge_len)
    raw_t = (pt - edge_start).DotProduct(edge_unit)
    return edge_start + edge_unit.Multiply(raw_t)


def _project_to_infinite_eave(ridge_pt, eave_edges):
    """Project a point onto the nearest infinite line among eave edges."""
    best_pt = None
    best_dist = 1e9
    for s, e in eave_edges:
        cand = _project_to_infinite_edge(ridge_pt, s, e)
        d = _dist(ridge_pt, cand)
        if d < best_dist:
            best_dist = d
            best_pt = cand
    return best_pt


# ======================================================================
#  Engine
# ======================================================================

class RoofFramingEngine(BaseFramingEngine):
    """Calculates and places roof framing -- stick-frame or truss."""

    def place_members(self, members, host_info):
        """Place roof members and apply roof-specific post-processing."""
        placed = BaseFramingEngine.place_members(self, members, host_info)

        try:
            self._set_coping_distance_zero(placed)
        except Exception:
            pass

        if placed:
            try:
                self.doc.Regenerate()
            except Exception:
                pass

        try:
            self._apply_automatic_coping(placed)
        except Exception:
            pass

        return placed

    @staticmethod
    def _set_coping_distance_zero(instances):
        """Set coping distance to zero on roof framing when the parameter exists."""
        try:
            from Autodesk.Revit.DB import BuiltInParameter
        except Exception:
            BuiltInParameter = None

        if BuiltInParameter is None:
            return

        for instance in instances or []:
            try:
                parameter = instance.get_Parameter(
                    BuiltInParameter.STRUCTURAL_COPING_DISTANCE,
                )
            except Exception:
                parameter = None
            if parameter is None:
                continue
            try:
                if not parameter.IsReadOnly:
                    parameter.Set(0.0)
            except Exception:
                pass

    def calculate_members(self, roof, mode="stick"):
        """Calculate framing members for a roof.

        Args:
            roof: Revit RoofBase element.
            mode: "stick" for rafter framing, "truss" for truss placement.

        Returns:
            (members_list, roof_info) or ([], None) on failure.
        """
        roof_info = analyze_roof_host(self.doc, roof, self.config)
        if roof_info is None:
            return [], None
        is_supported, support_reason, roof_type = _single_slope_support_status(
            getattr(roof_info, "planes", []) or []
        )
        try:
            roof_info.roof_type = roof_type
            roof_info.single_slope_supported = is_supported
            roof_info.single_slope_support_reason = support_reason
        except Exception:
            pass
        if not is_supported and mode != "truss":
            return [], roof_info
        if mode == "truss":
            members = self._calc_truss_positions(roof_info)
        else:
            members = self._calc_stick_frame(roof_info)
        return members, roof_info

    def _apply_automatic_coping(self, placed_instances):
        """Best-effort coping between newly placed rafters and perimeter boards."""
        if not placed_instances:
            return

        rafters = []
        boards = []
        member_pairs = getattr(self, "_last_placed_pairs", None) or []

        if member_pairs:
            for member, instance in member_pairs:
                member_type = getattr(member, "member_type", None)
                if member_type == "RAFTER":
                    rafters.append(instance)
                elif member_type in ("FASCIA", "LEDGER", "RIDGE_BOARD", "HIP_RAFTER", "VALLEY_RAFTER"):
                    boards.append(instance)
        else:
            for instance in placed_instances:
                tracking = get_tracking_data(instance)
                if tracking is None:
                    continue
                member_type = tracking.get("member")
                if member_type == "RAFTER":
                    rafters.append(instance)
                elif member_type in ("FASCIA", "LEDGER", "RIDGE_BOARD", "HIP_RAFTER", "VALLEY_RAFTER"):
                    boards.append(instance)

        if not rafters or not boards:
            return

        for rafter in rafters:
            add_coping = getattr(rafter, "AddCoping", None)
            if add_coping is None:
                continue
            for board in boards:
                if not self._elements_are_near(rafter, board):
                    continue
                try:
                    add_coping(board)
                except Exception:
                    pass

    @staticmethod
    def _elements_are_near(first, second, tolerance=0.25):
        """Return True when two elements' bounding boxes overlap or nearly touch."""
        first_box = first.get_BoundingBox(None)
        second_box = second.get_BoundingBox(None)
        if first_box is None or second_box is None:
            return False

        return not (
            (first_box.Max.X + tolerance) < second_box.Min.X
            or (second_box.Max.X + tolerance) < first_box.Min.X
            or (first_box.Max.Y + tolerance) < second_box.Min.Y
            or (second_box.Max.Y + tolerance) < first_box.Min.Y
            or (first_box.Max.Z + tolerance) < second_box.Min.Z
            or (second_box.Max.Z + tolerance) < first_box.Min.Z
        )

    # ------------------------------------------------------------------
    #  Stick framing
    # ------------------------------------------------------------------

    def _calc_stick_frame(self, roof_info):
        members = []
        planes = roof_info.planes
        roof_type = _classify_roof(planes)
        spacing = self.config.stud_spacing_ft
        if spacing <= 0:
            return members

        # 1. Ridge detection
        try:
            ridge_edges = _find_ridge_edges(planes)
        except Exception:
            ridge_edges = []

        members.extend(self._make_ridge_boards(ridge_edges, roof_info))

        # 2. Rafters per slope + classify edges
        all_eave_edges = []
        all_rake_edges = []
        all_ledger_edges = []
        for plane in planes:
            if plane.normal.Z >= FLAT_THRESHOLD:
                continue

            # Classify this plane's boundary edges
            try:
                edge_depth = 0.0
                classified = _classify_boundary_edges(plane, ridge_edges)
                if roof_type == "shed":
                    edge_depth = self._resolve_roof_layer_top_depth(plane)
                    profile_loops = self._roof_profile_loops_local(
                        plane,
                        edge_depth,
                    )
                    if profile_loops:
                        classified = _classify_profile_boundary_edges(
                            plane,
                            profile_loops,
                            edge_depth,
                            ridge_edges,
                        )
                all_eave_edges.extend(
                    (edge_start, edge_end, plane, "eave", edge_depth)
                    for edge_start, edge_end in classified["eave"]
                )
                all_rake_edges.extend(
                    (edge_start, edge_end, plane, "rake", edge_depth)
                    for edge_start, edge_end in classified["rake"]
                )
                all_ledger_edges.extend(
                    (edge_start, edge_end, plane, "ledger", edge_depth)
                    for edge_start, edge_end in classified["ledger"]
                )
            except Exception:
                pass

            # Place rafters: ridged roofs prefer ridge/eave-controlled axes.
            rafters = []
            if ridge_edges and roof_type != "shed":
                try:
                    rafters = self._make_rafters_for_plane(
                        plane, ridge_edges, roof_info)
                except Exception:
                    rafters = []
                if not rafters:
                    try:
                        rafters = self._make_rafters_scanline(
                            plane, spacing, roof_info)
                    except Exception:
                        rafters = []
            else:
                try:
                    rafters = self._make_rafters_scanline(
                        plane, spacing, roof_info)
                except Exception:
                    rafters = []
                if not rafters and ridge_edges:
                    try:
                        rafters = self._make_rafters_for_plane(
                            plane, ridge_edges, roof_info)
                    except Exception:
                        rafters = []
            members.extend(rafters)

        # 3. Collar ties per ridge segment.
        if (
            ridge_edges
            and bool(getattr(self.config, "include_collar_ties", True))
        ):
            try:
                members.extend(
                    self._make_collar_ties(planes, ridge_edges, roof_info))
            except Exception:
                pass

        # 4. Ceiling joists + kickers per ridge segment.
        if (
            ridge_edges
            and bool(getattr(self.config, "include_ceiling_joists", True))
        ):
            try:
                members.extend(
                    self._make_ceiling_joists(
                        planes,
                        ridge_edges,
                        roof_info,
                        spacing,
                        bool(getattr(self.config, "include_roof_kickers", True)),
                    )
                )
            except Exception:
                pass

        # 5. Shed roofs need border members along the low eave and rakes.
        fascia_edges = list(all_eave_edges)
        if roof_type == "shed":
            fascia_edges.extend(all_rake_edges)
        try:
            members.extend(
                self._make_fascia(fascia_edges, roof_info))
        except Exception:
            pass

        # 6. Ledger beam at high side (shed roofs)
        if roof_type == "shed" and all_ledger_edges:
            try:
                members.extend(
                    self._make_ledger(all_ledger_edges, roof_info))
            except Exception:
                pass

        return members

    # ------------------------------------------------------------------
    #  Ridge boards
    # ------------------------------------------------------------------

    def _make_ridge_boards(self, ridge_edges, roof_info):
        from Autodesk.Revit.DB import XYZ
        members = []
        seen = set()
        for rs, re, pa, pb in ridge_edges:
            if _dist(rs, re) < MIN_MEMBER_LENGTH:
                continue
            if pa.normal.DotProduct(pb.normal) > 0.999:
                continue
            key = (_pt_key(rs), _pt_key(re))
            rkey = (_pt_key(re), _pt_key(rs))
            if key in seen or rkey in seen:
                continue
            seen.add(key)
            
            # Classify edge
            edge_type = _classify_shared_edge(rs, re, pa, pb)
            
            m = FramingMember(FramingMember.HEADER, rs, re)
            m.member_type = edge_type
            m.family_name = (
                self.config.header_family_name or self.config.stud_family_name)
            m.type_name = (
                self.config.header_type_name or self.config.stud_type_name)
            
            # Resolve member depth
            _, member_depth = self._resolve_roof_member_size(m.family_name, m.type_name)
            if member_depth is None or member_depth <= 0.0:
                member_depth = 7.25 / 12.0
                
            # Shift vertically down to sit under sheathing
            depth_a = self._resolve_roof_layer_top_depth(pa)
            depth_b = self._resolve_roof_layer_top_depth(pb)
            depth_sync = max(depth_a, depth_b)
            shift_z = depth_sync + member_depth / 2.0
            
            m.start_point = rs - XYZ(0, 0, shift_z)
            m.end_point = re - XYZ(0, 0, shift_z)
            m.rotation = 0.0
            m.disallow_end_joins = True
            m.host_kind = roof_info.kind
            m.host_id = roof_info.element_id
            members.append(m)
        return members

    # ------------------------------------------------------------------
    #  Rafters
    # ------------------------------------------------------------------

    def _make_rafters_for_plane(self, plane, ridge_edges, roof_info):
        from Autodesk.Revit.DB import XYZ
        import math

        members = []
        spacing = self.config.stud_spacing_ft
        if spacing <= 0:
            return members

        # 1. Build a map of boundary segments to their classifications
        segment_classes = {}
        
        # Look at shared edges (ridges, hips, valleys)
        for rs, re, pa, pb in ridge_edges:
            if pa is plane or pb is plane:
                edge_type = _classify_shared_edge(rs, re, pa, pb)
                k1 = (_pt_key(rs), _pt_key(re))
                k2 = (_pt_key(re), _pt_key(rs))
                segment_classes[k1] = (edge_type, pb if pa is plane else pa, rs, re)
                segment_classes[k2] = (edge_type, pb if pa is plane else pa, re, rs)
                
        # Look at non-shared boundary edges
        classified = _classify_boundary_edges(plane, ridge_edges)
        for edge_start, edge_end in classified["eave"]:
            k1 = (_pt_key(edge_start), _pt_key(edge_end))
            k2 = (_pt_key(edge_end), _pt_key(edge_start))
            segment_classes[k1] = ("EAVE", None, edge_start, edge_end)
            segment_classes[k2] = ("EAVE", None, edge_end, edge_start)
            
        for edge_start, edge_end in classified["rake"]:
            k1 = (_pt_key(edge_start), _pt_key(edge_end))
            k2 = (_pt_key(edge_end), _pt_key(edge_start))
            segment_classes[k1] = ("RAKE", None, edge_start, edge_end)
            segment_classes[k2] = ("RAKE", None, edge_end, edge_start)
            
        for edge_start, edge_end in classified["ledger"]:
            k1 = (_pt_key(edge_start), _pt_key(edge_end))
            k2 = (_pt_key(edge_end), _pt_key(edge_start))
            segment_classes[k1] = ("LEDGER", None, edge_start, edge_end)
            segment_classes[k2] = ("LEDGER", None, edge_end, edge_start)

        # Helper to find boundary segment class for a 2D local point
        def find_boundary_segment_class(pt_local):
            best_segment = None
            min_d = 1e5
            for loop in plane.boundary_loops_local:
                count = len(loop)
                for i in range(count):
                    s_loc = loop[i]
                    e_loc = loop[(i + 1) % count]
                    d = _dist_point_to_segment_2d(pt_local, s_loc, e_loc)
                    if d < min_d:
                        min_d = d
                        best_segment = (s_loc, e_loc)
            if best_segment is None or min_d > 0.05:
                return "RAKE", None, None, None
                
            s_loc, e_loc = best_segment
            sw = _surface_point(plane, s_loc[0], s_loc[1])
            ew = _surface_point(plane, e_loc[0], e_loc[1])
            
            k = (_pt_key(sw), _pt_key(ew))
            if k in segment_classes:
                return segment_classes[k]
            rk = (_pt_key(ew), _pt_key(sw))
            if rk in segment_classes:
                return segment_classes[rk]
                
            best_match = ("RAKE", None, sw, ew)
            min_wd = 0.5
            for (sk, ek), val in segment_classes.items():
                sw_c, ew_c = val[2], val[3]
                d = _dist(sw, sw_c) + _dist(ew, ew_c)
                rd = _dist(sw, ew_c) + _dist(ew, sw_c)
                if d < min_wd:
                    min_wd = d
                    best_match = val
                if rd < min_wd:
                    min_wd = rd
                    best_match = val
            return best_match

        # Get local bounds of the plane
        pts_local = [pt for loop in plane.boundary_loops_local for pt in loop]
        if not pts_local:
            return members
            
        min_x = min(pt[0] for pt in pts_local)
        max_x = max(pt[0] for pt in pts_local)
        
        # Calculate global spacing reference to align rafters on opposing planes
        ref_local_x = (XYZ(0, 0, 0) - plane.origin).DotProduct(plane.x_axis)
        k_start = int(math.ceil((min_x - ref_local_x - 1e-4) / spacing))
        k_end = int(math.floor((max_x - ref_local_x + 1e-4) / spacing))
        
        seen = set()
        
        # Rafter dimensions
        rafter_family = self.config.stud_family_name
        rafter_type = self.config.stud_type_name
        rafter_width, rafter_depth = self._resolve_roof_member_size(rafter_family, rafter_type)
        if rafter_width is None or rafter_width <= 0.0:
            rafter_width = 1.5 / 12.0
        if rafter_depth is None or rafter_depth <= 0.0:
            rafter_depth = 5.5 / 12.0
            
        control_depth = self._resolve_roof_member_center_depth(plane, rafter_family, rafter_type)

        for k in range(k_start, k_end + 1):
            x = ref_local_x + k * spacing
            intervals = _scanline_intervals(plane.boundary_loops_local, "x", x)
            for start_y, end_y in intervals:
                if end_y - start_y < MIN_MEMBER_LENGTH:
                    continue
                    
                pt_upper_local = (x, start_y)
                pt_lower_local = (x, end_y)
                
                pt_upper_world = _surface_point(plane, x, start_y)
                pt_lower_world = _surface_point(plane, x, end_y)
                
                rafter_dir_world = pt_upper_world - pt_lower_world
                rafter_len = rafter_dir_world.GetLength()
                if rafter_len < MIN_MEMBER_LENGTH:
                    continue
                U_rafter = rafter_dir_world.Multiply(1.0 / rafter_len)
                
                # Retrieve boundary classes
                upper_class, opp_plane_upper, sw_up, ew_up = find_boundary_segment_class(pt_upper_local)
                lower_class, opp_plane_lower, sw_lo, ew_lo = find_boundary_segment_class(pt_lower_local)
                
                # 3. Process upper point
                if upper_class == "RIDGE_BOARD":
                    opposing_plane = opp_plane_upper
                    depth_A = control_depth
                    cos_theta_A = max(0.1, plane.normal.Z)
                    sin_theta_A = math.sqrt(max(0.0, 1.0 - cos_theta_A * cos_theta_A))
                    
                    ridge_family = self.config.header_family_name or self.config.stud_family_name
                    ridge_type = self.config.header_type_name or self.config.stud_type_name
                    ridge_width, _ = self._resolve_roof_member_size(ridge_family, ridge_type)
                    if ridge_width is None or ridge_width <= 0.0:
                        ridge_width = 1.5 / 12.0
                        
                    V_A = (depth_A + 0.5 * ridge_width * sin_theta_A) / cos_theta_A
                    
                    if opposing_plane is not None and opposing_plane.normal.Z < FLAT_THRESHOLD:
                        depth_opp = self._resolve_roof_member_center_depth(
                            opposing_plane, rafter_family, rafter_type
                        )
                        cos_theta_opp = max(0.1, opposing_plane.normal.Z)
                        sin_theta_opp = math.sqrt(max(0.0, 1.0 - cos_theta_opp * cos_theta_opp))
                        V_opp = (depth_opp + 0.5 * ridge_width * sin_theta_opp) / cos_theta_opp
                    else:
                        V_opp = 0.0
                        
                    V_sync = max(V_A, V_opp)
                    
                    slope_dir_xy = XYZ(plane.y_axis.X, plane.y_axis.Y, 0.0)
                    u_A = _normalize(slope_dir_xy)
                    if u_A is None:
                        u_A = XYZ(0, 0, 0)
                        
                    shift_vec = u_A.Multiply(0.5 * ridge_width) - XYZ(0, 0, V_sync)
                    rafter_end = pt_upper_world + shift_vec
                    
                elif upper_class in ("HIP_RAFTER", "VALLEY_RAFTER"):
                    pt_shifted = pt_upper_world - control_depth * plane.normal
                    hip_family = self.config.header_family_name or self.config.stud_family_name
                    hip_type = self.config.header_type_name or self.config.stud_type_name
                    hip_width, _ = self._resolve_roof_member_size(hip_family, hip_type)
                    if hip_width is None or hip_width <= 0.0:
                        hip_width = 1.5 / 12.0
                        
                    U_hip = _normalize(ew_up - sw_up)
                    if U_hip is not None:
                        cos_phi = abs(U_rafter.DotProduct(U_hip))
                        sin_phi = math.sqrt(max(0.1, 1.0 - cos_phi * cos_phi))
                        d_cutback = (0.5 * hip_width) / sin_phi
                        rafter_end = pt_shifted - U_rafter.Multiply(d_cutback)
                    else:
                        rafter_end = pt_shifted
                else:
                    rafter_end = pt_upper_world - control_depth * plane.normal

                # 4. Process lower point
                if lower_class == "RIDGE_BOARD":
                    opposing_plane = opp_plane_lower
                    depth_A = control_depth
                    cos_theta_A = max(0.1, plane.normal.Z)
                    sin_theta_A = math.sqrt(max(0.0, 1.0 - cos_theta_A * cos_theta_A))
                    
                    ridge_family = self.config.header_family_name or self.config.stud_family_name
                    ridge_type = self.config.header_type_name or self.config.stud_type_name
                    ridge_width, _ = self._resolve_roof_member_size(ridge_family, ridge_type)
                    if ridge_width is None or ridge_width <= 0.0:
                        ridge_width = 1.5 / 12.0
                        
                    V_A = (depth_A + 0.5 * ridge_width * sin_theta_A) / cos_theta_A
                    
                    if opposing_plane is not None and opposing_plane.normal.Z < FLAT_THRESHOLD:
                        depth_opp = self._resolve_roof_member_center_depth(
                            opposing_plane, rafter_family, rafter_type
                        )
                        cos_theta_opp = max(0.1, opposing_plane.normal.Z)
                        sin_theta_opp = math.sqrt(max(0.0, 1.0 - cos_theta_opp * cos_theta_opp))
                        V_opp = (depth_opp + 0.5 * ridge_width * sin_theta_opp) / cos_theta_opp
                    else:
                        V_opp = 0.0
                        
                    V_sync = max(V_A, V_opp)
                    
                    slope_dir_xy = XYZ(plane.y_axis.X, plane.y_axis.Y, 0.0)
                    u_A = _normalize(slope_dir_xy)
                    if u_A is None:
                        u_A = XYZ(0, 0, 0)
                        
                    shift_vec = u_A.Multiply(0.5 * ridge_width) - XYZ(0, 0, V_sync)
                    rafter_start = pt_lower_world + shift_vec
                    
                elif lower_class in ("HIP_RAFTER", "VALLEY_RAFTER"):
                    pt_shifted = pt_lower_world - control_depth * plane.normal
                    hip_family = self.config.header_family_name or self.config.stud_family_name
                    hip_type = self.config.header_type_name or self.config.stud_type_name
                    hip_width, _ = self._resolve_roof_member_size(hip_family, hip_type)
                    if hip_width is None or hip_width <= 0.0:
                        hip_width = 1.5 / 12.0
                        
                    U_hip = _normalize(ew_lo - sw_lo)
                    if U_hip is not None:
                        cos_phi = abs(U_rafter.DotProduct(U_hip))
                        sin_phi = math.sqrt(max(0.1, 1.0 - cos_phi * cos_phi))
                        d_cutback = (0.5 * hip_width) / sin_phi
                        rafter_start = pt_shifted + U_rafter.Multiply(d_cutback)
                    else:
                        rafter_start = pt_shifted
                else:
                    rafter_start = pt_lower_world - control_depth * plane.normal

                if _dist(rafter_start, rafter_end) < MIN_MEMBER_LENGTH:
                    continue
                    
                key = (_pt_key(rafter_start), _pt_key(rafter_end))
                rkey = (_pt_key(rafter_end), _pt_key(rafter_start))
                if key in seen or rkey in seen:
                    continue
                seen.add(key)
                
                m = FramingMember(FramingMember.STUD, rafter_start, rafter_end)
                m.member_type = "RAFTER"
                m.family_name = rafter_family
                m.type_name = rafter_type
                m.rotation = _rotation_from_up(rafter_end - rafter_start, plane.normal)
                m.disallow_end_joins = True
                m.host_kind = plane.kind
                m.host_id = plane.element_id
                members.append(m)
                
        return members

    def _make_rafters_scanline(self, plane, spacing, roof_info):
        """Fallback for shed / flat roofs with no ridge."""
        members = []
        family_name = self.config.stud_family_name
        type_name = self.config.stud_type_name
        control_depth = self._resolve_roof_member_center_depth(
            plane,
            family_name,
            type_name,
        )
        profile_loops = self._roof_profile_loops_local(plane, control_depth)
        if not profile_loops:
            return members

        points = [point for loop in profile_loops for point in loop]
        if not points:
            return members

        min_x = min(point[0] for point in points)
        max_x = max(point[0] for point in points)
        min_y = min(point[1] for point in points)
        max_y = max(point[1] for point in points)
        if max_y - min_y < MIN_MEMBER_LENGTH:
            return members

        x = min_x
        while x <= max_x + 1e-9:
            intervals = _scanline_intervals(profile_loops, "x", x)
            for start_y, end_y in intervals:
                if end_y - start_y < MIN_MEMBER_LENGTH:
                    continue
                s_pt = _plane_point_at_depth(plane, x, start_y, control_depth)
                e_pt = _plane_point_at_depth(plane, x, end_y, control_depth)
                original_length = _dist(s_pt, e_pt)
                clipped = self._clip_member_axis_to_roof(plane.element, s_pt, e_pt)
                if clipped is not None:
                    clipped_length = _dist(clipped[0], clipped[1])
                    if clipped_length <= original_length + 1e-6:
                        s_pt, e_pt = clipped
                if _dist(s_pt, e_pt) < MIN_MEMBER_LENGTH:
                    continue
                m = FramingMember(FramingMember.STUD, s_pt, e_pt)
                m.member_type = "RAFTER"
                m.family_name = self.config.stud_family_name
                m.type_name = self.config.stud_type_name
                m.rotation = _rotation_from_up(e_pt - s_pt, plane.normal)
                m.disallow_end_joins = True
                m.host_kind = plane.kind
                m.host_id = plane.element_id
                members.append(m)
            x += spacing
        return members

    def _resolve_roof_member_center_depth(self, plane, family_name, type_name):
        """Return the control-plane depth for a roof framing member centerline."""
        layer_top_depth = self._resolve_roof_layer_top_depth(plane)
        _, member_depth = self._resolve_roof_member_size(family_name, type_name)
        if member_depth > 0.0:
            return layer_top_depth + (member_depth / 2.0)

        target_layer_depth = getattr(plane, "target_layer_depth", 0.0)
        if target_layer_depth > 0.0:
            return target_layer_depth
        return layer_top_depth

    def _roof_profile_loops_local(self, plane, depth_from_exterior):
        """Derive a roof member control profile from adjacent roof faces."""
        if depth_from_exterior <= 1e-9:
            return plane.boundary_loops_local

        adjacent_faces = self._collect_adjacent_roof_faces(plane.element, plane.normal)
        if not adjacent_faces:
            return plane.boundary_loops_local

        shifted_loops = []
        for loop in plane.boundary_loops_local:
            world_loop = [
                _surface_point(plane, local_x, local_y)
                for local_x, local_y in loop
            ]
            shifted_loop = self._shift_roof_loop_local(
                plane,
                world_loop,
                depth_from_exterior,
                adjacent_faces,
            )
            if shifted_loop is None:
                return plane.boundary_loops_local
            shifted_loops.append(shifted_loop)

        return shifted_loops

    def _collect_adjacent_roof_faces(self, roof, top_normal):
        """Collect non-coplanar roof faces that can bound the framing profile."""
        from Autodesk.Revit.DB import GeometryInstance, Options, Solid, ViewDetailLevel

        try:
            options = Options()
            options.ComputeReferences = False
            options.DetailLevel = ViewDetailLevel.Fine
            geometry = roof.get_Geometry(options)
        except Exception:
            geometry = None
        if geometry is None:
            return []

        solids = []
        for geom_obj in geometry:
            if isinstance(geom_obj, Solid) and geom_obj.Volume > 0:
                solids.append(geom_obj)
                continue
            if isinstance(geom_obj, GeometryInstance):
                try:
                    instance_geometry = geom_obj.GetInstanceGeometry()
                except Exception:
                    instance_geometry = None
                if instance_geometry is None:
                    continue
                for sub_obj in instance_geometry:
                    if isinstance(sub_obj, Solid) and sub_obj.Volume > 0:
                        solids.append(sub_obj)

        adjacent_faces = []
        for solid in solids:
            for face in solid.Faces:
                face_normal = _face_normal(face)
                if face_normal is None:
                    continue
                if abs(face_normal.DotProduct(top_normal)) > 0.9999:
                    continue
                face_loops = _extract_face_loops(face)
                if face_loops:
                    adjacent_faces.append((face_normal, face_loops))

        return adjacent_faces

    def _shift_roof_loop_local(self, plane, loop_points, depth_from_exterior, adjacent_faces):
        """Project a top-face loop down to a parallel control plane via adjacent faces."""
        if len(loop_points) < 3:
            return None

        shifted_lines = []
        for index in range(len(loop_points)):
            start_point = loop_points[index]
            end_point = loop_points[(index + 1) % len(loop_points)]
            edge_dir = _normalize(end_point - start_point)
            if edge_dir is None:
                return None

            face_normal = self._find_adjacent_face_normal(
                adjacent_faces,
                start_point,
                end_point,
            )
            if face_normal is None:
                return None

            move_axis = _normalize(edge_dir.CrossProduct(face_normal))
            denominator = plane.normal.DotProduct(move_axis) if move_axis is not None else 0.0
            if move_axis is None or abs(denominator) < 1e-9:
                move_axis = _normalize(face_normal.CrossProduct(edge_dir))
                denominator = plane.normal.DotProduct(move_axis) if move_axis is not None else 0.0
            if move_axis is None or abs(denominator) < 1e-9:
                return None

            distance = -depth_from_exterior / denominator
            shift_vec = move_axis.Multiply(distance)
            shift_x = shift_vec.DotProduct(plane.x_axis)
            shift_y = shift_vec.DotProduct(plane.y_axis)

            start_local = _to_local(start_point, plane.origin, plane.x_axis, plane.y_axis)
            end_local = _to_local(end_point, plane.origin, plane.x_axis, plane.y_axis)
            shifted_lines.append(
                (
                    (start_local[0] + shift_x, start_local[1] + shift_y),
                    (end_local[0] + shift_x, end_local[1] + shift_y),
                )
            )

        shifted_loop = []
        for index in range(len(shifted_lines)):
            prev_line = shifted_lines[index - 1]
            curr_line = shifted_lines[index]
            point = _line_intersection_2d(prev_line, curr_line)
            if point is None:
                point = curr_line[0]
            shifted_loop.append(point)

        return shifted_loop

    def _find_adjacent_face_normal(self, adjacent_faces, start_point, end_point):
        """Find the non-coplanar roof face that shares a boundary segment."""
        for face_normal, loops in adjacent_faces:
            for loop in loops:
                count = len(loop)
                for index in range(count):
                    face_start = loop[index]
                    face_end = loop[(index + 1) % count]
                    if _same_segment(start_point, end_point, face_start, face_end):
                        return face_normal
        return None

    def _get_roof_solids(self, roof):
        """Return cached positive-volume solids for a roof element."""
        from Autodesk.Revit.DB import GeometryInstance, Options, Solid, ViewDetailLevel

        cache = getattr(self, "_roof_solids_cache", None)
        if cache is None:
            cache = {}
            self._roof_solids_cache = cache

        key = getattr(getattr(roof, "Id", None), "IntegerValue", None)
        if key in cache:
            return cache[key]

        solids = []
        try:
            options = Options()
            options.ComputeReferences = False
            options.DetailLevel = ViewDetailLevel.Fine
            geometry = roof.get_Geometry(options)
        except Exception:
            geometry = None

        if geometry is not None:
            for geom_obj in geometry:
                if isinstance(geom_obj, Solid) and geom_obj.Volume > 0:
                    solids.append(geom_obj)
                    continue
                if isinstance(geom_obj, GeometryInstance):
                    try:
                        instance_geometry = geom_obj.GetInstanceGeometry()
                    except Exception:
                        instance_geometry = None
                    if instance_geometry is None:
                        continue
                    for sub_obj in instance_geometry:
                        if isinstance(sub_obj, Solid) and sub_obj.Volume > 0:
                            solids.append(sub_obj)

        cache[key] = solids
        return solids

    def _clip_member_axis_to_roof(self, roof, start_point, end_point):
        """Clip a member axis to the actual roof solid along its line of action."""
        from Autodesk.Revit.DB import Line, SolidCurveIntersectionOptions

        axis_dir = _normalize(end_point - start_point)
        axis_len = _segment_length(start_point, end_point)
        if axis_dir is None or axis_len < MIN_MEMBER_LENGTH:
            return None

        solids = self._get_roof_solids(roof)
        if not solids:
            return (start_point, end_point)

        try:
            bbox = roof.get_BoundingBox(None)
        except Exception:
            bbox = None

        probe_half = axis_len + 10.0
        if bbox is not None:
            try:
                dx = bbox.Max.X - bbox.Min.X
                dy = bbox.Max.Y - bbox.Min.Y
                dz = bbox.Max.Z - bbox.Min.Z
                probe_half = max(probe_half, math.sqrt(dx * dx + dy * dy + dz * dz) + 10.0)
            except Exception:
                pass

        mid_point = _midpoint(start_point, end_point)
        probe_start = mid_point - axis_dir.Multiply(probe_half)
        probe_end = mid_point + axis_dir.Multiply(probe_half)

        try:
            probe_line = Line.CreateBound(probe_start, probe_end)
        except Exception:
            return (start_point, end_point)

        target_t = (mid_point - probe_start).DotProduct(axis_dir)
        best_segment = None
        best_contains_target = False
        best_distance = None
        best_length = 0.0

        for solid in solids:
            try:
                result = solid.IntersectWithCurve(
                    probe_line,
                    SolidCurveIntersectionOptions(),
                )
                seg_count = result.SegmentCount
            except Exception:
                continue

            for index in range(seg_count):
                try:
                    segment = result.GetCurveSegment(index)
                    seg_start = segment.GetEndPoint(0)
                    seg_end = segment.GetEndPoint(1)
                except Exception:
                    continue

                start_t = (seg_start - probe_start).DotProduct(axis_dir)
                end_t = (seg_end - probe_start).DotProduct(axis_dir)
                seg_min = min(start_t, end_t)
                seg_max = max(start_t, end_t)
                contains_target = (seg_min - 1e-6) <= target_t <= (seg_max + 1e-6)
                distance = 0.0 if contains_target else min(abs(target_t - seg_min), abs(target_t - seg_max))
                seg_length = _segment_length(seg_start, seg_end)

                choose = False
                if best_segment is None:
                    choose = True
                elif contains_target and not best_contains_target:
                    choose = True
                elif contains_target == best_contains_target:
                    if distance < (best_distance if best_distance is not None else float("inf")) - 1e-6:
                        choose = True
                    elif abs(distance - (best_distance if best_distance is not None else distance)) <= 1e-6 and seg_length > best_length:
                        choose = True

                if choose:
                    if start_t <= end_t:
                        best_segment = (seg_start, seg_end)
                    else:
                        best_segment = (seg_end, seg_start)
                    best_contains_target = contains_target
                    best_distance = distance
                    best_length = seg_length

        return best_segment

    def _choose_best_side_shift(self, roof, start_point, end_point, side_axis, distance):
        """Pick the side shift whose clipped axis stays farther inside the roof."""
        if side_axis is None or distance <= 1e-9:
            return None

        original_length = _dist(start_point, end_point)
        best_segment = None
        best_length = -1.0
        for sign in (-1.0, 1.0):
            shift = side_axis.Multiply(sign * distance)
            cand_start = start_point + shift
            cand_end = end_point + shift
            clipped = self._clip_member_axis_to_roof(roof, cand_start, cand_end)
            if clipped is None:
                continue
            seg_length = _dist(clipped[0], clipped[1])
            if seg_length > original_length + PROFILE_MATCH_TOL:
                continue
            if seg_length > best_length + 1e-6:
                best_segment = clipped
                best_length = seg_length

        return best_segment

    # ------------------------------------------------------------------
    #  Collar ties
    # ------------------------------------------------------------------

    def _make_collar_ties(self, planes, ridge_edges, roof_info):
        """Collar ties at 1/3 rafter length from ridge, every other rafter."""
        from Autodesk.Revit.DB import XYZ

        members = []
        seen = set()
        if not ridge_edges:
            return members

        spacing = self.config.stud_spacing_ft
        if spacing <= 0:
            return members

        tie_spacing = spacing * 2.0
        if tie_spacing > MAX_COLLAR_TIE_SPACING:
            tie_spacing = spacing
        if tie_spacing > MAX_COLLAR_TIE_SPACING:
            tie_spacing = MAX_COLLAR_TIE_SPACING

        for rs, re, pa, pb in ridge_edges:
            ridge_dir = re - rs
            ridge_len = ridge_dir.GetLength()
            if ridge_len < MIN_MEMBER_LENGTH:
                continue
            ridge_unit = ridge_dir.Multiply(1.0 / ridge_len)

            class_a = _classify_boundary_edges(pa, ridge_edges, (rs, re))
            class_b = _classify_boundary_edges(pb, ridge_edges, (rs, re))
            eave_a = class_a["eave"]
            eave_b = class_b["eave"]
            if not eave_a or not eave_b:
                continue

            d = tie_spacing / 2.0
            while d < ridge_len:
                ridge_pt = rs + ridge_unit.Multiply(d)
                foot_a = _project_to_best_eave(ridge_pt, eave_a, rs, re)
                foot_b = _project_to_best_eave(ridge_pt, eave_b, rs, re)
                if foot_a is None or foot_b is None:
                    d += tie_spacing
                    continue

                tie_a = _lerp(ridge_pt, foot_a, COLLAR_TIE_FRACTION)
                tie_b = _lerp(ridge_pt, foot_b, COLLAR_TIE_FRACTION)
                tie_z = (tie_a.Z + tie_b.Z) / 2.0
                tie_a = XYZ(tie_a.X, tie_a.Y, tie_z)
                tie_b = XYZ(tie_b.X, tie_b.Y, tie_z)

                if _dist(tie_a, tie_b) >= MIN_MEMBER_LENGTH:
                    key = (_pt_key(tie_a), _pt_key(tie_b))
                    rkey = (_pt_key(tie_b), _pt_key(tie_a))
                    if key not in seen and rkey not in seen:
                        seen.add(key)
                        m = FramingMember(FramingMember.HEADER, tie_a, tie_b)
                        m.member_type = "COLLAR_TIE"
                        m.family_name = self.config.stud_family_name
                        m.type_name = self.config.stud_type_name
                        m.rotation = -math.pi / 2.0  # flat
                        m.host_kind = roof_info.kind
                        m.host_id = roof_info.element_id
                        members.append(m)
                d += tie_spacing

        return members

    # ------------------------------------------------------------------
    #  Ceiling joists + kickers
    # ------------------------------------------------------------------

    def _make_ceiling_joists(self, planes, ridge_edges, roof_info, spacing, include_kickers=True):
        """Ceiling joists spanning eave-to-eave at plate line elevation.

        Also generates kicker/outrigger braces from each joist up to
        the rafter at KICKER_FRACTION of rafter length from eave.
        """
        from Autodesk.Revit.DB import XYZ

        members = []
        joist_seen = set()
        kicker_seen = set()
        if not ridge_edges:
            return members
        if spacing <= 0:
            return members

        for rs, re, pa, pb in ridge_edges:
            ridge_dir = re - rs
            ridge_len = ridge_dir.GetLength()
            if ridge_len < MIN_MEMBER_LENGTH:
                continue
            ridge_unit = ridge_dir.Multiply(1.0 / ridge_len)

            class_a = _classify_boundary_edges(pa, ridge_edges, (rs, re))
            class_b = _classify_boundary_edges(pb, ridge_edges, (rs, re))
            eave_a = class_a["eave"]
            eave_b = class_b["eave"]
            if not eave_a or not eave_b:
                continue

            d = 0.0
            while d <= ridge_len + 1e-9:
                ridge_pt = rs + ridge_unit.Multiply(min(d, ridge_len))
                foot_a = _project_to_best_eave(ridge_pt, eave_a, rs, re)
                foot_b = _project_to_best_eave(ridge_pt, eave_b, rs, re)
                if foot_a is None or foot_b is None:
                    d += spacing
                    continue

                # Use lower support point to avoid floating joists on uneven eaves.
                joist_z = min(foot_a.Z, foot_b.Z)
                joist_a = XYZ(foot_a.X, foot_a.Y, joist_z)
                joist_b = XYZ(foot_b.X, foot_b.Y, joist_z)

                if _dist(joist_a, joist_b) >= MIN_MEMBER_LENGTH:
                    jkey = (_pt_key(joist_a), _pt_key(joist_b))
                    jrkey = (_pt_key(joist_b), _pt_key(joist_a))
                    if jkey not in joist_seen and jrkey not in joist_seen:
                        joist_seen.add(jkey)
                        m = FramingMember(FramingMember.HEADER, joist_a, joist_b)
                        m.member_type = "CEILING_JOIST"
                        m.family_name = self.config.stud_family_name
                        m.type_name = self.config.stud_type_name
                        m.rotation = -math.pi / 2.0  # flat
                        m.host_kind = roof_info.kind
                        m.host_id = roof_info.element_id
                        members.append(m)

                    if include_kickers:
                        # Kicker side A: diagonal from joist to rafter.
                        rafter_pt_a = _lerp(foot_a, ridge_pt, KICKER_FRACTION)
                        kick_base_a = _lerp(joist_a, joist_b, KICKER_FRACTION)
                        if _dist(kick_base_a, rafter_pt_a) >= MIN_MEMBER_LENGTH:
                            kkey = (_pt_key(kick_base_a), _pt_key(rafter_pt_a))
                            krkey = (_pt_key(rafter_pt_a), _pt_key(kick_base_a))
                            if kkey not in kicker_seen and krkey not in kicker_seen:
                                kicker_seen.add(kkey)
                                km = FramingMember(
                                    FramingMember.STUD,
                                    kick_base_a,
                                    rafter_pt_a,
                                )
                                km.member_type = "KICKER"
                                km.family_name = self.config.stud_family_name
                                km.type_name = self.config.stud_type_name
                                km.rotation = _rotation_from_up(
                                    rafter_pt_a - kick_base_a,
                                    getattr(pa, "normal", None),
                                )
                                km.host_kind = roof_info.kind
                                km.host_id = roof_info.element_id
                                members.append(km)

                        # Kicker side B.
                        rafter_pt_b = _lerp(foot_b, ridge_pt, KICKER_FRACTION)
                        kick_base_b = _lerp(joist_b, joist_a, KICKER_FRACTION)
                        if _dist(kick_base_b, rafter_pt_b) >= MIN_MEMBER_LENGTH:
                            kkey = (_pt_key(kick_base_b), _pt_key(rafter_pt_b))
                            krkey = (_pt_key(rafter_pt_b), _pt_key(kick_base_b))
                            if kkey not in kicker_seen and krkey not in kicker_seen:
                                kicker_seen.add(kkey)
                                km = FramingMember(
                                    FramingMember.STUD,
                                    kick_base_b,
                                    rafter_pt_b,
                                )
                                km.member_type = "KICKER"
                                km.family_name = self.config.stud_family_name
                                km.type_name = self.config.stud_type_name
                                km.rotation = _rotation_from_up(
                                    rafter_pt_b - kick_base_b,
                                    getattr(pb, "normal", None),
                                )
                                km.host_kind = roof_info.kind
                                km.host_id = roof_info.element_id
                                members.append(km)

                d += spacing

        return members

    # ------------------------------------------------------------------
    #  Fascia / border trim
    # ------------------------------------------------------------------

    def _make_fascia(self, eave_edges, roof_info):
        """Create border trim members along the supplied roof boundary edges."""
        members = []
        seen = set()
        for es, ee, plane, edge_role, edge_depth in eave_edges:
            if _dist(es, ee) < MIN_MEMBER_LENGTH:
                continue
            key = (_pt_key(es), _pt_key(ee))
            rkey = (_pt_key(ee), _pt_key(es))
            if key in seen or rkey in seen:
                continue
            seen.add(key)
            member = self._make_roof_border_member(
                es,
                ee,
                plane,
                roof_info,
                "FASCIA",
                edge_role,
                edge_depth,
            )
            if member is not None:
                members.append(member)
        return members

    # ------------------------------------------------------------------
    #  Ledger -- high-side beam on shed roofs
    # ------------------------------------------------------------------

    def _make_ledger(self, ledger_edges, roof_info):
        """Ledger / header beam at the high side of a shed roof."""
        members = []
        seen = set()
        for ls, le, plane, edge_role, edge_depth in ledger_edges:
            if _dist(ls, le) < MIN_MEMBER_LENGTH:
                continue
            key = (_pt_key(ls), _pt_key(le))
            rkey = (_pt_key(le), _pt_key(ls))
            if key in seen or rkey in seen:
                continue
            seen.add(key)
            member = self._make_roof_border_member(
                ls,
                le,
                plane,
                roof_info,
                "LEDGER",
                edge_role,
                edge_depth,
            )
            if member is not None:
                members.append(member)
        return members

    def _make_roof_border_member(self, start_point, end_point, plane, roof_info, member_type, edge_role, edge_depth):
        """Create a roof border member aligned to the host roof face."""
        from Autodesk.Revit.DB import XYZ

        member_dir = _normalize(end_point - start_point)
        if member_dir is None:
            return None

        family_name = (
            self.config.header_family_name or self.config.stud_family_name)
        type_name = (
            self.config.header_type_name or self.config.stud_type_name)
        member_width, member_depth = self._resolve_roof_member_size(
            family_name,
            type_name,
        )
        layer_top_depth = self._resolve_roof_layer_top_depth(plane)
        roof_normal = getattr(plane, "normal", None)
        roof_normal = _normalize(roof_normal) if roof_normal is not None else None

        rotation = 0.0
        offset = None

        source_depth = self._edge_depth_from_roof_face(
            plane,
            start_point,
            end_point,
            edge_depth,
        )
        extra_depth = max(0.0, layer_top_depth - source_depth)
        if roof_normal is not None and extra_depth > 0.0:
            offset = roof_normal.Multiply(-extra_depth)

        if edge_role in ("eave", "ledger"):
            plumb_down = _normalize(
                _project_perpendicular(XYZ.BasisZ.Multiply(-1.0), member_dir)
            )
            outward = getattr(plane, "y_axis", None)
            outward = _normalize(_project_perpendicular(outward, member_dir))
            if outward is not None and edge_role == "ledger":
                outward = outward.Multiply(-1.0)

            if plumb_down is not None and member_depth > 0.0:
                drop_shift = plumb_down.Multiply(member_depth / 2.0)
                offset = drop_shift if offset is None else offset + drop_shift
            if outward is not None and member_width > 0.0:
                outward_shift = outward.Multiply(-member_width / 2.0)
                offset = outward_shift if offset is None else offset + outward_shift
        else:
            desired_up = roof_normal
            reference_up = _beam_reference_up(member_dir)
            if reference_up is not None and desired_up is not None:
                rotation = _signed_angle_about(member_dir, reference_up, desired_up)
            if member_depth > 0.0 and desired_up is not None:
                depth_shift = desired_up.Multiply(-member_depth / 2.0)
                offset = depth_shift if offset is None else offset + depth_shift

            side_axis = _normalize(
                _project_perpendicular(getattr(plane, "x_axis", None), member_dir)
            )
            if side_axis is not None and member_width > 0.0:
                probe_start = start_point if offset is None else start_point + offset
                probe_end = end_point if offset is None else end_point + offset
                best_shifted = self._choose_best_side_shift(
                    roof_info.element,
                    probe_start,
                    probe_end,
                    side_axis,
                    member_width / 2.0,
                )
                if best_shifted is not None:
                    start_point, end_point = best_shifted
                    offset = None

        if offset is not None:
            start_point = start_point + offset
            end_point = end_point + offset

        original_length = _dist(start_point, end_point)
        clipped = self._clip_member_axis_to_roof(roof_info.element, start_point, end_point)
        if clipped is not None:
            clipped_length = _dist(clipped[0], clipped[1])
            if clipped_length <= original_length + PROFILE_MATCH_TOL:
                start_point, end_point = clipped
        if _dist(start_point, end_point) < MIN_MEMBER_LENGTH:
            return None

        member = FramingMember(FramingMember.HEADER, start_point, end_point)
        member.member_type = member_type
        member.family_name = family_name
        member.type_name = type_name
        member.rotation = rotation
        member.host_kind = roof_info.kind
        member.host_id = roof_info.element_id
        return member

    @staticmethod
    def _edge_depth_from_roof_face(plane, start_point, end_point, fallback_depth):
        """Return average edge depth from the roof exterior face."""
        normal = getattr(plane, "normal", None)
        normal = _normalize(normal) if normal is not None else None
        if normal is None:
            return max(0.0, float(fallback_depth or 0.0))
        try:
            start_depth = -(start_point - plane.origin).DotProduct(normal)
            end_depth = -(end_point - plane.origin).DotProduct(normal)
            return max(0.0, (float(start_depth) + float(end_depth)) * 0.5)
        except Exception:
            return max(0.0, float(fallback_depth or 0.0))

    @staticmethod
    def _resolve_roof_layer_top_depth(plane):
        """Return the depth from roof exterior to the top of the target layer."""
        target_layer = RoofFramingEngine._preferred_roof_structural_layer(plane)
        if target_layer is None:
            return 0.0
        try:
            return max(0.0, float(getattr(target_layer, "start_depth", 0.0)))
        except Exception:
            return 0.0

    @staticmethod
    def _preferred_roof_structural_layer(plane):
        """Prefer a physical roof structure layer over a virtual core fallback."""
        target_layer = getattr(plane, "target_layer", None)
        if (RoofFramingEngine._layer_is_roof_structure(target_layer)
                and not getattr(target_layer, "is_virtual", False)):
            return target_layer

        for layer in getattr(plane, "layers", []) or []:
            if getattr(layer, "is_virtual", False):
                continue
            if RoofFramingEngine._layer_is_roof_structure(layer):
                return layer

        return target_layer

    @staticmethod
    def _layer_is_roof_structure(layer):
        if layer is None:
            return False
        if bool(getattr(layer, "is_structural", False)):
            return True
        function = str(getattr(layer, "function", "") or "").lower()
        return "struct" in function

    def _resolve_roof_member_size(self, family_name, type_name):
        """Resolve member thickness and depth from the type or nominal size."""
        depth = self.get_type_depth(family_name, type_name)
        width = None

        text = "{0} {1}".format(family_name or "", type_name or "").lower()
        for nominal, dimensions in LUMBER_ACTUAL.items():
            if nominal.lower() in text:
                width = inches_to_feet(dimensions[0])
                if depth is None or depth <= 0.0:
                    depth = inches_to_feet(dimensions[1])
                break

        if depth is not None and depth > 0.0:
            if width is None or width <= 0.0:
                width = PLATE_THICKNESS
            return width, depth

        match = re.search(r"\b2x(2|3|4|6|8|10|12)\b", text)
        if match:
            nominal = "2x{0}".format(match.group(1))
            dims = LUMBER_ACTUAL.get(nominal)
            if dims is not None:
                return inches_to_feet(dims[0]), inches_to_feet(dims[1])

        return PLATE_THICKNESS, 0.0

    # ------------------------------------------------------------------
    #  Truss placement
    # ------------------------------------------------------------------

    def _get_support_elements(self, roof_info):
        from Autodesk.Revit.DB import FilteredElementCollector, Wall, BuiltInCategory, Outline, BoundingBoxIntersectsFilter, XYZ
        doc = self.doc
        roof = roof_info.element
        bbox = roof.get_BoundingBox(None)
        if bbox is None:
            return [], []
        
        # Expand Outline vertically and horizontally to find walls/beams directly below
        outline = Outline(
            XYZ(bbox.Min.X - 2.0, bbox.Min.Y - 2.0, bbox.Min.Z - 12.0),
            XYZ(bbox.Max.X + 2.0, bbox.Max.Y + 2.0, bbox.Max.Z + 2.0)
        )
        box_filter = BoundingBoxIntersectsFilter(outline)
        
        walls = list(FilteredElementCollector(doc).OfClass(Wall).WherePasses(box_filter).ToElements())
        beams = list(FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralFraming).WherePasses(box_filter).ToElements())
        return walls, beams

    def _find_supports_along_slice(self, foot_a, foot_b, walls, beams):
        from Autodesk.Revit.DB import XYZ
        
        p1 = foot_a
        p2 = foot_b
        U_x = (p2 - p1)
        L = U_x.GetLength()
        if L < 1e-9:
            return []
        U_x = U_x.Multiply(1.0 / L)
        
        intersections = []
        
        def check_line_intersection(w_start, w_end, element_z):
            dx1, dy1 = p2.X - p1.X, p2.Y - p1.Y
            dx2, dy2 = w_end.X - w_start.X, w_end.Y - w_start.Y
            denom = dx1 * dy2 - dy1 * dx2
            if abs(denom) < 1e-7:
                return None
            t = ((w_start.X - p1.X) * dy2 - (w_start.Y - p1.Y) * dx2) / denom
            u = ((w_start.X - p1.X) * dy1 - (w_start.Y - p1.Y) * dx1) / denom
            
            if -1e-4 <= u <= 1.0 + 1e-4 and -1e-4 <= t <= 1.0 + 1e-4:
                pt_xy = p1 + (p2 - p1).Multiply(t)
                return XYZ(pt_xy.X, pt_xy.Y, element_z)
            return None

        for wall in walls:
            try:
                loc = wall.Location
                if loc is not None and getattr(loc, "Curve", None) is not None:
                    curve = loc.Curve
                    w_start = curve.GetEndPoint(0)
                    w_end = curve.GetEndPoint(1)
                    bbox = wall.get_BoundingBox(None)
                    wall_top_z = bbox.Max.Z if bbox else foot_a.Z
                    pt = check_line_intersection(w_start, w_end, wall_top_z)
                    if pt is not None:
                        intersections.append(pt)
            except Exception:
                pass
                
        for beam in beams:
            try:
                loc = beam.Location
                if loc is not None and getattr(loc, "Curve", None) is not None:
                    curve = loc.Curve
                    b_start = curve.GetEndPoint(0)
                    b_end = curve.GetEndPoint(1)
                    bbox = beam.get_BoundingBox(None)
                    beam_top_z = bbox.Max.Z if bbox else foot_a.Z
                    pt = check_line_intersection(b_start, b_end, beam_top_z)
                    if pt is not None:
                        intersections.append(pt)
            except Exception:
                pass
                
        return intersections

    def _shorten_to_support_face(self, p_start, p_end, support_start, support_end, support_width):
        from Autodesk.Revit.DB import XYZ
        
        dx1, dy1 = p_end.X - p_start.X, p_end.Y - p_start.Y
        dx2, dy2 = support_end.X - support_start.X, support_end.Y - support_start.Y
        denom = dx1 * dy2 - dy1 * dx2
        if abs(denom) < 1e-7:
            return p_end
        t = ((support_start.X - p_start.X) * dy2 - (support_start.Y - p_start.Y) * dx2) / denom
        u = ((support_start.X - p_start.X) * dy1 - (support_start.Y - p_start.Y) * dx1) / denom
        
        if -0.15 <= u <= 1.15 and -0.05 <= t <= 1.05:
            p_int = p_start + (p_end - p_start).Multiply(t)
            dir_support = (support_end - support_start).Normalize()
            N = XYZ(-dir_support.Y, dir_support.X, 0.0).Normalize()
            D = (p_end - p_start).Normalize()
            dot = D.DotProduct(N)
            if abs(dot) > 1e-5:
                shorten_len = (support_width / 2.0) / abs(dot)
                # Ensure we don't shorten more than the member length
                member_len = (p_end - p_start).GetLength()
                if shorten_len < member_len - 0.1:
                    return p_int - D.Multiply(shorten_len)
        return p_end

    def _calc_truss_positions(self, roof_info):
        """Place trusses at OC spacing based on precise 3D boundary slicing."""
        from Autodesk.Revit.DB import XYZ
        import math

        members = []
        planes = roof_info.planes
        
        truss_spacing_in = getattr(self.config, 'truss_spacing', 24.0)
        ceiling_spacing_in = getattr(self.config, 'ceiling_spacing', 16.0)
        truss_spacing = truss_spacing_in / 12.0
        ceiling_spacing = ceiling_spacing_in / 12.0
        
        family_top = getattr(self.config, 'family_top_chords', (self.config.stud_family_name, self.config.stud_type_name))
        family_bottom = getattr(self.config, 'family_bottom_chords', (self.config.stud_family_name, self.config.stud_type_name))
        family_web = getattr(self.config, 'family_web_bracing', (self.config.stud_family_name, self.config.stud_type_name))
        family_edge = getattr(self.config, 'family_hips_ridges', (self.config.stud_family_name, self.config.stud_type_name))

        _, tc_depth = self._resolve_roof_member_size(family_top[0], family_top[1])
        _, bc_depth = self._resolve_roof_member_size(family_bottom[0], family_bottom[1])
        _, web_depth = self._resolve_roof_member_size(family_web[0], family_web[1])

        walls, beams = self._get_support_elements(roof_info)

        try:
            ridge_edges = _find_ridge_edges(planes)
        except Exception:
            ridge_edges = []

        horizontal_ridges = []
        for edge in ridge_edges:
            rs_edge, re_edge, pa_edge, pb_edge = edge
            if pa_edge.normal.DotProduct(pb_edge.normal) > 0.5:
                continue
            if abs(re_edge.Z - rs_edge.Z) < RIDGE_TOL:
                horizontal_ridges.append(edge)

        if ridge_edges and not horizontal_ridges:
            horizontal_ridges = ridge_edges
            
        sloped = [p for p in planes if p.normal.Z < FLAT_THRESHOLD]
        
        slice_planes = []

        seen_t = set()
        for rs, re, pa, pb in horizontal_ridges:
            ridge_dir = re - rs
            ridge_len = ridge_dir.GetLength()
            if ridge_len < MIN_MEMBER_LENGTH:
                continue
            ridge_unit = ridge_dir.Multiply(1.0 / ridge_len)
            
            U_x = XYZ(-ridge_unit.Y, ridge_unit.X, 0.0).Normalize()
            
            min_d, max_d = 0.0, ridge_len
            proj_vals = []
            for plane in planes:
                if plane.normal.Z >= FLAT_THRESHOLD:
                    continue
                for loop in plane.boundary_loops_local:
                    for vertex in loop:
                        pt = _surface_point(plane, vertex[0], vertex[1])
                        proj = (pt - rs).DotProduct(ridge_unit)
                        proj_vals.append(proj)
                        
            if proj_vals:
                min_d = min(proj_vals)
                max_d = max(proj_vals)
                
            span_len = max_d - min_d
            num_trusses = int(math.ceil(span_len / truss_spacing)) if truss_spacing > 0 else 1
            num_trusses = max(1, num_trusses)
            actual_truss_spacing = span_len / num_trusses
            
            for i in range(num_trusses + 1):
                d_station = min_d + i * actual_truss_spacing
                ridge_pt = rs + ridge_unit.Multiply(d_station)
                
                rounded_station = round(d_station, 2)
                if rounded_station in seen_t:
                    continue
                seen_t.add(rounded_station)
                
                slice_planes.append({
                    "origin": ridge_pt,
                    "U_x": U_x,
                    "type": "MAIN"
                })

        for p in sloped:
            downslope = XYZ(p.normal.X, p.normal.Y, 0.0).Normalize()
            is_main = False
            for rs, re, pa, pb in horizontal_ridges:
                ridge_dir = (re - rs).Normalize()
                U_x_main = XYZ(-ridge_dir.Y, ridge_dir.X, 0.0).Normalize()
                if abs(downslope.DotProduct(U_x_main)) > 0.99:
                    is_main = True
                    break
            
            if not is_main:
                cross_slope = XYZ(-downslope.Y, downslope.X, 0.0).Normalize()
                proj_vals = []
                for loop in p.boundary_loops_local:
                    for vertex in loop:
                        pt = _surface_point(p, vertex[0], vertex[1])
                        proj_vals.append(pt.DotProduct(cross_slope))
                        
                if proj_vals:
                    min_c = min(proj_vals)
                    max_c = max(proj_vals)
                    span_c = max_c - min_c
                    num_jacks = int(math.ceil(span_c / truss_spacing)) if truss_spacing > 0 else 1
                    actual_spacing = span_c / num_jacks
                    
                    for i in range(1, num_jacks):
                        c_val = min_c + i * actual_spacing
                        origin = cross_slope.Multiply(c_val)
                        slice_planes.append({
                            "origin": origin,
                            "U_x": downslope,
                            "type": "HIP_JACK"
                        })

        for s_plane in slice_planes:
            Origin = s_plane["origin"]
            U_x = s_plane["U_x"]
            N = U_x.CrossProduct(XYZ.BasisZ).Normalize()
            
            segments = []
            for p in sloped:
                intersections = []
                for loop in p.boundary_loops_local:
                    pts3d = [_surface_point(p, v[0], v[1]) for v in loop]
                    for i in range(len(pts3d)):
                        V1 = pts3d[i]
                        V2 = pts3d[(i+1)%len(pts3d)]
                        d1 = (V1 - Origin).DotProduct(N)
                        d2 = (V2 - Origin).DotProduct(N)
                        
                        if d1 * d2 < 0:
                            t = d1 / (d1 - d2)
                            P_int = V1 + (V2 - V1).Multiply(t)
                            x_val = (P_int - Origin).DotProduct(U_x)
                            intersections.append(x_val)
                        elif abs(d1) < 1e-5:
                            intersections.append((V1 - Origin).DotProduct(U_x))
                
                if len(intersections) >= 2:
                    x_min = min(intersections)
                    x_max = max(intersections)
                    if x_max - x_min < MIN_MEMBER_LENGTH:
                        continue
                        
                    layer_depth = self._resolve_roof_layer_top_depth(p)
                    p_ref_shifted = _surface_point(p, 0, 0) - p.normal.Multiply(layer_depth + tc_depth / 2.0)
                    
                    denom = p.normal.Z
                    if abs(denom) > 1e-5:
                        A = - U_x.DotProduct(p.normal) / denom
                        B = - (Origin - p_ref_shifted).DotProduct(p.normal) / denom
                        segments.append({
                            "x_min": x_min,
                            "x_max": x_max,
                            "A": A,
                            "B": B,
                            "plane": p
                        })
            
            if not segments:
                continue
                
            global_min_x = min(s["x_min"] for s in segments)
            global_max_x = max(s["x_max"] for s in segments)
            
            p_start_3d = Origin + U_x.Multiply(global_min_x)
            p_end_3d = Origin + U_x.Multiply(global_max_x)
            
            intersections = self._find_supports_along_slice(p_start_3d, p_end_3d, walls, beams)
            joist_z = min([pt.Z for pt in intersections]) if intersections else min(s["A"]*s["x_min"]+s["B"] for s in segments) - 0.5
            
            x_events = set()
            for s in segments:
                x_events.add(round(s["x_min"], 4))
                x_events.add(round(s["x_max"], 4))
            
            for i in range(len(segments)):
                for j in range(i+1, len(segments)):
                    s1 = segments[i]
                    s2 = segments[j]
                    if abs(s1["A"] - s2["A"]) > 1e-5:
                        x_int = (s2["B"] - s1["B"]) / (s1["A"] - s2["A"])
                        if max(s1["x_min"], s2["x_min"]) - 1e-3 <= x_int <= min(s1["x_max"], s2["x_max"]) + 1e-3:
                            x_events.add(round(x_int, 4))
                            
            x_events = sorted(list(x_events))
            
            profile_nodes = []
            for x_val in x_events:
                best_z = -1e9
                best_plane = None
                for s in segments:
                    if s["x_min"] - 1e-3 <= x_val <= s["x_max"] + 1e-3:
                        z_val = s["A"] * x_val + s["B"]
                        if z_val > best_z:
                            best_z = z_val
                            best_plane = s["plane"]
                if best_z > -1e8:
                    profile_nodes.append({"x": x_val, "z": best_z, "plane": best_plane})
                    
            if len(profile_nodes) < 2:
                continue
                
            filtered_nodes = [profile_nodes[0]]
            for i in range(1, len(profile_nodes)-1):
                p_prev = profile_nodes[i-1]
                p_curr = profile_nodes[i]
                p_next = profile_nodes[i+1]
                
                dx1 = p_curr["x"] - p_prev["x"]
                dz1 = p_curr["z"] - p_prev["z"]
                dx2 = p_next["x"] - p_curr["x"]
                dz2 = p_next["z"] - p_curr["z"]
                
                m1 = dz1/dx1 if dx1 != 0 else 1e9
                m2 = dz2/dx2 if dx2 != 0 else 1e9
                if abs(m1 - m2) > 1e-4:
                    filtered_nodes.append(p_curr)
            filtered_nodes.append(profile_nodes[-1])
            
            for i in range(len(filtered_nodes)-1):
                n1 = filtered_nodes[i]
                n2 = filtered_nodes[i+1]
                pt1 = Origin + U_x.Multiply(n1["x"]) + XYZ.BasisZ.Multiply(n1["z"])
                pt2 = Origin + U_x.Multiply(n2["x"]) + XYZ.BasisZ.Multiply(n2["z"])
                if (pt2 - pt1).GetLength() < MIN_MEMBER_LENGTH:
                    continue
                
                m_tc = FramingMember(FramingMember.STUD, pt1, pt2)
                m_tc.member_type = "TOP_CHORD"
                m_tc.family_name = family_top[0]
                m_tc.type_name = family_top[1]
                m_tc.rotation = _rotation_from_up(pt2 - pt1, n1["plane"].normal)
                m_tc.host_kind = roof_info.kind
                m_tc.host_id = roof_info.element_id
                m_tc.disallow_end_joins = True
                members.append(m_tc)
                
            y_bc = joist_z + bc_depth / 2.0
            
            heel_x_L = None
            heel_x_R = None
            
            for i in range(len(filtered_nodes)-1):
                n1 = filtered_nodes[i]
                n2 = filtered_nodes[i+1]
                if n1["z"] >= y_bc and n2["z"] >= y_bc:
                    heel_x_L = n1["x"]
                    break
                elif n1["z"] <= y_bc <= n2["z"]:
                    t = (y_bc - n1["z"]) / (n2["z"] - n1["z"])
                    heel_x_L = n1["x"] + t * (n2["x"] - n1["x"])
                    break
            
            for i in reversed(range(len(filtered_nodes)-1)):
                n1 = filtered_nodes[i]
                n2 = filtered_nodes[i+1]
                if n1["z"] >= y_bc and n2["z"] >= y_bc:
                    heel_x_R = n2["x"]
                    break
                elif n2["z"] <= y_bc <= n1["z"]:
                    t = (y_bc - n1["z"]) / (n2["z"] - n1["z"])
                    heel_x_R = n1["x"] + t * (n2["x"] - n1["x"])
                    break
                    
            if heel_x_L is None or heel_x_R is None or abs(heel_x_R - heel_x_L) < MIN_MEMBER_LENGTH:
                continue
                
            p_bc_start = Origin + U_x.Multiply(heel_x_L) + XYZ.BasisZ.Multiply(y_bc)
            p_bc_end = Origin + U_x.Multiply(heel_x_R) + XYZ.BasisZ.Multiply(y_bc)
            
            m_bc = FramingMember(FramingMember.STUD, p_bc_start, p_bc_end)
            m_bc.member_type = "BOTTOM_CHORD"
            m_bc.family_name = family_bottom[0]
            m_bc.type_name = family_bottom[1]
            m_bc.rotation = -math.pi / 2.0
            m_bc.host_kind = roof_info.kind
            m_bc.host_id = roof_info.element_id
            m_bc.disallow_end_joins = True
            members.append(m_bc)
            
            web_nodes_top = []
            for n in filtered_nodes:
                if heel_x_L + 1e-3 < n["x"] < heel_x_R - 1e-3:
                    web_nodes_top.append(n)
            
            for n in web_nodes_top:
                pt_top = Origin + U_x.Multiply(n["x"]) + XYZ.BasisZ.Multiply(n["z"])
                pt_bot = Origin + U_x.Multiply(n["x"]) + XYZ.BasisZ.Multiply(y_bc)
                if (pt_top - pt_bot).GetLength() < MIN_MEMBER_LENGTH:
                    continue
                m_web = FramingMember(FramingMember.STUD, pt_bot, pt_top)
                m_web.member_type = "WEB_BRACING"
                m_web.family_name = family_web[0]
                m_web.type_name = family_web[1]
                m_web.rotation = _rotation_from_up(pt_top - pt_bot, XYZ.BasisZ)
                m_web.host_kind = roof_info.kind
                m_web.host_id = roof_info.element_id
                m_web.disallow_end_joins = True
                members.append(m_web)
                
            if s_plane["type"] == "HIP_JACK":
                high_x = heel_x_L if filtered_nodes[0]["z"] > filtered_nodes[-1]["z"] else heel_x_R
                high_z = None
                for n in filtered_nodes:
                    if abs(n["x"] - high_x) < 1e-3:
                        high_z = n["z"]
                        break
                if high_z is not None and high_z > y_bc + 0.5:
                    pt_top = Origin + U_x.Multiply(high_x) + XYZ.BasisZ.Multiply(high_z)
                    pt_bot = Origin + U_x.Multiply(high_x) + XYZ.BasisZ.Multiply(y_bc)
                    if (pt_top - pt_bot).GetLength() >= MIN_MEMBER_LENGTH:
                        m_web = FramingMember(FramingMember.STUD, pt_bot, pt_top)
                        m_web.member_type = "WEB_BRACING"
                        m_web.family_name = family_web[0]
                        m_web.type_name = family_web[1]
                        m_web.rotation = _rotation_from_up(pt_top - pt_bot, XYZ.BasisZ)
                        m_web.host_kind = roof_info.kind
                        m_web.host_id = roof_info.element_id
                        m_web.disallow_end_joins = True
                        members.append(m_web)

        try:
            members.extend(self._make_ridge_boards(ridge_edges, roof_info))
        except Exception:
            pass

        return members
        """Place trusses at OC spacing perpendicular to ridge, eave-to-eave."""
        from Autodesk.Revit.DB import XYZ
        import math

        members = []
        planes = roof_info.planes
        
        # Get parametric variables from config (injected from script.py)
        truss_spacing_in = getattr(self.config, 'truss_spacing', 24.0)
        ceiling_spacing_in = getattr(self.config, 'ceiling_spacing', 16.0)
        truss_spacing = truss_spacing_in / 12.0
        ceiling_spacing = ceiling_spacing_in / 12.0
        
        family_top = getattr(self.config, 'family_top_chords', (self.config.stud_family_name, self.config.stud_type_name))
        family_bottom = getattr(self.config, 'family_bottom_chords', (self.config.stud_family_name, self.config.stud_type_name))
        family_web = getattr(self.config, 'family_web_bracing', (self.config.stud_family_name, self.config.stud_type_name))
        family_edge = getattr(self.config, 'family_hips_ridges', (self.config.stud_family_name, self.config.stud_type_name))

        # Resolve member depths
        _, tc_depth = self._resolve_roof_member_size(family_top[0], family_top[1])
        _, bc_depth = self._resolve_roof_member_size(family_bottom[0], family_bottom[1])
        _, web_depth = self._resolve_roof_member_size(family_web[0], family_web[1])
        edge_width, _ = self._resolve_roof_member_size(family_edge[0], family_edge[1])

        # Get supports (walls, beams) under roof
        walls, beams = self._get_support_elements(roof_info)

        # Collect ridges and hips
        try:
            ridge_edges = _find_ridge_edges(planes)
        except Exception:
            ridge_edges = []

        # Filter for horizontal ridges
        horizontal_ridges = []
        for edge in ridge_edges:
            rs_edge, re_edge, pa_edge, pb_edge = edge
            if pa_edge.normal.DotProduct(pb_edge.normal) > 0.5:
                continue
            if abs(re_edge.Z - rs_edge.Z) < RIDGE_TOL:
                horizontal_ridges.append(edge)

        # If there are ridge edges but no horizontal ridges, fall back to all ridge edges
        if ridge_edges and not horizontal_ridges:
            horizontal_ridges = ridge_edges
            
        sloped = [p for p in planes if p.normal.Z < FLAT_THRESHOLD]
        
        truss_lines = []
        ceiling_lines = []

        if not ridge_edges:
            # Monoslope / shed roof layout
            if not sloped:
                return []
            plane = sloped[0]
            control_depth = self._resolve_roof_member_center_depth(plane, family_top[0], family_top[1])
            profile_loops = self._roof_profile_loops_local(plane, control_depth)
            if not profile_loops:
                return []
            points = [point for loop in profile_loops for point in loop]
            if not points:
                return []
            min_x = min(p[0] for p in points)
            max_x = max(p[0] for p in points)
            span_x = max_x - min_x
            
            # Trusses: equal spacing, always start and finish at bounds
            num_trusses = int(math.ceil(span_x / truss_spacing)) if truss_spacing > 0 else 1
            num_trusses = max(1, num_trusses)
            actual_truss_spacing = span_x / num_trusses
            
            for i in range(num_trusses + 1):
                x = min_x + i * actual_truss_spacing
                intervals = _scanline_intervals(profile_loops, "x", x)
                for start_y, end_y in intervals:
                    if end_y - start_y < MIN_MEMBER_LENGTH:
                        continue
                    s_pt = _plane_point_at_depth(plane, x, start_y, control_depth)
                    e_pt = _plane_point_at_depth(plane, x, end_y, control_depth)
                    truss_lines.append((s_pt, e_pt, plane))
                
            # Ceiling joists: equal spacing, always start and finish at bounds
            num_ceilings = int(math.ceil(span_x / ceiling_spacing)) if ceiling_spacing > 0 else 1
            num_ceilings = max(1, num_ceilings)
            actual_ceiling_spacing = span_x / num_ceilings
            
            for i in range(num_ceilings + 1):
                x = min_x + i * actual_ceiling_spacing
                intervals = _scanline_intervals(profile_loops, "x", x)
                for start_y, end_y in intervals:
                    if end_y - start_y < MIN_MEMBER_LENGTH:
                        continue
                    s_pt = _plane_point_at_depth(plane, x, start_y, control_depth)
                    e_pt = _plane_point_at_depth(plane, x, end_y, control_depth)
                    ceiling_lines.append((s_pt, e_pt))
        else:
            # Dual slope (Gable / Hip) layout
            seen_t = set()
            seen_c = set()
            for rs, re, pa, pb in horizontal_ridges:
                ridge_dir = re - rs
                ridge_len = ridge_dir.GetLength()
                if ridge_len < MIN_MEMBER_LENGTH:
                    continue
                ridge_unit = ridge_dir.Multiply(1.0 / ridge_len)

                pa_lower = None
                for p in planes:
                    if p.normal.Z >= FLAT_THRESHOLD:
                        continue
                    if p is pa:
                        continue
                    if p.normal.DotProduct(pa.normal) > 0.7:
                        shares_edge = False
                        for edge in ridge_edges:
                            rs_edge, re_edge, pa_edge, pb_edge = edge
                            if (pa_edge is pa and pb_edge is p) or (pa_edge is p and pb_edge is pa):
                                shares_edge = True
                                break
                        if shares_edge:
                            class_p = _classify_boundary_edges(p, ridge_edges, (rs, re))
                            if class_p["eave"]:
                                pa_lower = p
                                break

                pb_lower = None
                for p in planes:
                    if p.normal.Z >= FLAT_THRESHOLD:
                        continue
                    if p is pb:
                        continue
                    if p.normal.DotProduct(pb.normal) > 0.7:
                        shares_edge = False
                        for edge in ridge_edges:
                            rs_edge, re_edge, pa_edge, pb_edge = edge
                            if (pa_edge is pb and pb_edge is p) or (pa_edge is p and pb_edge is pb):
                                shares_edge = True
                                break
                        if shares_edge:
                            class_p = _classify_boundary_edges(p, ridge_edges, (rs, re))
                            if class_p["eave"]:
                                pb_lower = p
                                break

                class_a = _classify_boundary_edges(pa, ridge_edges, (rs, re))
                class_b = _classify_boundary_edges(pb, ridge_edges, (rs, re))

                eave_a = class_a["eave"] if pa_lower is None else _classify_boundary_edges(pa_lower, ridge_edges, (rs, re))["eave"]
                eave_b = class_b["eave"] if pb_lower is None else _classify_boundary_edges(pb_lower, ridge_edges, (rs, re))["eave"]

                if not eave_a or not eave_b:
                    continue

                slope_change_edge_a = None
                if pa_lower is not None:
                    for edge in ridge_edges:
                        rs_edge, re_edge, pa_edge, pb_edge = edge
                        if (pa_edge is pa and pb_edge is pa_lower) or (pa_edge is pa_lower and pb_edge is pa):
                            slope_change_edge_a = (rs_edge, re_edge)
                            break

                slope_change_edge_b = None
                if pb_lower is not None:
                    for edge in ridge_edges:
                        rs_edge, re_edge, pa_edge, pb_edge = edge
                        if (pa_edge is pb and pb_edge is pb_lower) or (pa_edge is pb_lower and pb_edge is pb):
                            slope_change_edge_b = (rs_edge, re_edge)
                            break

                # Get the projection values of all roof vertices to find the full horizontal span
                proj_vals = []
                for plane in planes:
                    if plane.normal.Z >= FLAT_THRESHOLD:
                        continue
                    for loop in plane.boundary_loops_local:
                        for vertex in loop:
                            pt = _surface_point(plane, vertex[0], vertex[1])
                            proj = (pt - rs).DotProduct(ridge_unit)
                            proj_vals.append(proj)
                            
                if proj_vals:
                    min_d = min(proj_vals)
                    max_d = max(proj_vals)
                else:
                    min_d = 0.0
                    max_d = ridge_len

                span_len = max_d - min_d

                # Trusses: equal spacing, always start and finish at bounds
                num_trusses = int(math.ceil(span_len / truss_spacing)) if truss_spacing > 0 else 1
                num_trusses = max(1, num_trusses)
                actual_truss_spacing = span_len / num_trusses
                
                for i in range(num_trusses + 1):
                    d_station = min_d + i * actual_truss_spacing
                    ridge_pt = rs + ridge_unit.Multiply(d_station)
                    is_step_down = (d_station < 0.0 or d_station > ridge_len)
                    if is_step_down:
                        foot_a = _project_to_infinite_eave(ridge_pt, eave_a)
                        foot_b = _project_to_infinite_eave(ridge_pt, eave_b)
                    else:
                        foot_a = _project_to_best_eave(ridge_pt, eave_a, rs, re)
                        foot_b = _project_to_best_eave(ridge_pt, eave_b, rs, re)
                    if foot_a is None or foot_b is None:
                        continue
                    if _dist(foot_a, foot_b) >= MIN_MEMBER_LENGTH:
                        key = (_pt_key(foot_a), _pt_key(foot_b))
                        rkey = (_pt_key(foot_b), _pt_key(foot_a))
                        if key not in seen_t and rkey not in seen_t:
                            seen_t.add(key)
                            truss_lines.append((foot_a, foot_b, pa, pb, ridge_pt, d_station, rs, ridge_unit, ridge_len, pa_lower, pb_lower, slope_change_edge_a, slope_change_edge_b))

                # Ceiling joists: equal spacing, always start and finish at bounds
                num_ceilings = int(math.ceil(span_len / ceiling_spacing)) if ceiling_spacing > 0 else 1
                num_ceilings = max(1, num_ceilings)
                actual_ceiling_spacing = span_len / num_ceilings
                
                for i in range(num_ceilings + 1):
                    d_station = min_d + i * actual_ceiling_spacing
                    ridge_pt = rs + ridge_unit.Multiply(d_station)
                    is_step_down = (d_station < 0.0 or d_station > ridge_len)
                    if is_step_down:
                        foot_a = _project_to_infinite_eave(ridge_pt, eave_a)
                        foot_b = _project_to_infinite_eave(ridge_pt, eave_b)
                    else:
                        foot_a = _project_to_best_eave(ridge_pt, eave_a, rs, re)
                        foot_b = _project_to_best_eave(ridge_pt, eave_b, rs, re)
                    if foot_a is None or foot_b is None:
                        continue
                    if _dist(foot_a, foot_b) >= MIN_MEMBER_LENGTH:
                        key = (_pt_key(foot_a), _pt_key(foot_b))
                        rkey = (_pt_key(foot_b), _pt_key(foot_a))
                        if key not in seen_c and rkey not in seen_c:
                            seen_c.add(key)
                            ceiling_lines.append((foot_a, foot_b))

        def is_near_any_truss(c_start, c_end):
            for t_line in truss_lines:
                t_start = t_line[0]
                t_end = t_line[1]
                dist_s = _dist(c_start, t_start)
                dist_e = _dist(c_end, t_end)
                if dist_s < inches_to_feet(2.0) and dist_e < inches_to_feet(2.0):
                    return True
            return False

        # 1. Generate Truss Elements
        for t_data in truss_lines:
            if len(t_data) >= 5:
                pa_lower = None
                pb_lower = None
                slope_change_edge_a = None
                slope_change_edge_b = None
                if len(t_data) >= 9:
                    foot_a, foot_b, pa, pb, ridge_pt, d_station, rs_val, ridge_unit_val, ridge_len_val = t_data[:9]
                    if len(t_data) == 13:
                        pa_lower, pb_lower, slope_change_edge_a, slope_change_edge_b = t_data[9:]
                else:
                    foot_a, foot_b, pa, pb, ridge_pt = t_data[:5]
                    d_station = 0.0
                    rs_val = None
                    ridge_unit_val = None
                    ridge_len_val = 0.0
                    
                joist_z = min(foot_a.Z, foot_b.Z)
                
                p_eave_a = XYZ(foot_a.X, foot_a.Y, joist_z)
                p_eave_b = XYZ(foot_b.X, foot_b.Y, joist_z)
                U_x = (p_eave_b - p_eave_a).Normalize()
                
                layer_depth_a = self._resolve_roof_layer_top_depth(pa_lower if pa_lower is not None else pa)
                layer_depth_b = self._resolve_roof_layer_top_depth(pb_lower if pb_lower is not None else pb)
                layer_depth_avg = (layer_depth_a + layer_depth_b) * 0.5
                
                # Calibrated 3D sloped offsets along roof normals
                p_tc_start_3d_L = foot_a - (pa_lower if pa_lower is not None else pa).normal.Multiply(layer_depth_a + tc_depth / 2.0)
                p_tc_end_3d_L = ridge_pt - pa.normal.Multiply(layer_depth_avg + tc_depth / 2.0)
                
                p_tc_start_3d_R = foot_b - (pb_lower if pb_lower is not None else pb).normal.Multiply(layer_depth_b + tc_depth / 2.0)
                p_tc_end_3d_R = ridge_pt - pb.normal.Multiply(layer_depth_avg + tc_depth / 2.0)
                
                intersections = self._find_supports_along_slice(p_eave_a, p_eave_b, walls, beams)
                if len(intersections) >= 2:
                    intersections.sort(key=lambda pt: (pt - p_eave_a).DotProduct(U_x))
                    support_a = intersections[0]
                    support_b = intersections[-1]
                else:
                    tot_len = (p_eave_b - p_eave_a).GetLength()
                    oh = min(1.5, tot_len * 0.15)
                    support_a = p_eave_a + U_x.Multiply(oh)
                    support_b = p_eave_b - U_x.Multiply(oh)
                    
                L = (support_b - support_a).GetLength()
                if L < MIN_MEMBER_LENGTH:
                    continue

                # Project directly to 2D local space to preserve un-approximated slope
                x_tc_start_L = (p_tc_start_3d_L - support_a).DotProduct(U_x)
                y_tc_start_L = p_tc_start_3d_L.Z - joist_z
                
                x_tc_end_L = (p_tc_end_3d_L - support_a).DotProduct(U_x)
                y_tc_end_L = p_tc_end_3d_L.Z - joist_z
                
                x_tc_start_R = (p_tc_start_3d_R - support_a).DotProduct(U_x)
                y_tc_start_R = p_tc_start_3d_R.Z - joist_z
                
                x_tc_end_R = (p_tc_end_3d_R - support_a).DotProduct(U_x)
                y_tc_end_R = p_tc_end_3d_R.Z - joist_z

                p_break_a = None
                if pa_lower is not None and slope_change_edge_a is not None and ridge_unit_val is not None:
                    s_edge, e_edge = slope_change_edge_a
                    edge_dir = e_edge - s_edge
                    denom = edge_dir.DotProduct(ridge_unit_val)
                    if abs(denom) > 1e-5:
                        t = (ridge_pt - s_edge).DotProduct(ridge_unit_val) / denom
                        if -0.05 <= t <= 1.05:
                            t = max(0.0, min(1.0, t))
                            p_break_a = s_edge + edge_dir.Multiply(t)

                p_break_b = None
                if pb_lower is not None and slope_change_edge_b is not None and ridge_unit_val is not None:
                    s_edge, e_edge = slope_change_edge_b
                    edge_dir = e_edge - s_edge
                    denom = edge_dir.DotProduct(ridge_unit_val)
                    if abs(denom) > 1e-5:
                        t = (ridge_pt - s_edge).DotProduct(ridge_unit_val) / denom
                        if -0.05 <= t <= 1.05:
                            t = max(0.0, min(1.0, t))
                            p_break_b = s_edge + edge_dir.Multiply(t)

                p_tc_break_3d_L = None
                if p_break_a is not None:
                    n_avg = (pa.normal + pa_lower.normal).Normalize()
                    p_tc_break_3d_L = p_break_a - n_avg.Multiply(layer_depth_a + tc_depth / 2.0)

                p_tc_break_3d_R = None
                if p_break_b is not None:
                    n_avg = (pb.normal + pb_lower.normal).Normalize()
                    p_tc_break_3d_R = p_break_b - n_avg.Multiply(layer_depth_b + tc_depth / 2.0)

                x_tc_break_L = (p_tc_break_3d_L - support_a).DotProduct(U_x) if p_tc_break_3d_L is not None else None
                y_tc_break_L = p_tc_break_3d_L.Z - joist_z if p_tc_break_3d_L is not None else None

                x_tc_break_R = (p_tc_break_3d_R - support_a).DotProduct(U_x) if p_tc_break_3d_R is not None else None
                y_tc_break_R = p_tc_break_3d_R.Z - joist_z if p_tc_break_3d_R is not None else None

                is_multi_slope = (p_tc_break_3d_L is not None or p_tc_break_3d_R is not None)

                # Slopes and intercepts
                if p_tc_break_3d_L is not None:
                    m_L_lower = (y_tc_break_L - y_tc_start_L) / (x_tc_break_L - x_tc_start_L) if abs(x_tc_break_L - x_tc_start_L) > 1e-5 else 0.0
                    C_L_lower = y_tc_start_L - m_L_lower * x_tc_start_L
                    m_L_upper = (y_tc_end_L - y_tc_break_L) / (x_tc_end_L - x_tc_break_L) if abs(x_tc_end_L - x_tc_break_L) > 1e-5 else 0.0
                    C_L_upper = y_tc_break_L - m_L_upper * x_tc_break_L
                else:
                    m_L_lower = (y_tc_end_L - y_tc_start_L) / (x_tc_end_L - x_tc_start_L) if abs(x_tc_end_L - x_tc_start_L) > 1e-5 else 0.0
                    C_L_lower = y_tc_start_L - m_L_lower * x_tc_start_L
                    m_L_upper = m_L_lower
                    C_L_upper = C_L_lower

                if p_tc_break_3d_R is not None:
                    m_R_lower = (y_tc_break_R - y_tc_start_R) / (x_tc_break_R - x_tc_start_R) if abs(x_tc_break_R - x_tc_start_R) > 1e-5 else 0.0
                    C_R_lower = y_tc_start_R - m_R_lower * x_tc_start_R
                    m_R_upper = (y_tc_end_R - y_tc_break_R) / (x_tc_end_R - x_tc_break_R) if abs(x_tc_end_R - x_tc_break_R) > 1e-5 else 0.0
                    C_R_upper = y_tc_break_R - m_R_upper * x_tc_break_R
                else:
                    m_R_lower = (y_tc_end_R - y_tc_start_R) / (x_tc_end_R - x_tc_start_R) if abs(x_tc_end_R - x_tc_start_R) > 1e-5 else 0.0
                    C_R_lower = y_tc_start_R - m_R_lower * x_tc_start_R
                    m_R_upper = m_R_lower
                    C_R_upper = C_R_lower

                # Heel and Ridge nodes
                y_bc = bc_depth / 2.0
                
                x_heel_L = (y_bc - C_L_lower) / m_L_lower if abs(m_L_lower) > 1e-5 else x_tc_start_L
                n_heel_L = (x_heel_L, y_bc)
                
                x_heel_R = (y_bc - C_R_lower) / m_R_lower if abs(m_R_lower) > 1e-5 else x_tc_start_R
                n_heel_R = (x_heel_R, y_bc)
                
                if abs(m_L_upper - m_R_upper) > 1e-5:
                    x_ridge_node = (C_R_upper - C_L_upper) / (m_L_upper - m_R_upper)
                    y_ridge_node = m_L_upper * x_ridge_node + C_L_upper
                else:
                    x_ridge_node = (x_tc_end_L + x_tc_end_R) * 0.5
                    y_ridge_node = (y_tc_end_L + y_tc_end_R) * 0.5
                n_ridge = (x_ridge_node, y_ridge_node)
                
                m_L = m_L_lower
                C_L = C_L_lower
                m_R = m_R_lower
                C_R = C_R_lower
                
                n_eave_L = (x_tc_start_L, y_tc_start_L)
                n_eave_R = (x_tc_start_R, y_tc_start_R)

                # Check for step down hip intersections
                p_hip_L = None
                p_hip_R = None
                if rs_val is not None and ridge_unit_val is not None:
                    is_step_down = (d_station < 0.0 or d_station > ridge_len_val)
                    if is_step_down:
                        hip_pts = []
                        for rs_edge, re_edge, pa_edge, pb_edge in ridge_edges:
                            if abs(re_edge.Z - rs_edge.Z) >= RIDGE_TOL:
                                edge_dir = re_edge - rs_edge
                                denom_proj = edge_dir.DotProduct(ridge_unit_val)
                                if abs(denom_proj) > 1e-5:
                                    t_edge = (ridge_pt - rs_edge).DotProduct(ridge_unit_val) / denom_proj
                                    if -0.05 <= t_edge <= 1.05:
                                        t_edge = max(0.0, min(1.0, t_edge))
                                        p_int = rs_edge + edge_dir.Multiply(t_edge)
                                        hip_pts.append(p_int)
                        if len(hip_pts) >= 2:
                            hip_pts.sort(key=lambda pt: (pt - foot_a).DotProduct(U_x))
                            p_hip_L = hip_pts[0]
                            p_hip_R = hip_pts[-1]

                if p_hip_L is not None and p_hip_R is not None:
                    # Flat-Top Step-Down Truss
                    p_hip_L_shifted = p_hip_L - pa.normal.Multiply(layer_depth_a + tc_depth / 2.0)
                    p_hip_R_shifted = p_hip_R - pb.normal.Multiply(layer_depth_b + tc_depth / 2.0)
                    
                    x_hip_L = (p_hip_L_shifted - support_a).DotProduct(U_x)
                    y_hip_L = p_hip_L_shifted.Z - joist_z
                    
                    x_hip_R = (p_hip_R_shifted - support_a).DotProduct(U_x)
                    y_hip_R = p_hip_R_shifted.Z - joist_z
                    
                    n_hip_L_node = (x_hip_L, y_hip_L)
                    n_hip_R_node = (x_hip_R, y_hip_R)
                    
                    nodes = {
                        "heel_L": n_heel_L,
                        "heel_R": n_heel_R,
                        "hip_L": n_hip_L_node,
                        "hip_R": n_hip_R_node,
                        "bot_L_web": (x_hip_L, y_bc),
                        "bot_R_web": (x_hip_R, y_bc),
                        "top_mid": ((x_hip_L + x_hip_R) * 0.5, (y_hip_L + y_hip_R) * 0.5),
                        "bot_mid": ((x_hip_L + x_hip_R) * 0.5, y_bc)
                    }
                    
                    webs = [
                        ("bot_L_web", "hip_L"),
                        ("bot_R_web", "hip_R"),
                        ("bot_mid", "top_mid"),
                        ("bot_L_web", "top_mid"),
                        ("bot_R_web", "top_mid")
                    ]
                    
                    def to_3d(n2d):
                        return support_a + U_x.Multiply(n2d[0]) + XYZ.BasisZ.Multiply(n2d[1])
                    
                    p3d_heel_L = to_3d(n_heel_L)
                    p3d_heel_R = to_3d(n_heel_R)
                    p3d_eave_L = to_3d(n_eave_L)
                    p3d_eave_R = to_3d(n_eave_R)
                    p3d_hip_L = to_3d(n_hip_L_node)
                    p3d_hip_R = to_3d(n_hip_R_node)
                    
                    # Bottom Chord
                    m_bc = FramingMember(FramingMember.STUD, p3d_heel_L, p3d_heel_R)
                    m_bc.member_type = "BOTTOM_CHORD"
                    m_bc.family_name = family_bottom[0]
                    m_bc.type_name = family_bottom[1]
                    m_bc.rotation = -math.pi / 2.0
                    m_bc.host_kind = roof_info.kind
                    m_bc.host_id = roof_info.element_id
                    m_bc.disallow_end_joins = True
                    members.append(m_bc)
                    
                    # Left Top Chord
                    if p_tc_break_3d_L is not None and x_hip_L >= x_tc_break_L:
                        p3d_break_L = to_3d((x_tc_break_L, y_tc_break_L))
                        nodes["break_L"] = (x_tc_break_L, y_tc_break_L)
                        nodes["bot_break_L"] = (x_tc_break_L, y_bc)
                        webs.append(("bot_break_L", "break_L"))
                        
                        m_tc_L1 = FramingMember(FramingMember.STUD, p3d_eave_L, p3d_break_L)
                        m_tc_L1.member_type = "TOP_CHORD"
                        m_tc_L1.family_name = family_top[0]
                        m_tc_L1.type_name = family_top[1]
                        m_tc_L1.rotation = _rotation_from_up(p3d_break_L - p3d_eave_L, (pa_lower if pa_lower is not None else pa).normal)
                        m_tc_L1.host_kind = roof_info.kind
                        m_tc_L1.host_id = roof_info.element_id
                        m_tc_L1.disallow_end_joins = True
                        members.append(m_tc_L1)
                        
                        m_tc_L2 = FramingMember(FramingMember.STUD, p3d_break_L, p3d_hip_L)
                        m_tc_L2.member_type = "TOP_CHORD"
                        m_tc_L2.family_name = family_top[0]
                        m_tc_L2.type_name = family_top[1]
                        m_tc_L2.rotation = _rotation_from_up(p3d_hip_L - p3d_break_L, pa.normal)
                        m_tc_L2.host_kind = roof_info.kind
                        m_tc_L2.host_id = roof_info.element_id
                        m_tc_L2.disallow_end_joins = True
                        members.append(m_tc_L2)
                    else:
                        m_tc_L = FramingMember(FramingMember.STUD, p3d_eave_L, p3d_hip_L)
                        m_tc_L.member_type = "TOP_CHORD"
                        m_tc_L.family_name = family_top[0]
                        m_tc_L.type_name = family_top[1]
                        normal_L = (pa_lower if pa_lower is not None else pa).normal if p_tc_break_3d_L is not None else pa.normal
                        m_tc_L.rotation = _rotation_from_up(p3d_hip_L - p3d_eave_L, normal_L)
                        m_tc_L.host_kind = roof_info.kind
                        m_tc_L.host_id = roof_info.element_id
                        m_tc_L.disallow_end_joins = True
                        members.append(m_tc_L)
                        
                    # Right Top Chord
                    if p_tc_break_3d_R is not None and x_hip_R <= x_tc_break_R:
                        p3d_break_R = to_3d((x_tc_break_R, y_tc_break_R))
                        nodes["break_R"] = (x_tc_break_R, y_tc_break_R)
                        nodes["bot_break_R"] = (x_tc_break_R, y_bc)
                        webs.append(("bot_break_R", "break_R"))
                        
                        m_tc_R1 = FramingMember(FramingMember.STUD, p3d_eave_R, p3d_break_R)
                        m_tc_R1.member_type = "TOP_CHORD"
                        m_tc_R1.family_name = family_top[0]
                        m_tc_R1.type_name = family_top[1]
                        m_tc_R1.rotation = _rotation_from_up(p3d_break_R - p3d_eave_R, (pb_lower if pb_lower is not None else pb).normal)
                        m_tc_R1.host_kind = roof_info.kind
                        m_tc_R1.host_id = roof_info.element_id
                        m_tc_R1.disallow_end_joins = True
                        members.append(m_tc_R1)
                        
                        m_tc_R2 = FramingMember(FramingMember.STUD, p3d_break_R, p3d_hip_R)
                        m_tc_R2.member_type = "TOP_CHORD"
                        m_tc_R2.family_name = family_top[0]
                        m_tc_R2.type_name = family_top[1]
                        m_tc_R2.rotation = _rotation_from_up(p3d_hip_R - p3d_break_R, pb.normal)
                        m_tc_R2.host_kind = roof_info.kind
                        m_tc_R2.host_id = roof_info.element_id
                        m_tc_R2.disallow_end_joins = True
                        members.append(m_tc_R2)
                    else:
                        m_tc_R = FramingMember(FramingMember.STUD, p3d_eave_R, p3d_hip_R)
                        m_tc_R.member_type = "TOP_CHORD"
                        m_tc_R.family_name = family_top[0]
                        m_tc_R.type_name = family_top[1]
                        normal_R = (pb_lower if pb_lower is not None else pb).normal if p_tc_break_3d_R is not None else pb.normal
                        m_tc_R.rotation = _rotation_from_up(p3d_hip_R - p3d_eave_R, normal_R)
                        m_tc_R.host_kind = roof_info.kind
                        m_tc_R.host_id = roof_info.element_id
                        m_tc_R.disallow_end_joins = True
                        members.append(m_tc_R)
                        
                    # Flat Top Chord
                    m_tc_flat = FramingMember(FramingMember.STUD, p3d_hip_L, p3d_hip_R)
                    m_tc_flat.member_type = "TOP_CHORD"
                    m_tc_flat.family_name = family_top[0]
                    m_tc_flat.type_name = family_top[1]
                    m_tc_flat.rotation = -math.pi / 2.0
                    m_tc_flat.host_kind = roof_info.kind
                    m_tc_flat.host_id = roof_info.element_id
                    m_tc_flat.disallow_end_joins = True
                    members.append(m_tc_flat)
                    
                    # Webs
                    for w_start_key, w_end_key in webs:
                        w_start_3d = to_3d(nodes[w_start_key])
                        w_end_3d = to_3d(nodes[w_end_key])
                        m_web = FramingMember(FramingMember.STUD, w_start_3d, w_end_3d)
                        m_web.member_type = "WEB_BRACING"
                        m_web.family_name = family_web[0]
                        m_web.type_name = family_web[1]
                        m_web.rotation = _rotation_from_up(w_end_3d - w_start_3d, pa.normal)
                        m_web.host_kind = roof_info.kind
                        m_web.host_id = roof_info.element_id
                        m_web.disallow_end_joins = True
                        members.append(m_web)
                        
                    continue

                nodes = {}
                webs = []

                if is_multi_slope:
                    # Gambrel/Mansard Multi-Slope Topology
                    nodes["heel_L"] = n_heel_L
                    nodes["heel_R"] = n_heel_R
                    nodes["ridge"] = n_ridge
                    
                    if p_tc_break_3d_L is not None:
                        nodes["break_L"] = (x_tc_break_L, y_tc_break_L)
                        nodes["bot_break_L"] = (x_tc_break_L, y_bc)
                    else:
                        mid_x_L = (x_heel_L + x_ridge_node) * 0.5
                        nodes["break_L"] = (mid_x_L, m_L_upper * mid_x_L + C_L_upper)
                        nodes["bot_break_L"] = (mid_x_L, y_bc)
                        
                    if p_tc_break_3d_R is not None:
                        nodes["break_R"] = (x_tc_break_R, y_tc_break_R)
                        nodes["bot_break_R"] = (x_tc_break_R, y_bc)
                    else:
                        mid_x_R = (x_heel_R + x_ridge_node) * 0.5
                        nodes["break_R"] = (mid_x_R, m_R_upper * mid_x_R + C_R_upper)
                        nodes["bot_break_R"] = (mid_x_R, y_bc)
                        
                    nodes["bot_mid"] = (x_ridge_node, y_bc)
                    
                    webs = [
                        ("bot_break_L", "break_L"),
                        ("bot_break_R", "break_R"),
                        ("bot_mid", "ridge"),
                        ("bot_mid", "break_L"),
                        ("bot_mid", "break_R")
                    ]
                    
                    def to_3d(n2d):
                        return support_a + U_x.Multiply(n2d[0]) + XYZ.BasisZ.Multiply(n2d[1])
                    
                    p3d_heel_L = to_3d(nodes["heel_L"])
                    p3d_heel_R = to_3d(nodes["heel_R"])
                    p3d_break_L = to_3d(nodes["break_L"])
                    p3d_break_R = to_3d(nodes["break_R"])
                    p3d_ridge = to_3d(nodes["ridge"])
                    p3d_eave_L = to_3d(n_eave_L)
                    p3d_eave_R = to_3d(n_eave_R)
                    
                    # Bottom Chord
                    m_bc = FramingMember(FramingMember.STUD, p3d_heel_L, p3d_heel_R)
                    m_bc.member_type = "BOTTOM_CHORD"
                    m_bc.family_name = family_bottom[0]
                    m_bc.type_name = family_bottom[1]
                    m_bc.rotation = -math.pi / 2.0
                    m_bc.host_kind = roof_info.kind
                    m_bc.host_id = roof_info.element_id
                    m_bc.disallow_end_joins = True
                    members.append(m_bc)
                    
                    left_end_pt = p3d_ridge
                    right_end_pt = p3d_ridge
                    
                    if ridge_edges:
                        for rs_edge, re_edge, _, _ in ridge_edges:
                            left_end_pt = self._shorten_to_support_face(p3d_break_L, left_end_pt, rs_edge, re_edge, edge_width)
                            right_end_pt = self._shorten_to_support_face(p3d_break_R, right_end_pt, rs_edge, re_edge, edge_width)
                    
                    # Left Lower Top Chord
                    m_tc_L1 = FramingMember(FramingMember.STUD, p3d_eave_L, p3d_break_L)
                    m_tc_L1.member_type = "TOP_CHORD"
                    m_tc_L1.family_name = family_top[0]
                    m_tc_L1.type_name = family_top[1]
                    m_tc_L1.rotation = _rotation_from_up(p3d_break_L - p3d_eave_L, (pa_lower if pa_lower is not None else pa).normal)
                    m_tc_L1.host_kind = roof_info.kind
                    m_tc_L1.host_id = roof_info.element_id
                    m_tc_L1.disallow_end_joins = True
                    members.append(m_tc_L1)
                    
                    # Left Upper Top Chord
                    m_tc_L2 = FramingMember(FramingMember.STUD, p3d_break_L, left_end_pt)
                    m_tc_L2.member_type = "TOP_CHORD"
                    m_tc_L2.family_name = family_top[0]
                    m_tc_L2.type_name = family_top[1]
                    m_tc_L2.rotation = _rotation_from_up(left_end_pt - p3d_break_L, pa.normal)
                    m_tc_L2.host_kind = roof_info.kind
                    m_tc_L2.host_id = roof_info.element_id
                    m_tc_L2.disallow_end_joins = True
                    members.append(m_tc_L2)
                    
                    # Right Lower Top Chord
                    m_tc_R1 = FramingMember(FramingMember.STUD, p3d_eave_R, p3d_break_R)
                    m_tc_R1.member_type = "TOP_CHORD"
                    m_tc_R1.family_name = family_top[0]
                    m_tc_R1.type_name = family_top[1]
                    m_tc_R1.rotation = _rotation_from_up(p3d_break_R - p3d_eave_R, (pb_lower if pb_lower is not None else pb).normal)
                    m_tc_R1.host_kind = roof_info.kind
                    m_tc_R1.host_id = roof_info.element_id
                    m_tc_R1.disallow_end_joins = True
                    members.append(m_tc_R1)
                    
                    # Right Upper Top Chord
                    m_tc_R2 = FramingMember(FramingMember.STUD, p3d_break_R, right_end_pt)
                    m_tc_R2.member_type = "TOP_CHORD"
                    m_tc_R2.family_name = family_top[0]
                    m_tc_R2.type_name = family_top[1]
                    m_tc_R2.rotation = _rotation_from_up(right_end_pt - p3d_break_R, pb.normal)
                    m_tc_R2.host_kind = roof_info.kind
                    m_tc_R2.host_id = roof_info.element_id
                    m_tc_R2.disallow_end_joins = True
                    members.append(m_tc_R2)
                    
                    # Webs
                    for w_start_key, w_end_key in webs:
                        w_start_3d = to_3d(nodes[w_start_key])
                        w_end_3d = to_3d(nodes[w_end_key])
                        m_web = FramingMember(FramingMember.STUD, w_start_3d, w_end_3d)
                        m_web.member_type = "WEB_BRACING"
                        m_web.family_name = family_web[0]
                        m_web.type_name = family_web[1]
                        m_web.rotation = _rotation_from_up(w_end_3d - w_start_3d, pa.normal)
                        m_web.host_kind = roof_info.kind
                        m_web.host_id = roof_info.element_id
                        m_web.disallow_end_joins = True
                        members.append(m_web)
                        
                else:
                    # Standard Single-Slope Truss
                    span_node = x_heel_R - x_heel_L
                    topology = getattr(self.config, 'truss_type', 'Dynamic')
                    if topology == "Dynamic":
                        if span_node < 16.0:
                            topology = "KingPost"
                        elif span_node <= 28.0:
                            topology = "Fink"
                        else:
                            topology = "Pratt"
      
                    if topology == "KingPost":
                        # KingPost Topology
                        nodes["heel_L"] = n_heel_L
                        nodes["heel_R"] = n_heel_R
                        nodes["ridge"] = n_ridge
                        nodes["bot_mid"] = (x_heel_L + span_node / 2.0, y_bc)
                        webs.append(("bot_mid", "ridge"))
                    elif topology == "Fink":
                        # Fink Topology
                        nodes["heel_L"] = n_heel_L
                        nodes["heel_R"] = n_heel_R
                        nodes["ridge"] = n_ridge
                        nodes["bot_mid"] = (x_heel_L + span_node / 2.0, y_bc)
                        nodes["bot_L_third"] = (x_heel_L + span_node / 3.0, y_bc)
                        nodes["bot_R_third"] = (x_heel_L + 2.0 * span_node / 3.0, y_bc)
                        nodes["top_L_mid"] = ((n_heel_L[0] + n_ridge[0]) / 2.0, (n_heel_L[1] + n_ridge[1]) / 2.0)
                        nodes["top_R_mid"] = ((n_heel_R[0] + n_ridge[0]) / 2.0, (n_heel_R[1] + n_ridge[1]) / 2.0)
                        webs.append(("bot_mid", "top_L_mid"))
                        webs.append(("bot_L_third", "top_L_mid"))
                        webs.append(("bot_mid", "top_R_mid"))
                        webs.append(("bot_R_third", "top_R_mid"))
                    else:
                        # Pratt Topology (6 panels)
                        nodes["heel_L"] = n_heel_L
                        nodes["heel_R"] = n_heel_R
                        nodes["ridge"] = n_ridge
                        
                        dx = span_node / 6.0
                        for i in range(1, 6):
                            nodes["bot_{0}".format(i)] = (x_heel_L + i * dx, y_bc)
                        
                        nodes["top_1"] = (x_heel_L + dx, m_L * (x_heel_L + dx) + C_L)
                        nodes["top_2"] = (x_heel_L + 2.0 * dx, m_L * (x_heel_L + 2.0 * dx) + C_L)
                        nodes["top_4"] = (x_heel_L + 4.0 * dx, m_R * (x_heel_L + 4.0 * dx) + C_R)
                        nodes["top_5"] = (x_heel_L + 5.0 * dx, m_R * (x_heel_L + 5.0 * dx) + C_R)
                        
                        webs.append(("top_1", "bot_1"))
                        webs.append(("top_2", "bot_2"))
                        webs.append(("ridge", "bot_3"))
                        webs.append(("top_4", "bot_4"))
                        webs.append(("top_5", "bot_5"))
                        
                        webs.append(("heel_L", "top_1"))
                        webs.append(("top_1", "bot_2"))
                        webs.append(("top_2", "bot_3"))
                        webs.append(("top_4", "bot_3"))
                        webs.append(("top_5", "bot_4"))
                        webs.append(("heel_R", "top_5"))
                    
                    def to_3d(n2d):
                        return support_a + U_x.Multiply(n2d[0]) + XYZ.BasisZ.Multiply(n2d[1])
                    
                    p3d_heel_L = to_3d(n_heel_L)
                    p3d_heel_R = to_3d(n_heel_R)
                    p3d_ridge = to_3d(n_ridge)
                    p3d_eave_L = to_3d(n_eave_L)
                    p3d_eave_R = to_3d(n_eave_R)
                    
                    m_bc = FramingMember(FramingMember.STUD, p3d_heel_L, p3d_heel_R)
                    m_bc.member_type = "BOTTOM_CHORD"
                    m_bc.family_name = family_bottom[0]
                    m_bc.type_name = family_bottom[1]
                    m_bc.rotation = -math.pi / 2.0
                    m_bc.host_kind = roof_info.kind
                    m_bc.host_id = roof_info.element_id
                    m_bc.disallow_end_joins = True
                    members.append(m_bc)
                    
                    left_end_pt = p3d_ridge
                    right_end_pt = p3d_ridge
                    
                    if ridge_edges:
                        for rs_edge, re_edge, _, _ in ridge_edges:
                            left_end_pt = self._shorten_to_support_face(p3d_eave_L, left_end_pt, rs_edge, re_edge, edge_width)
                            right_end_pt = self._shorten_to_support_face(p3d_eave_R, right_end_pt, rs_edge, re_edge, edge_width)
                            
                    m_tc_L = FramingMember(FramingMember.STUD, p3d_eave_L, left_end_pt)
                    m_tc_L.member_type = "TOP_CHORD"
                    m_tc_L.family_name = family_top[0]
                    m_tc_L.type_name = family_top[1]
                    m_tc_L.rotation = _rotation_from_up(left_end_pt - p3d_eave_L, pa.normal)
                    m_tc_L.host_kind = roof_info.kind
                    m_tc_L.host_id = roof_info.element_id
                    m_tc_L.disallow_end_joins = True
                    members.append(m_tc_L)
                    
                    m_tc_R = FramingMember(FramingMember.STUD, p3d_eave_R, right_end_pt)
                    m_tc_R.member_type = "TOP_CHORD"
                    m_tc_R.family_name = family_top[0]
                    m_tc_R.type_name = family_top[1]
                    m_tc_R.rotation = _rotation_from_up(right_end_pt - p3d_eave_R, pb.normal)
                    m_tc_R.host_kind = roof_info.kind
                    m_tc_R.host_id = roof_info.element_id
                    m_tc_R.disallow_end_joins = True
                    members.append(m_tc_R)
                    
                    for w_start_key, w_end_key in webs:
                        w_start_2d = nodes[w_start_key]
                        w_end_2d = nodes[w_end_key]
                        w_start_3d = to_3d(w_start_2d)
                        w_end_3d = to_3d(w_end_2d)
                        
                        m_web = FramingMember(FramingMember.STUD, w_start_3d, w_end_3d)
                        m_web.member_type = "WEB_BRACING"
                        m_web.family_name = family_web[0]
                        m_web.type_name = family_web[1]
                        m_web.rotation = _rotation_from_up(w_end_3d - w_start_3d, pa.normal)
                        m_web.host_kind = roof_info.kind
                        m_web.host_id = roof_info.element_id
                        m_web.disallow_end_joins = True
                        members.append(m_web)
                    
            else:
                # Monoslope Truss (Shed Roof)
                s_pt, e_pt, plane = t_data
                joist_z = min(s_pt.Z, e_pt.Z)
                
                p_eave_low = XYZ(s_pt.X, s_pt.Y, joist_z)
                p_eave_high = XYZ(e_pt.X, e_pt.Y, joist_z)
                U_x = (p_eave_high - p_eave_low).Normalize()
                
                layer_depth = self._resolve_roof_layer_top_depth(plane)
                
                # Calibrated 3D sloped offsets along roof normal
                p_tc_start_3d = s_pt - plane.normal.Multiply(layer_depth + tc_depth / 2.0)
                p_tc_end_3d = e_pt - plane.normal.Multiply(layer_depth + tc_depth / 2.0)
                
                intersections = self._find_supports_along_slice(p_eave_low, p_eave_high, walls, beams)
                if len(intersections) >= 2:
                    intersections.sort(key=lambda pt: (pt - p_eave_low).DotProduct(U_x))
                    support_low = intersections[0]
                    support_high = intersections[-1]
                else:
                    tot_len = (p_eave_high - p_eave_low).GetLength()
                    oh = min(1.5, tot_len * 0.15)
                    support_low = p_eave_low + U_x.Multiply(oh)
                    support_high = p_eave_high - U_x.Multiply(oh)
                    
                L = (support_high - support_low).GetLength()
                if L < MIN_MEMBER_LENGTH:
                    continue
                
                x_tc_start = (p_tc_start_3d - support_low).DotProduct(U_x)
                y_tc_start = p_tc_start_3d.Z - joist_z
                
                x_tc_end = (p_tc_end_3d - support_low).DotProduct(U_x)
                y_tc_end = p_tc_end_3d.Z - joist_z
                
                m = (y_tc_end - y_tc_start) / (x_tc_end - x_tc_start) if abs(x_tc_end - x_tc_start) > 1e-5 else 0.0
                theta = math.atan(m)
                
                y_bc = bc_depth / 2.0
                C = y_tc_start - m * x_tc_start
                
                x_heel_L = (y_bc - C) / m if abs(m) > 1e-5 else x_tc_start
                n_heel_L = (x_heel_L, y_bc)
                
                n_heel_R = (L, y_bc)
                y_top_high = m * L + C
                n_top_R = (L, y_top_high)
                
                n_eave_low = (x_tc_start, y_tc_start)
                n_eave_high = (x_tc_end, y_tc_end)
                
                span_node = L - x_heel_L
                if span_node < 12.0:
                    N_panels = 2
                elif span_node <= 24.0:
                    N_panels = 3
                else:
                    N_panels = 4
                
                dx = span_node / N_panels
                nodes = {}
                webs = []
                
                for i in range(N_panels + 1):
                    x_i = x_heel_L + i * dx
                    nodes["bot_{0}".format(i)] = (x_i, y_bc)
                    nodes["top_{0}".format(i)] = (x_i, m * x_i + C)
                    
                    if i > 0:
                        webs.append(("top_{0}".format(i), "bot_{0}".format(i)))
                        webs.append(("bot_{0}".format(i-1), "top_{0}".format(i)))
                
                def to_3d(n2d):
                    return support_low + U_x.Multiply(n2d[0]) + XYZ.BasisZ.Multiply(n2d[1])
                
                p3d_heel_L = to_3d(n_heel_L)
                p3d_heel_R = to_3d(n_heel_R)
                p3d_eave_low = to_3d(n_eave_low)
                p3d_eave_high = to_3d(n_eave_high)
                
                m_bc = FramingMember(FramingMember.STUD, p3d_heel_L, p3d_heel_R)
                m_bc.member_type = "BOTTOM_CHORD"
                m_bc.family_name = family_bottom[0]
                m_bc.type_name = family_bottom[1]
                m_bc.rotation = -math.pi / 2.0
                m_bc.host_kind = roof_info.kind
                m_bc.host_id = roof_info.element_id
                m_bc.disallow_end_joins = True
                members.append(m_bc)
                
                m_tc = FramingMember(FramingMember.STUD, p3d_eave_low, p3d_eave_high)
                m_tc.member_type = "TOP_CHORD"
                m_tc.family_name = family_top[0]
                m_tc.type_name = family_top[1]
                m_tc.rotation = _rotation_from_up(p3d_eave_high - p3d_eave_low, plane.normal)
                m_tc.host_kind = roof_info.kind
                m_tc.host_id = roof_info.element_id
                m_tc.disallow_end_joins = True
                members.append(m_tc)
                
                for w_start_key, w_end_key in webs:
                    w_start_3d = to_3d(nodes[w_start_key])
                    w_end_3d = to_3d(nodes[w_end_key])
                    m_web = FramingMember(FramingMember.STUD, w_start_3d, w_end_3d)
                    m_web.member_type = "WEB_BRACING"
                    m_web.family_name = family_web[0]
                    m_web.type_name = family_web[1]
                    m_web.rotation = _rotation_from_up(w_end_3d - w_start_3d, plane.normal)
                    m_web.host_kind = roof_info.kind
                    m_web.host_id = roof_info.element_id
                    m_web.disallow_end_joins = True
                    members.append(m_web)

        # 2. Generate Ceiling Joists (Grid)
        for c_data in ceiling_lines:
            c_start, c_end = c_data
            
            if is_near_any_truss(c_start, c_end):
                continue
                
            joist_z = min(c_start.Z, c_end.Z)
            p_joist_start = XYZ(c_start.X, c_start.Y, joist_z + bc_depth / 2.0)
            p_joist_end = XYZ(c_end.X, c_end.Y, joist_z + bc_depth / 2.0)
            
            m_cj = FramingMember(FramingMember.STUD, p_joist_start, p_joist_end)
            m_cj.member_type = "CEILING_JOIST"
            m_cj.family_name = family_bottom[0]
            m_cj.type_name = family_bottom[1]
            m_cj.rotation = -math.pi / 2.0
            m_cj.host_kind = roof_info.kind
            m_cj.host_id = roof_info.element_id
            m_cj.disallow_end_joins = True
            members.append(m_cj)

        # 3. Generate Ridge and Hip Boards
        try:
            members.extend(self._make_ridge_boards(ridge_edges, roof_info))
        except Exception:
            pass

        # 4. Generate Jack Rafters on Hip End Planes (planes not adjacent to any horizontal ridge)
        try:
            horizontal_planes = set()
            for t_line in truss_lines:
                if len(t_line) == 13:
                    _, _, pa_edge, pb_edge, _, _, _, _, _, pa_lower_edge, pb_lower_edge, _, _ = t_line
                    horizontal_planes.add(pa_edge)
                    horizontal_planes.add(pb_edge)
                    if pa_lower_edge is not None:
                        horizontal_planes.add(pa_lower_edge)
                    if pb_lower_edge is not None:
                        horizontal_planes.add(pb_lower_edge)
                elif len(t_line) >= 4:
                    _, _, pa_edge, pb_edge = t_line[:4]
                    horizontal_planes.add(pa_edge)
                    horizontal_planes.add(pb_edge)
                elif len(t_line) == 3:
                    horizontal_planes.add(t_line[2])

            for plane in planes:
                if plane.normal.Z >= FLAT_THRESHOLD:
                    continue
                if plane not in horizontal_planes:
                    rafters = self._make_rafters_for_plane(plane, ridge_edges, roof_info)
                    members.extend(rafters)
        except Exception:
            pass

        return members
