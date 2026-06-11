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


def _get_non_ridge_edges(plane, ridge_edges):
    """Return all boundary edges of a plane that are NOT ridge edges."""
    edges = []
    for loop in plane.boundary_loops_local:
        count = len(loop)
        for i in range(count):
            s_local = loop[i]
            e_local = loop[(i + 1) % count]
            s_world = _surface_point(plane, s_local[0], s_local[1])
            e_world = _surface_point(plane, e_local[0], e_local[1])
            
            is_ridge = False
            for rs, re, pa, pb in ridge_edges:
                if ((_dist(s_world, rs) < 1.5 and _dist(e_world, re) < 1.5) or
                    (_dist(s_world, re) < 1.5 and _dist(e_world, rs) < 1.5)):
                    is_ridge = True
                    break
            if not is_ridge:
                edges.append((s_world, e_world))
    return edges


def _classify_boundary_edges(plane, ridge_edges, ridge_segment=None):
    """Classify non-ridge boundary edges into eave, rake, and ledger."""
    non_ridge = _get_non_ridge_edges(plane, ridge_edges)
    if not non_ridge:
        return {"eave": [], "rake": [], "ledger": []}

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
            ridge_dir = _normalize(plane.x_axis)

    if ridge_dir is None:
        return {"eave": non_ridge, "rake": [], "ledger": []}

    parallel = []
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
#  Topology-Driven Constraint Solvers
# ======================================================================

def intersect_three_planes(n1, d1, n2, d2, n3, d3):
    from Autodesk.Revit.DB import XYZ
    denom = n1.DotProduct(n2.CrossProduct(n3))
    if abs(denom) < 1e-6:
        return None
    cross23 = n2.CrossProduct(n3)
    cross31 = n3.CrossProduct(n1)
    cross12 = n1.CrossProduct(n2)
    
    term1 = cross23.Multiply(-d1)
    term2 = cross31.Multiply(-d2)
    term3 = cross12.Multiply(-d3)
    
    sum_terms = term1 + term2 + term3
    return sum_terms.Multiply(1.0 / denom)


def find_nearest_edge_in_face(pt_local, loops_local, edges, plane_info):
    best_edge = None
    min_dist = 1e9
    for loop in loops_local:
        count = len(loop)
        for i in range(count):
            s_loc = loop[i]
            e_loc = loop[(i + 1) % count]
            d = _dist_point_to_segment_2d(pt_local, s_loc, e_loc)
            if d < min_dist:
                min_dist = d
                best_edge = (s_loc, e_loc)
                
    if best_edge is None:
        return None
        
    s_loc, e_loc = best_edge
    sw = _surface_point(plane_info, s_loc[0], s_loc[1])
    ew = _surface_point(plane_info, e_loc[0], e_loc[1])
    
    for edge in edges:
        d1 = _dist(edge.node_a.pt, sw) + _dist(edge.node_b.pt, ew)
        d2 = _dist(edge.node_a.pt, ew) + _dist(edge.node_b.pt, sw)
        if d1 < 0.1 or d2 < 0.1:
            return edge
            
    return None


class RoofNode(object):
    def __init__(self, key, pt):
        self.key = key
        self.pt = pt
        self.edges = set()
        self.faces = set()


class RoofEdge(object):
    def __init__(self, node_a, node_b):
        self.node_a = node_a
        self.node_b = node_b
        self.edge_type = None  # RIDGE, HIP, VALLEY, EAVE, RAKE
        self.faces = set()


class RoofFace(object):
    def __init__(self, plane_info, index):
        self.plane_info = plane_info
        self.index = index
        self.normal = plane_info.normal
        self.d = -plane_info.normal.DotProduct(plane_info.origin)
        self.d_shifted = self.d
        self.loops_nodes = []
        self.edges = []


class RoofTopologyGraph(object):
    def __init__(self, planes, snap_tolerance=0.05):
        self.nodes = {}
        self.edges = {}
        self.faces = []
        self.snap_tolerance = snap_tolerance
        
        # 1. Build faces
        for idx, plane in enumerate(planes):
            if plane.normal.Z >= FLAT_THRESHOLD:
                continue
            face = RoofFace(plane, idx)
            self.faces.append(face)
            
        # 2. Extract and map loops to nodes
        for face in self.faces:
            for loop in face.plane_info.boundary_loops_local:
                loop_nodes = []
                for pt_local in loop:
                    pt_world = _surface_point(face.plane_info, pt_local[0], pt_local[1])
                    node = self._get_or_create_node(pt_world)
                    loop_nodes.append(node)
                if len(loop_nodes) >= 3:
                    face.loops_nodes.append(loop_nodes)
                    
        # 3. Create edges
        for face in self.faces:
            for loop_nodes in face.loops_nodes:
                count = len(loop_nodes)
                for i in range(count):
                    n_a = loop_nodes[i]
                    n_b = loop_nodes[(i + 1) % count]
                    edge = self._get_or_create_edge(n_a, n_b)
                    edge.faces.add(face)
                    face.edges.append(edge)
                    n_a.faces.add(face)
                    n_b.faces.add(face)
                    
    def _get_or_create_node(self, pt):
        for node in self.nodes.values():
            if _dist(node.pt, pt) < self.snap_tolerance:
                return node
        key = _pt_key(pt)
        node = RoofNode(key, pt)
        self.nodes[key] = node
        return node
        
    def _get_or_create_edge(self, n_a, n_b):
        key = (min(n_a.key, n_b.key), max(n_a.key, n_b.key))
        if key in self.edges:
            return self.edges[key]
        edge = RoofEdge(n_a, n_b)
        self.edges[key] = edge
        n_a.edges.add(edge)
        n_b.edges.add(edge)
        return edge

    def solve_geometry(self):
        # Solve each node
        solved_pts = {}
        for key, node in self.nodes.items():
            planes = []
            for face in node.faces:
                planes.append((face.normal, face.d))
                
            solved_pt = None
            if len(planes) >= 3:
                for i in range(len(planes)):
                    for j in range(i + 1, len(planes)):
                        for k in range(j + 1, len(planes)):
                            n1, d1 = planes[i]
                            n2, d2 = planes[j]
                            n3, d3 = planes[k]
                            pt = intersect_three_planes(n1, d1, n2, d2, n3, d3)
                            if pt is not None:
                                solved_pt = pt
                                break
                        if solved_pt is not None:
                            break
                    if solved_pt is not None:
                        break
            
            if solved_pt is None and len(planes) == 2:
                n1, d1 = planes[0]
                n2, d2 = planes[1]
                u = _normalize(n1.CrossProduct(n2))
                if u is not None:
                    n3 = u
                    d3 = -u.DotProduct(node.pt)
                    solved_pt = intersect_three_planes(n1, d1, n2, d2, n3, d3)
                    
            if solved_pt is None and len(planes) >= 1:
                n1, d1 = planes[0]
                dist_to_plane = n1.DotProduct(node.pt) + d1
                solved_pt = node.pt - n1.Multiply(dist_to_plane)
                
            if solved_pt is not None:
                solved_pts[key] = solved_pt
            else:
                solved_pts[key] = node.pt
                
        for key, solved_pt in solved_pts.items():
            self.nodes[key].pt = solved_pt

    def classify_edges(self):
        for edge in self.edges.values():
            if len(edge.faces) == 2:
                face_list = list(edge.faces)
                fa, fb = face_list[0], face_list[1]
                z_diff = abs(edge.node_a.pt.Z - edge.node_b.pt.Z)
                edge_vec = edge.node_b.pt - edge.node_a.pt
                edge_len = edge_vec.GetLength()
                
                if edge_len < 1e-4:
                    edge.edge_type = "RIDGE"
                    continue
                    
                edge_slope = z_diff / edge_len
                if edge_slope < 0.15 or z_diff < 0.25:
                    edge.edge_type = "RIDGE"
                else:
                    # Distinguish HIP vs VALLEY using half-space check
                    # Find a vertex on face A that is not on the edge
                    pt_in_fa = None
                    for loop in fa.loops_nodes:
                        for n in loop:
                            if n.key != edge.node_a.key and n.key != edge.node_b.key:
                                # Check if it's not collinear
                                vec = n.pt - edge.node_a.pt
                                cross = vec.CrossProduct(edge_vec)
                                if cross.GetLength() > 1e-4:
                                    pt_in_fa = n.pt
                                    break
                        if pt_in_fa:
                            break
                            
                    if pt_in_fa is None:
                        # Fallback if no valid vertex found
                        edge.edge_type = "HIP"
                    else:
                        # Since Revit roof normals point UP (out of the house volume),
                        # a Valley (concave) means Face A's point is in the positive half-space of Face B.
                        # A Hip (convex) means it is in the negative half-space.
                        dist = fb.normal.DotProduct(pt_in_fa) + fb.d
                        if dist > 0.001:
                            edge.edge_type = "VALLEY"
                        else:
                            edge.edge_type = "HIP"
            else:
                z_diff = abs(edge.node_a.pt.Z - edge.node_b.pt.Z)
                edge_len = (edge.node_b.pt - edge.node_a.pt).GetLength()
                edge_slope = (z_diff / edge_len) if edge_len > 1e-4 else 0.0
                if edge_slope < 0.15 or z_diff < 0.25:
                    edge.edge_type = "EAVE"
                else:
                    edge.edge_type = "RAKE"

    def print_diagnostics(self, total_unsnapped):
        """Print topological diagnostics to the pyRevit output window."""
        try:
            from pyrevit import script
            output = script.get_output()
            
            counts = {"RIDGE": 0, "HIP": 0, "VALLEY": 0, "EAVE": 0, "RAKE": 0, "UNKNOWN": 0}
            for edge in self.edges.values():
                etype = edge.edge_type or "UNKNOWN"
                counts[etype] = counts.get(etype, 0) + 1
                
            output.print_md("### RoofTopologyGraph Summary")
            output.print_md("- **Nodes Before Snap (un-merged loop vertices):** {}".format(total_unsnapped))
            output.print_md("- **Nodes After Snap (merged graph nodes):** {}".format(len(self.nodes)))
            for etype in sorted(counts.keys()):
                output.print_md("  - **{}:** {}".format(etype, counts[etype]))
                
            # Log warning/diagnostic message if interior edges have less than 2 faces
            for edge in self.edges.values():
                if len(edge.faces) >= 2 and edge.edge_type not in ("RIDGE", "HIP", "VALLEY"):
                    output.print_md("> **Warning:** Shared edge between faces {} classified as '{}' (expected RIDGE/HIP/VALLEY)".format(
                        [f.index for f in edge.faces], edge.edge_type
                    ))

            # 1. Expanded Topology Diagnostics for EVERY edge
            output.print_md("#### Detailed Edge Classification Details:")
            for idx, edge in enumerate(self.edges.values()):
                length = (edge.node_b.pt - edge.node_a.pt).GetLength()
                output.print_md("- **Edge #{}**:\n"
                                "  - **Type**: {}\n"
                                "  - **Faces**: {}\n"
                                "  - **Length**: {:.3f} ft\n"
                                "  - **Start**: ({:.3f}, {:.3f}, {:.3f})\n"
                                "  - **End**: ({:.3f}, {:.3f}, {:.3f})".format(
                                    idx + 1,
                                    edge.edge_type or "UNKNOWN",
                                    [f.index for f in edge.faces],
                                    length,
                                    edge.node_a.pt.X, edge.node_a.pt.Y, edge.node_a.pt.Z,
                                    edge.node_b.pt.X, edge.node_b.pt.Y, edge.node_b.pt.Z
                                ))

            # 8. Topology Consistency Checks
            ridge_count = counts.get("RIDGE", 0)
            hip_count = counts.get("HIP", 0)
            
            # Count convex sloped edges
            convex_sloped_edges = 0
            for edge in self.edges.values():
                if len(edge.faces) == 2:
                    edge_vec = edge.node_b.pt - edge.node_a.pt
                    edge_len = edge_vec.GetLength()
                    if edge_len > 1e-4:
                        z_diff = abs(edge.node_a.pt.Z - edge.node_b.pt.Z)
                        edge_slope = z_diff / edge_len
                        if edge_slope >= 0.15 and z_diff >= 0.25:
                            # Convex check using half-space test
                            face_list = list(edge.faces)
                            fa, fb = face_list[0], face_list[1]
                            pt_in_fa = None
                            for loop in fa.loops_nodes:
                                for n in loop:
                                    if n.key != edge.node_a.key and n.key != edge.node_b.key:
                                        vec = n.pt - edge.node_a.pt
                                        cross = vec.CrossProduct(edge_vec)
                                        if cross.GetLength() > 1e-4:
                                            pt_in_fa = n.pt
                                            break
                                if pt_in_fa:
                                    break
                            if pt_in_fa is not None:
                                dist = fb.normal.DotProduct(pt_in_fa) + fb.d
                                if dist <= 0.001:
                                    convex_sloped_edges += 1

            if len(self.faces) >= 2 and ridge_count == 0:
                output.print_md("\n> ### **ERROR**\n"
                                "> **Multi-plane roof detected but no ridge edges were generated.**\n")
                                
            if convex_sloped_edges > 0 and hip_count == 0:
                output.print_md("\n> ### **ERROR**\n"
                                "> **Convex sloped roof intersections detected but no hip edges generated.**\n")

        except Exception as e:
            try:
                from pyrevit import script
                script.get_output().print_md("> **Diagnostics Error**: {}".format(e))
            except Exception:
                pass


# ======================================================================
#  Engine
# ======================================================================

class RoofFramingEngine(BaseFramingEngine):
    """Calculates and places roof framing -- stick-frame or truss."""

    def _get_roof_base_elevation(self, roof_info):
        from Autodesk.Revit.DB import BuiltInParameter
        try:
            roof = roof_info.element
            p_offset = roof.get_Parameter(BuiltInParameter.ROOF_LEVEL_OFFSET_PARAM)
            offset = p_offset.AsDouble() if p_offset is not None else 0.0
            return roof_info.level_elevation + offset
        except Exception:
            return roof_info.level_elevation

    def _calc_ridges_hips_valleys(self, graph, roof_info):
        from Autodesk.Revit.DB import XYZ
        import math
        
        def is_finite_xyz(pt):
            return (not math.isnan(pt.X) and not math.isinf(pt.X) and
                    not math.isnan(pt.Y) and not math.isinf(pt.Y) and
                    not math.isnan(pt.Z) and not math.isinf(pt.Z))
                    
        # Ridge and Hip dimensions
        ridge_family = self.config.header_family_name or self.config.stud_family_name
        ridge_type = self.config.header_type_name or self.config.stud_type_name
        ridge_width, ridge_depth = self._resolve_roof_member_size(ridge_family, ridge_type)
        if ridge_width is None or ridge_width <= 0.0:
            ridge_width = 1.5 / 12.0
        if ridge_depth is None or ridge_depth <= 0.0:
            ridge_depth = 7.25 / 12.0

        members = []
        ridge_seen = set()
        ridge_edges_for_ties = []
        
        # Initialize lifecycle tracking if not already present
        if not hasattr(self, "_edge_lifecycle") or self._edge_lifecycle is None:
            self._edge_lifecycle = {}

        # 3. Place Ridge Boards
        for edge in graph.edges.values():
            if edge.edge_type == "RIDGE":
                edge_key = (min(edge.node_a.key, edge.node_b.key), max(edge.node_a.key, edge.node_b.key))
                
                self._edge_lifecycle[edge_key] = {
                    "type": "RIDGE",
                    "detected": "YES",
                    "created": "NO",
                    "appended": "NO",
                    "sent_to_placement": "NO",
                    "successfully_placed": "NO",
                    "start": edge.node_a.pt,
                    "end": edge.node_b.pt,
                    "length": (edge.node_b.pt - edge.node_a.pt).GetLength(),
                    "family_resolved": "None",
                    "type_resolved": "None",
                    "element_id": "None",
                    "offset_logged": "None",
                    "skip_reason": "None"
                }

                if edge_key in ridge_seen:
                    self._edge_lifecycle[edge_key]["skip_reason"] = "Duplicate ridge edge"
                    continue
                ridge_seen.add(edge_key)
                
                faces = list(edge.faces)
                if len(faces) >= 2:
                    ridge_edges_for_ties.append((edge.node_a.pt, edge.node_b.pt, faces[0].plane_info, faces[1].plane_info))
                
                # Geometry Validation
                length = (edge.node_b.pt - edge.node_a.pt).GetLength()
                skip_reason = None
                if length < MIN_MEMBER_LENGTH:
                    skip_reason = "Below MIN_MEMBER_LENGTH ({:.3f} ft)".format(length)
                elif edge.node_a.pt.DistanceTo(edge.node_b.pt) < 1e-4:
                    skip_reason = "Start and End points are identical"
                elif not is_finite_xyz(edge.node_a.pt) or not is_finite_xyz(edge.node_b.pt):
                    skip_reason = "Non-finite coordinates detected"
                    
                if skip_reason is not None:
                    self._edge_lifecycle[edge_key]["skip_reason"] = skip_reason
                    try:
                        from pyrevit import script
                        script.get_output().print_md("> **Skipped RIDGE edge**: length={:.3f} ft, reason: {}".format(length, skip_reason))
                    except Exception:
                        pass
                    continue

                m = FramingMember(FramingMember.HEADER, edge.node_a.pt, edge.node_b.pt)
                m.member_type = "RIDGE_BOARD"
                m.family_name = ridge_family
                m.type_name = ridge_type
                m.edge_key = edge_key
                self._edge_lifecycle[edge_key]["created"] = "YES"
                
                depth_sync = 0.0
                for face in edge.faces:
                    depth_sync = max(depth_sync, self._resolve_roof_layer_top_depth(face.plane_info))
                shift_z = depth_sync + ridge_depth / 2.0
                
                # Log applied offsets
                offset_msg = "shift_z={:.3f} ft (depth_sync={:.3f} ft, ridge_depth={:.3f} ft)".format(shift_z, depth_sync, ridge_depth)
                self._edge_lifecycle[edge_key]["offset_logged"] = offset_msg
                
                m.start_point = edge.node_a.pt - XYZ(0, 0, shift_z)
                m.end_point = edge.node_b.pt - XYZ(0, 0, shift_z)
                m.rotation = 0.0
                m.disallow_end_joins = True
                m.host_kind = roof_info.kind
                m.host_id = roof_info.element_id
                members.append(m)
                self._edge_lifecycle[edge_key]["appended"] = "YES"
                
        # 4. Place Hip and Valley Rafters
        for edge in graph.edges.values():
            if edge.edge_type in ("HIP", "VALLEY"):
                edge_key = (min(edge.node_a.key, edge.node_b.key), max(edge.node_a.key, edge.node_b.key))
                
                self._edge_lifecycle[edge_key] = {
                    "type": edge.edge_type,
                    "detected": "YES",
                    "created": "NO",
                    "appended": "NO",
                    "sent_to_placement": "NO",
                    "successfully_placed": "NO",
                    "start": edge.node_a.pt,
                    "end": edge.node_b.pt,
                    "length": (edge.node_b.pt - edge.node_a.pt).GetLength(),
                    "family_resolved": "None",
                    "type_resolved": "None",
                    "element_id": "None",
                    "offset_logged": "None",
                    "skip_reason": "None"
                }

                # Geometry Validation
                length = (edge.node_b.pt - edge.node_a.pt).GetLength()
                skip_reason = None
                if length < MIN_MEMBER_LENGTH:
                    skip_reason = "Below MIN_MEMBER_LENGTH ({:.3f} ft)".format(length)
                elif edge.node_a.pt.DistanceTo(edge.node_b.pt) < 1e-4:
                    skip_reason = "Start and End points are identical"
                elif not is_finite_xyz(edge.node_a.pt) or not is_finite_xyz(edge.node_b.pt):
                    skip_reason = "Non-finite coordinates detected"
                    
                if skip_reason is not None:
                    self._edge_lifecycle[edge_key]["skip_reason"] = skip_reason
                    try:
                        from pyrevit import script
                        script.get_output().print_md("> **Skipped {} edge**: length={:.3f} ft, reason: {}".format(edge.edge_type, length, skip_reason))
                    except Exception:
                        pass
                    continue

                m = FramingMember(FramingMember.HEADER, edge.node_a.pt, edge.node_b.pt)
                m.member_type = "HIP_RAFTER" if edge.edge_type == "HIP" else "VALLEY_RAFTER"
                m.family_name = ridge_family
                m.type_name = ridge_type
                m.edge_key = edge_key
                self._edge_lifecycle[edge_key]["created"] = "YES"
                
                avg_normal = XYZ(0, 0, 0)
                depth_sync = 0.0
                for face in edge.faces:
                    avg_normal = avg_normal + face.normal
                    depth_sync = max(depth_sync, self._resolve_roof_layer_top_depth(face.plane_info))
                if len(edge.faces) > 0:
                    avg_normal = avg_normal.Multiply(1.0 / len(edge.faces)).Normalize()
                else:
                    avg_normal = XYZ(0, 0, 1)
                    
                shift = avg_normal.Multiply(depth_sync + ridge_depth / 2.0)
                
                # Log applied offsets
                offset_msg = "shift vector=({:.3f}, {:.3f}, {:.3f}), avg_normal=({:.3f}, {:.3f}, {:.3f}), depth_sync={:.3f} ft".format(
                    shift.X, shift.Y, shift.Z, avg_normal.X, avg_normal.Y, avg_normal.Z, depth_sync
                )
                self._edge_lifecycle[edge_key]["offset_logged"] = offset_msg
                
                m.start_point = edge.node_a.pt - shift
                m.end_point = edge.node_b.pt - shift
                
                m.rotation = _rotation_from_up(edge.node_b.pt - edge.node_a.pt, avg_normal)
                m.disallow_end_joins = True
                m.host_kind = roof_info.kind
                m.host_id = roof_info.element_id
                members.append(m)
                self._edge_lifecycle[edge_key]["appended"] = "YES"

        return members, ridge_edges_for_ties

    def place_members(self, members, host_info):
        """Place roof members and apply roof-specific post-processing."""
        # 1. Trace lifecycle: sent_to_placement
        lifecycle = getattr(self, "_edge_lifecycle", {})
        for m in members:
            edge_key = getattr(m, "edge_key", None)
            if edge_key and edge_key in lifecycle:
                lifecycle[edge_key]["sent_to_placement"] = "YES"

        try:
            from pyrevit import script
            output = script.get_output()
            output.print_md("### Debug place_members in wf_roof:")
            output.print_md("- **Total members to place:** {}".format(len(members)))
            
            mtype_counts = {}
            for m in members:
                mtype = getattr(m, "member_type", "UNKNOWN")
                mtype_counts[mtype] = mtype_counts.get(mtype, 0) + 1
            output.print_md("- **Member type counts to place:** {}".format(mtype_counts))

            for m in members:
                if m.member_type in ("RIDGE_BOARD", "HIP_RAFTER", "VALLEY_RAFTER"):
                    output.print_md("  - **To Place {}**: Start=({:.3f}, {:.3f}, {:.3f}), End=({:.3f}, {:.3f}, {:.3f}), Family={}, Type={}".format(
                        m.member_type, m.start_point.X, m.start_point.Y, m.start_point.Z,
                        m.end_point.X, m.end_point.Y, m.end_point.Z, m.family_name, m.type_name
                    ))
        except Exception as debug_err:
            try:
                from pyrevit import script
                script.get_output().print_md("> **Debug Error before placement:** {}".format(debug_err))
            except Exception:
                pass

        placed = BaseFramingEngine.place_members(self, members, host_info)

        # 2. Trace lifecycle: successfully_placed & resolve details
        try:
            member_pairs = getattr(self, "_last_placed_pairs", None) or []
            for member, instance in member_pairs:
                edge_key = getattr(member, "edge_key", None)
                if edge_key and edge_key in lifecycle:
                    if instance is not None:
                        lifecycle[edge_key]["successfully_placed"] = "YES"
                        lifecycle[edge_key]["element_id"] = str(instance.Id.Value)
                        lifecycle[edge_key]["family_resolved"] = member.family_name
                        lifecycle[edge_key]["type_resolved"] = member.type_name
        except Exception as trace_err:
            try:
                from pyrevit import script
                script.get_output().print_md("> **Error tracing placed members:** {}".format(trace_err))
            except Exception:
                pass

        # 3. Print summaries
        try:
            from pyrevit import script
            output = script.get_output()
            
            # Placement Summary
            ridge_calc = sum(1 for k, v in lifecycle.items() if v["type"] == "RIDGE" and v["created"] == "YES")
            ridge_placed = sum(1 for k, v in lifecycle.items() if v["type"] == "RIDGE" and v["successfully_placed"] == "YES")
            hip_calc = sum(1 for k, v in lifecycle.items() if v["type"] == "HIP" and v["created"] == "YES")
            hip_placed = sum(1 for k, v in lifecycle.items() if v["type"] == "HIP" and v["successfully_placed"] == "YES")
            valley_calc = sum(1 for k, v in lifecycle.items() if v["type"] == "VALLEY" and v["created"] == "YES")
            valley_placed = sum(1 for k, v in lifecycle.items() if v["type"] == "VALLEY" and v["successfully_placed"] == "YES")
            
            output.print_md("### RIDGE & HIP PLACEMENT SUMMARY")
            output.print_md("- **Ridge Boards**: calculated={}, placed={}".format(ridge_calc, ridge_placed))
            output.print_md("- **Hip Rafters**: calculated={}, placed={}".format(hip_calc, hip_placed))
            output.print_md("- **Valley Rafters**: calculated={}, placed={}".format(valley_calc, valley_placed))
            
            # Lifecycle Summary
            output.print_md("### RIDGE & HIP LIFECYCLE SUMMARY")
            for edge_key, info in sorted(lifecycle.items(), key=lambda x: x[1]["type"]):
                output.print_md(
                    "#### **{} #{}**\n"
                    "- **Length**: {:.3f} ft\n"
                    "- **Start**: ({:.3f}, {:.3f}, {:.3f})\n"
                    "- **End**: ({:.3f}, {:.3f}, {:.3f})\n"
                    "- **Classified/Detected**: {}\n"
                    "- **Created**: {}\n"
                    "- **Appended**: {}\n"
                    "- **Sent To Placement**: {}\n"
                    "- **Placement Success**: {}\n"
                    "- **Revit Element ID**: {}\n"
                    "- **Family/Type**: {} / {}\n"
                    "- **Offsets Logged**: {}\n"
                    "- **Skip Reason**: {}".format(
                        info["type"], edge_key[0], info["length"],
                        info["start"].X, info["start"].Y, info["start"].Z,
                        info["end"].X, info["end"].Y, info["end"].Z,
                        info["detected"], info["created"], info["appended"],
                        info["sent_to_placement"], info["successfully_placed"],
                        info["element_id"], info["family_resolved"], info["type_resolved"],
                        info["offset_logged"], info["skip_reason"]
                    )
                )
        except Exception as summary_err:
            try:
                from pyrevit import script
                script.get_output().print_md("> **Error printing summaries:** {}".format(summary_err))
            except Exception:
                pass

        # 4. Optional Topology Debug Geometry (ModelLines)
        if getattr(self.config, "debug_roof_topology", True):
            try:
                self._create_debug_model_lines(members)
            except Exception as debug_geom_err:
                try:
                    from pyrevit import script
                    script.get_output().print_md("> **Failed to draw debug lines**: {}".format(debug_geom_err))
                except Exception:
                    pass

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

    def _create_debug_model_lines(self, members):
        """Create temporary Revit ModelLines for RIDGE, HIP, and VALLEY edges."""
        from Autodesk.Revit.DB import Plane, SketchPlane, XYZ, Line
        
        # Keep track of what we draw so we don't draw duplicates
        drawn_keys = set()
        
        for m in members:
            mtype = getattr(m, "member_type", "UNKNOWN")
            if mtype in ("RIDGE_BOARD", "HIP_RAFTER", "VALLEY_RAFTER"):
                edge_key = getattr(m, "edge_key", None)
                if edge_key:
                    if edge_key in drawn_keys:
                        continue
                    drawn_keys.add(edge_key)
                    
                start = m.start_point
                end = m.end_point
                length = (end - start).GetLength()
                if length < 0.1:
                    continue
                    
                try:
                    line = Line.CreateBound(start, end)
                    
                    # Determine normal of plane to create SketchPlane
                    direction = (end - start).Normalize()
                    ref_up = XYZ.BasisZ
                    if abs(direction.DotProduct(ref_up)) > 0.99:
                        ref_up = XYZ.BasisX
                        
                    plane_normal = direction.CrossProduct(ref_up).Normalize()
                    plane = Plane.CreateByNormalAndOrigin(plane_normal, start)
                    sketch_plane = SketchPlane.Create(self.doc, plane)
                    
                    # Create the ModelLine (NewModelCurve)
                    self.doc.Create.NewModelCurve(line, sketch_plane)
                    
                    from pyrevit import script
                    script.get_output().print_md("> **Created Debug ModelLine** for `{}`: length={:.3f} ft, start=({:.3f}, {:.3f}, {:.3f}), end=({:.3f}, {:.3f}, {:.3f})".format(
                        mtype, length, start.X, start.Y, start.Z, end.X, end.Y, end.Z
                    ))
                except Exception as err:
                    try:
                        from pyrevit import script
                        script.get_output().print_md("> **Failed to create debug line** for `{}`: {}".format(mtype, err))
                    except Exception:
                        pass

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
        """Calculate framing members for a roof."""
        roof_info = analyze_roof_host(self.doc, roof, self.config)
        if roof_info is None:
            return [], None
            
        if mode == "truss":
            members = self._calc_truss_positions(roof_info)
        else:
            members = self._calc_stick_frame(roof_info)
        return members, roof_info

    def _apply_automatic_coping(self, placed_instances):
        """Best-effort coping between newly placed rafters and perimeter boards."""
        if not placed_instances:
            return

        try:
            from Autodesk.Revit.DB.Structure import StructuralFramingUtils
        except ImportError:
            StructuralFramingUtils = None

        if StructuralFramingUtils is None:
            return

        rafters = []
        boards = []
        member_pairs = getattr(self, "_last_placed_pairs", None) or []

        if member_pairs:
            for member, instance in member_pairs:
                member_type = getattr(member, "member_type", None)
                if member_type in ("RAFTER", "TOP_CHORD", "BOTTOM_CHORD"):
                    rafters.append(instance)
                elif member_type in ("FASCIA", "LEDGER", "RIDGE_BOARD", "HIP_RAFTER", "VALLEY_RAFTER", "PURLIN"):
                    boards.append(instance)
        else:
            for instance in placed_instances:
                tracking = get_tracking_data(instance)
                if tracking is None:
                    continue
                member_type = tracking.get("member")
                if member_type in ("RAFTER", "TOP_CHORD", "BOTTOM_CHORD"):
                    rafters.append(instance)
                elif member_type in ("FASCIA", "LEDGER", "RIDGE_BOARD", "HIP_RAFTER", "VALLEY_RAFTER", "PURLIN"):
                    boards.append(instance)

        if not rafters or not boards:
            return

        for rafter in rafters:
            for board in boards:
                if not self._elements_are_near(rafter, board):
                    continue
                try:
                    StructuralFramingUtils.AddCoping(self.doc, rafter, board)
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
        from Autodesk.Revit.DB import XYZ
        import math
        members = []
        planes = roof_info.planes
        spacing = self.config.stud_spacing_ft
        if spacing <= 0:
            return members
            
        # Initialize edge lifecycle tracking
        self._edge_lifecycle = {}
        
        def is_finite_xyz(pt):
            return (not math.isnan(pt.X) and not math.isinf(pt.X) and
                    not math.isnan(pt.Y) and not math.isinf(pt.Y) and
                    not math.isnan(pt.Z) and not math.isinf(pt.Z))
            
        # 1. Build and solve topology graph
        graph = RoofTopologyGraph(planes)
        graph.solve_geometry()
        graph.classify_edges()
        
        # Diagnostics
        try:
            total_unsnapped = sum(len(loop) for face in graph.faces for loop in face.plane_info.boundary_loops_local)
            graph.print_diagnostics(total_unsnapped)
        except Exception:
            pass
        
        # 2. Rafter Dimensions
        rafter_family = self.config.stud_family_name
        rafter_type = self.config.stud_type_name
        rafter_width, rafter_depth = self._resolve_roof_member_size(rafter_family, rafter_type)
        if rafter_width is None or rafter_width <= 0.0:
            rafter_width = 1.5 / 12.0
        if rafter_depth is None or rafter_depth <= 0.0:
            rafter_depth = 5.5 / 12.0
            
        # 3 & 4. Place Ridge Boards, Hip & Valley Rafters
        ridge_hip_members, ridge_edges_for_ties = self._calc_ridges_hips_valleys(graph, roof_info)
        members.extend(ridge_hip_members)

        # Resolve ridge width for rafter cutbacks
        ridge_family = self.config.header_family_name or self.config.stud_family_name
        ridge_type = self.config.header_type_name or self.config.stud_type_name
        ridge_width, _ = self._resolve_roof_member_size(ridge_family, ridge_type)
        if ridge_width is None or ridge_width <= 0.0:
            ridge_width = 1.5 / 12.0

        # 5. Place Common and Jack Rafters
        for face in graph.faces:
            local_loops = []
            for loop in face.loops_nodes:
                local_loop = []
                for node in loop:
                    pt_loc = _to_local(node.pt, face.plane_info.origin, face.plane_info.x_axis, face.plane_info.y_axis)
                    local_loop.append(pt_loc)
                if len(local_loop) >= 3:
                    local_loops.append(local_loop)
                    
            if not local_loops:
                continue
                
            all_pts = [pt for loop in local_loops for pt in loop]
            min_x = min(pt[0] for pt in all_pts)
            max_x = max(pt[0] for pt in all_pts)
            
            ref_local_x = (XYZ(0,0,0) - face.plane_info.origin).DotProduct(face.plane_info.x_axis)
            k_start = int(math.ceil((min_x - ref_local_x - 1e-4) / spacing))
            k_end = int(math.floor((max_x - ref_local_x + 1e-4) / spacing))
            
            control_depth = self._resolve_roof_member_center_depth(face.plane_info, rafter_family, rafter_type)
            
            for k in range(k_start, k_end + 1):
                x = ref_local_x + k * spacing
                intervals = _scanline_intervals(local_loops, "x", x)
                for start_y, end_y in intervals:
                    if end_y - start_y < MIN_MEMBER_LENGTH:
                        continue
                        
                    pt_upper_loc = (x, start_y)
                    pt_lower_loc = (x, end_y)
                    
                    pt_upper_world = _surface_point(face.plane_info, x, start_y)
                    pt_lower_world = _surface_point(face.plane_info, x, end_y)
                    
                    pt_upper_shifted = pt_upper_world - face.normal.Multiply(control_depth)
                    pt_lower_shifted = pt_lower_world - face.normal.Multiply(control_depth)
                    
                    rafter_dir = _normalize(pt_lower_shifted - pt_upper_shifted)
                    if rafter_dir is None:
                        continue
                        
                    upper_edge = find_nearest_edge_in_face(pt_upper_loc, local_loops, face.edges, face.plane_info)
                    lower_edge = find_nearest_edge_in_face(pt_lower_loc, local_loops, face.edges, face.plane_info)
                    
                    # Apply cutbacks
                    if upper_edge is not None:
                        if upper_edge.edge_type == "RIDGE":
                            pt_upper_shifted = pt_upper_shifted + rafter_dir.Multiply(ridge_width / 2.0)
                        elif upper_edge.edge_type in ("HIP", "VALLEY"):
                            edge_dir = _normalize(upper_edge.node_b.pt - upper_edge.node_a.pt)
                            if edge_dir is not None:
                                cos_phi = abs(rafter_dir.DotProduct(edge_dir))
                                sin_phi = math.sqrt(max(0.1, 1.0 - cos_phi * cos_phi))
                                d_cutback = (ridge_width / 2.0) / sin_phi
                                pt_upper_shifted = pt_upper_shifted + rafter_dir.Multiply(d_cutback)
                            else:
                                pt_upper_shifted = pt_upper_shifted + rafter_dir.Multiply(ridge_width / 2.0)
                                
                    if lower_edge is not None:
                        if lower_edge.edge_type == "VALLEY":
                            edge_dir = _normalize(lower_edge.node_b.pt - lower_edge.node_a.pt)
                            if edge_dir is not None:
                                cos_phi = abs(rafter_dir.DotProduct(edge_dir))
                                sin_phi = math.sqrt(max(0.1, 1.0 - cos_phi * cos_phi))
                                d_cutback = (ridge_width / 2.0) / sin_phi
                                pt_lower_shifted = pt_lower_shifted - rafter_dir.Multiply(d_cutback)
                            else:
                                pt_lower_shifted = pt_lower_shifted - rafter_dir.Multiply(ridge_width / 2.0)
                        elif lower_edge.edge_type in ("HIP", "RIDGE"):
                            edge_dir = _normalize(lower_edge.node_b.pt - lower_edge.node_a.pt)
                            if edge_dir is not None:
                                cos_phi = abs(rafter_dir.DotProduct(edge_dir))
                                sin_phi = math.sqrt(max(0.1, 1.0 - cos_phi * cos_phi))
                                d_cutback = (ridge_width / 2.0) / sin_phi
                                pt_lower_shifted = pt_lower_shifted - rafter_dir.Multiply(d_cutback)
                            else:
                                pt_lower_shifted = pt_lower_shifted - rafter_dir.Multiply(ridge_width / 2.0)
                                
                    if _dist(pt_upper_shifted, pt_lower_shifted) < MIN_MEMBER_LENGTH:
                        continue
                        
                    m = FramingMember(FramingMember.STUD, pt_lower_shifted, pt_upper_shifted)
                    m.member_type = "RAFTER"
                    m.family_name = rafter_family
                    m.type_name = rafter_type
                    m.rotation = _rotation_from_up(pt_upper_shifted - pt_lower_shifted, face.normal)
                    m.disallow_end_joins = True
                    m.host_kind = face.plane_info.kind
                    m.host_id = face.plane_info.element_id
                    members.append(m)

        # 6. Collar ties & Ceiling joists
        if ridge_edges_for_ties and bool(getattr(self.config, "include_collar_ties", True)):
            try:
                members.extend(self._make_collar_ties(planes, ridge_edges_for_ties, roof_info))
            except Exception:
                pass
                
        if ridge_edges_for_ties and (bool(getattr(self.config, "include_ceiling_joists", True)) or bool(getattr(self.config, "include_king_posts", False))):
            try:
                # `_make_ceiling_joists` expects: self, planes, ridge_edges, roof_info, spacing, include_kickers=True
                members.extend(self._make_ceiling_joists(
                    planes,
                    ridge_edges_for_ties,
                    roof_info,
                    spacing,
                    getattr(self.config, "include_kickers", True)
                ))
            except Exception:
                pass

        # 7. Continuous Purlins
        try:
            members.extend(self._make_purlins(graph, roof_info, mode="stick"))
        except Exception:
            pass

        # 8. Border members (Fascia & Ledger)
        all_eave_edges = []
        all_rake_edges = []
        all_ledger_edges = []
        
        for face in graph.faces:
            for edge in face.edges:
                if len(edge.faces) == 1:
                    edge_depth = self._resolve_roof_layer_top_depth(face.plane_info)
                    role = "eave" if edge.edge_type == "EAVE" else ("ledger" if edge.edge_type == "RIDGE" else "rake")
                    triple = (edge.node_a.pt, edge.node_b.pt, face.plane_info, role, edge_depth)
                    if edge.edge_type == "EAVE":
                        all_eave_edges.append(triple)
                    elif edge.edge_type == "RAKE":
                        all_rake_edges.append(triple)
                    else:
                        all_ledger_edges.append(triple)

        fascia_edges = list(all_eave_edges)
        if len(graph.faces) == 1:
            fascia_edges.extend(all_rake_edges)
            
        try:
            members.extend(self._make_fascia(fascia_edges, roof_info))
        except Exception:
            pass
            
        if len(graph.faces) == 1 and all_ledger_edges:
            try:
                members.extend(self._make_ledger(all_ledger_edges, roof_info))
            except Exception:
                pass

        return members

    def _resolve_roof_member_center_depth(self, plane, family_name, type_name):
        """Return the control-plane depth for a roof framing member centerline."""
        layer_top_depth = self._resolve_roof_layer_top_depth(plane)
        _, member_depth = self._resolve_roof_member_size(family_name, type_name)
        if member_depth is not None and member_depth > 0.0:
            return layer_top_depth + (member_depth / 2.0)
        return layer_top_depth

    def _make_purlins(self, graph, roof_info, mode="stick"):
        from Autodesk.Revit.DB import XYZ
        members = []
        
        boundary_zs = []
        for node in graph.nodes.values():
            is_boundary = any(len(edge.faces) == 1 for edge in node.edges)
            if is_boundary:
                boundary_zs.append(node.pt.Z)
        
        Z_eave = min(boundary_zs) if boundary_zs else 0.0
        
        ridge_zs = []
        for edge in graph.edges.values():
            if edge.edge_type == "RIDGE":
                ridge_zs.append(edge.node_a.pt.Z)
                ridge_zs.append(edge.node_b.pt.Z)
                
        Z_ridge = max(ridge_zs) if ridge_zs else Z_eave + 8.0
        
        if graph.faces:
            theta = math.acos(max(0.1, min(1.0, graph.faces[0].normal.Z)))
        else:
            theta = math.pi / 6.0
            
        slope_spacing = 4.0
        vert_spacing = slope_spacing * math.sin(theta)
        if vert_spacing < 0.5:
            vert_spacing = 2.0
            
        purlin_family = self.config.stud_family_name
        purlin_type = self.config.stud_type_name
        purlin_width, purlin_depth = self._resolve_roof_member_size(purlin_family, purlin_type)
        if purlin_width is None or purlin_width <= 0.0:
            purlin_width = 1.5 / 12.0
        if purlin_depth is None or purlin_depth <= 0.0:
            purlin_depth = 3.5 / 12.0
            
        rafter_family = self.config.stud_family_name
        rafter_type = self.config.stud_type_name
        _, rafter_depth = self._resolve_roof_member_size(rafter_family, rafter_type)
        if rafter_depth is None or rafter_depth <= 0.0:
            rafter_depth = 5.5 / 12.0
            
        Z_curr = Z_eave + vert_spacing
        while Z_curr < Z_ridge - 0.5:
            for face in graph.faces:
                intersections = []
                for edge in face.edges:
                    z_min = min(edge.node_a.pt.Z, edge.node_b.pt.Z)
                    z_max = max(edge.node_a.pt.Z, edge.node_b.pt.Z)
                    if z_min <= Z_curr <= z_max:
                        dz = edge.node_b.pt.Z - edge.node_a.pt.Z
                        if abs(dz) > 1e-4:
                            t = (Z_curr - edge.node_a.pt.Z) / dz
                            pt = edge.node_a.pt + (edge.node_b.pt - edge.node_a.pt).Multiply(t)
                        else:
                            pt = _midpoint(edge.node_a.pt, edge.node_b.pt)
                        intersections.append(pt)
                
                unique_ints = []
                for pt in intersections:
                    if not any(_dist(pt, x) < 0.1 for x in unique_ints):
                        unique_ints.append(pt)
                        
                if len(unique_ints) >= 2:
                    sorted_ints = sorted(unique_ints, key=lambda p: (p - face.plane_info.origin).DotProduct(face.plane_info.x_axis))
                    for idx in range(0, len(sorted_ints) - 1, 2):
                        pt_a = sorted_ints[idx]
                        pt_b = sorted_ints[idx+1]
                        
                        if _dist(pt_a, pt_b) < MIN_MEMBER_LENGTH:
                            continue
                            
                        layer_depth = self._resolve_roof_layer_top_depth(face.plane_info)
                        if mode == "stick":
                            shift_dist = layer_depth + rafter_depth + purlin_depth / 2.0
                        else:
                            shift_dist = layer_depth + purlin_depth / 2.0
                            
                        shift = face.normal.Multiply(shift_dist)
                        
                        m = FramingMember(FramingMember.HEADER, pt_a - shift, pt_b - shift)
                        m.member_type = "PURLIN"
                        m.family_name = purlin_family
                        m.type_name = purlin_type
                        
                        m.rotation = _rotation_from_up(pt_b - pt_a, face.normal)
                        m.disallow_end_joins = True
                        m.host_kind = face.plane_info.kind
                        m.host_id = face.plane_info.element_id
                        members.append(m)
                        
            Z_curr += vert_spacing
            
        return members

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

    def _make_ceiling_joists(self, planes, ridge_edges, roof_info, spacing, include_kickers=True):
        """Ceiling joists spanning eave-to-eave at plate line elevation."""
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

                # Push ceiling joist up by half its depth so its bottom rests exactly at the top plate elevation
                joist_width, joist_depth = self._resolve_roof_member_size(
                    self.config.stud_family_name,
                    self.config.stud_type_name
                )
                if joist_depth is None or joist_depth <= 0.0:
                    joist_depth = 5.5 / 12.0
                roof_base_z = self._get_roof_base_elevation(roof_info)
                joist_z = roof_base_z + (joist_depth / 2.0)
                joist_a = XYZ(foot_a.X, foot_a.Y, joist_z)
                joist_b = XYZ(foot_b.X, foot_b.Y, joist_z)

                if bool(getattr(self.config, "include_ceiling_joists", True)):
                    if _dist(joist_a, joist_b) >= MIN_MEMBER_LENGTH:
                        jkey = (_pt_key(joist_a), _pt_key(joist_b))
                        jrkey = (_pt_key(joist_b), _pt_key(joist_a))
                        if jkey not in joist_seen and jrkey not in joist_seen:
                            joist_seen.add(jkey)
                            m = FramingMember(FramingMember.HEADER, joist_a, joist_b)
                            m.member_type = "CEILING_JOIST"
                            m.family_name = self.config.stud_family_name
                            m.type_name = self.config.stud_type_name
                            m.rotation = 0.0  # on edge
                            m.host_kind = roof_info.kind
                            m.host_id = roof_info.element_id
                            members.append(m)

                    if include_kickers:
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

                if bool(getattr(self.config, "include_king_posts", False)):
                    # Resolve depths
                    ridge_family = self.config.header_family_name or self.config.stud_family_name
                    ridge_type = self.config.header_type_name or self.config.stud_type_name
                    _, ridge_depth = self._resolve_roof_member_size(ridge_family, ridge_type)
                    if ridge_depth is None or ridge_depth <= 0.0:
                        ridge_depth = 7.25 / 12.0
                        
                    depth_sync = max(self._resolve_roof_layer_top_depth(pa), self._resolve_roof_layer_top_depth(pb))
                    
                    post_start_z = joist_z + joist_depth / 2.0
                    post_end_z = ridge_pt.Z - depth_sync - ridge_depth
                    
                    if post_end_z - post_start_z >= MIN_MEMBER_LENGTH:
                        pt_bot = XYZ(ridge_pt.X, ridge_pt.Y, post_start_z)
                        pt_top = XYZ(ridge_pt.X, ridge_pt.Y, post_end_z)
                        
                        m_post = FramingMember(FramingMember.STUD, pt_bot, pt_top)
                        m_post.member_type = "KING_POST"
                        m_post.family_name = self.config.stud_family_name
                        m_post.type_name = self.config.stud_type_name
                        m_post.is_column = True
                        m_post.rotation = 0.0
                        m_post.host_kind = roof_info.kind
                        m_post.host_id = roof_info.element_id
                        m_post.disallow_end_joins = True
                        members.append(m_post)

                d += spacing

        return members

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

    def _choose_best_side_shift(self, roof, start_point, end_point, side_axis, shift_distance):
        """Choose side shift direction that keeps the member inside the roof envelope."""
        shift_a = side_axis.Multiply(shift_distance)
        shift_b = side_axis.Multiply(-shift_distance)

        c_a = self._clip_member_axis_to_roof(roof, start_point + shift_a, end_point + shift_a)
        c_b = self._clip_member_axis_to_roof(roof, start_point + shift_b, end_point + shift_b)

        len_a = _dist(c_a[0], c_a[1]) if c_a else 0.0
        len_b = _dist(c_b[0], c_b[1]) if c_b else 0.0

        if len_a >= len_b and len_a >= MIN_MEMBER_LENGTH:
            return start_point + shift_a, end_point + shift_a
        elif len_b >= MIN_MEMBER_LENGTH:
            return start_point + shift_b, end_point + shift_b
        return None

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

    def _get_support_elements(self, roof_info):
        from Autodesk.Revit.DB import FilteredElementCollector, Wall, BuiltInCategory, Outline, BoundingBoxIntersectsFilter, XYZ
        doc = self.doc
        roof = roof_info.element
        bbox = roof.get_BoundingBox(None)
        if bbox is None:
            return [], []
        
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
                member_len = (p_end - p_start).GetLength()
                if shorten_len < member_len - 0.1:
                    return p_int - D.Multiply(shorten_len)
        return p_end

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

                t0 = (seg_start - probe_start).DotProduct(axis_dir)
                t1 = (seg_end - probe_start).DotProduct(axis_dir)
                t_min = min(t0, t1)
                t_max = max(t0, t1)
                contains_target = (t_min - 1e-3) <= target_t <= (t_max + 1e-3)
                seg_length = abs(t_max - t_min)

                if contains_target:
                    clipped_start = probe_start + axis_dir.Multiply(max(target_t - axis_len / 2.0, t_min))
                    clipped_end = probe_start + axis_dir.Multiply(min(target_t + axis_len / 2.0, t_max))
                    clipped = (clipped_start, clipped_end)
                    dist = abs(t_min - (target_t - axis_len / 2.0)) + abs((target_t + axis_len / 2.0) - t_max)
                    if not best_contains_target or dist < best_distance:
                        best_segment = clipped
                        best_contains_target = True
                        best_distance = dist
                elif not best_contains_target:
                    clipped_start = probe_start + axis_dir.Multiply(max(target_t - axis_len / 2.0, t_min))
                    clipped_end = probe_start + axis_dir.Multiply(min(target_t + axis_len / 2.0, t_max))
                    clipped = (clipped_start, clipped_end)
                    if seg_length > best_length:
                        best_segment = clipped
                        best_length = seg_length

        return best_segment

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

    def _calc_truss_positions(self, roof_info):
        from Autodesk.Revit.DB import XYZ
        import math
        members = []
        planes = roof_info.planes
        
        truss_spacing_in = getattr(self.config, 'truss_spacing', 24.0)
        truss_spacing = truss_spacing_in / 12.0
        
        family_top = getattr(self.config, 'family_top_chords', (self.config.stud_family_name, self.config.stud_type_name))
        family_bottom = getattr(self.config, 'family_bottom_chords', (self.config.stud_family_name, self.config.stud_type_name))
        family_web = getattr(self.config, 'family_web_bracing', (self.config.stud_family_name, self.config.stud_type_name))
        
        _, tc_depth = self._resolve_roof_member_size(family_top[0], family_top[1])
        _, bc_depth = self._resolve_roof_member_size(family_bottom[0], family_bottom[1])
        _, web_depth = self._resolve_roof_member_size(family_web[0], family_web[1])
        if tc_depth is None or tc_depth <= 0.0:
            tc_depth = 5.5 / 12.0
        if bc_depth is None or bc_depth <= 0.0:
            bc_depth = 5.5 / 12.0
        if web_depth is None or web_depth <= 0.0:
            web_depth = 3.5 / 12.0
            
        walls, beams = self._get_support_elements(roof_info)
        
        # 1. Build and solve topology graph
        graph = RoofTopologyGraph(planes)
        graph.solve_geometry()
        graph.classify_edges()
        
        # Diagnostics
        try:
            total_unsnapped = sum(len(loop) for face in graph.faces for loop in face.plane_info.boundary_loops_local)
            graph.print_diagnostics(total_unsnapped)
        except Exception:
            pass
            
        # Calculate and place Ridge Boards, Hip & Valley Rafters
        self._edge_lifecycle = {}
        ridge_hip_members, _ = self._calc_ridges_hips_valleys(graph, roof_info)
        members.extend(ridge_hip_members)
        
        for face in graph.faces:
            layer_depth = self._resolve_roof_layer_top_depth(face.plane_info)
            face.d_shifted = face.d + layer_depth + tc_depth / 2.0
            
        # 2. Establish global truss direction
        eave_edges = [edge for edge in graph.edges.values() if edge.edge_type == "EAVE"]
        if not eave_edges:
            eave_edges = [edge for edge in graph.edges.values() if len(edge.faces) == 1]
            
        if eave_edges:
            longest_eave = max(eave_edges, key=lambda e: _dist(e.node_a.pt, e.node_b.pt))
            eave_dir = _normalize(longest_eave.node_b.pt - longest_eave.node_a.pt)
        else:
            eave_dir = XYZ(1, 0, 0)
            
        truss_dir = XYZ(-eave_dir.Y, eave_dir.X, 0.0).Normalize()
        slice_normal = eave_dir
        
        proj_vals = [node.pt.DotProduct(slice_normal) for node in graph.nodes.values()]
        if not proj_vals:
            return members
        roof_min = min(proj_vals)
        roof_max = max(proj_vals)
        
        support_ts = []
        for wall in walls:
            try:
                loc = wall.Location
                if loc is not None and getattr(loc, "Curve", None) is not None:
                    curve = loc.Curve
                    support_ts.append(curve.GetEndPoint(0).DotProduct(slice_normal))
                    support_ts.append(curve.GetEndPoint(1).DotProduct(slice_normal))
            except Exception:
                pass
        for beam in beams:
            try:
                loc = beam.Location
                if loc is not None and getattr(loc, "Curve", None) is not None:
                    curve = loc.Curve
                    support_ts.append(curve.GetEndPoint(0).DotProduct(slice_normal))
                    support_ts.append(curve.GetEndPoint(1).DotProduct(slice_normal))
            except Exception:
                pass
                
        if support_ts:
            t_min = max(min(support_ts), roof_min)
            t_max = min(max(support_ts), roof_max)
            # If support range is too small/degenerate, fallback
            if t_max - t_min < 2.0:
                t_min = roof_min
                t_max = roof_max
                try:
                    from pyrevit import script
                    script.get_output().print_md("> **Warning:** Support range was too narrow. Falling back to full roof bounds.")
                except Exception:
                    pass
        else:
            t_min = roof_min
            t_max = roof_max
            try:
                from pyrevit import script
                script.get_output().print_md("> **Warning:** No support walls/beams found along slice normal. Falling back to full roof bounds.")
            except Exception:
                pass
                
        span_len = t_max - t_min
        num_trusses = int(math.ceil(span_len / truss_spacing)) if truss_spacing > 0 else 1
        num_trusses = max(1, num_trusses)
        actual_truss_spacing = span_len / num_trusses
        
        # 3. Step through stations
        for i in range(num_trusses + 1):
            t_station = t_min + i * actual_truss_spacing
            slice_origin = slice_normal.Multiply(t_station)
            
            intersections = []
            for edge in graph.edges.values():
                d_a = (edge.node_a.pt - slice_origin).DotProduct(slice_normal)
                d_b = (edge.node_b.pt - slice_origin).DotProduct(slice_normal)
                if d_a * d_b < 0:
                    t_int = d_a / (d_a - d_b)
                    pt = edge.node_a.pt + (edge.node_b.pt - edge.node_a.pt).Multiply(t_int)
                    intersections.append((pt, edge))
                elif abs(d_a) < 1e-4:
                    intersections.append((edge.node_a.pt, edge))
                elif abs(d_b) < 1e-4:
                    intersections.append((edge.node_b.pt, edge))
                    
            unique_ints = []
            seen_u = set()
            for pt, edge in intersections:
                u_val = (pt - slice_origin).DotProduct(truss_dir)
                rounded_u = round(u_val, 3)
                if rounded_u not in seen_u:
                    seen_u.add(rounded_u)
                    unique_ints.append((pt, edge, u_val))
                    
            if len(unique_ints) < 2:
                continue
                
            unique_ints.sort(key=lambda item: item[2])
            
            segments = []
            for idx in range(len(unique_ints) - 1):
                pt_a, edge_a, u_a = unique_ints[idx]
                pt_b, edge_b, u_b = unique_ints[idx+1]
                if u_b - u_a < MIN_MEMBER_LENGTH:
                    continue
                    
                face = None
                face_intersection = edge_a.faces.intersection(edge_b.faces)
                if face_intersection:
                    face = list(face_intersection)[0]
                else:
                    combined = list(edge_a.faces) + list(edge_b.faces)
                    if combined:
                        face = combined[0]
                        
                if face is not None:
                    denom = face.normal.Z
                    if abs(denom) > 1e-5:
                        A = - truss_dir.DotProduct(face.normal) / denom
                        B = - (slice_origin).DotProduct(face.normal) / denom - face.d_shifted / denom
                        segments.append({
                            "x_min": u_a,
                            "x_max": u_b,
                            "A": A,
                            "B": B,
                            "face": face
                        })
                        
            if not segments:
                continue
                
            p_start_3d = slice_origin + truss_dir.Multiply(segments[0]["x_min"])
            p_end_3d = slice_origin + truss_dir.Multiply(segments[-1]["x_max"])
            
            lowest_roof_z = 1e9
            for s in segments:
                lowest_roof_z = min(lowest_roof_z, s["A"] * s["x_min"] + s["B"])
                lowest_roof_z = min(lowest_roof_z, s["A"] * s["x_max"] + s["B"])
                
            intersections_support = self._find_supports_along_slice(p_start_3d, p_end_3d, walls, beams)
            valid_zs = [pt.Z for pt in intersections_support if pt.Z < lowest_roof_z - 0.1]
            roof_base_z = self._get_roof_base_elevation(roof_info)
            joist_z = min(valid_zs) if valid_zs else roof_base_z
            
            y_bc = joist_z + bc_depth / 2.0
            
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
                best_v = -1e9
                best_face = None
                for s in segments:
                    if s["x_min"] - 1e-3 <= x_val <= s["x_max"] + 1e-3:
                        v_val = s["A"] * x_val + s["B"]
                        if v_val > best_v:
                            best_v = v_val
                            best_face = s["face"]
                if best_v > -1e8:
                    profile_nodes.append({"x": x_val, "z": best_v, "face": best_face})
                    
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
            
            # Phase 4 - Validate Profile Generation Near Roof Cuts
            is_valid_profile = True
            for idx in range(len(filtered_nodes) - 1):
                if filtered_nodes[idx]["x"] >= filtered_nodes[idx+1]["x"] - 1e-4:
                    is_valid_profile = False
                    break
                    
            for idx in range(len(filtered_nodes) - 1):
                dx = filtered_nodes[idx+1]["x"] - filtered_nodes[idx]["x"]
                dz = filtered_nodes[idx+1]["z"] - filtered_nodes[idx]["z"]
                if dx > 0:
                    slope = abs(dz / dx)
                    if slope > 5.0:
                        is_valid_profile = False
                        break
                        
            roof_base_z = self._get_roof_base_elevation(roof_info)
            for n in filtered_nodes:
                if n["z"] < roof_base_z - 20.0 or n["z"] > roof_base_z + 100.0:
                    is_valid_profile = False
                    break
                    
            if not is_valid_profile:
                try:
                    from pyrevit import script
                    script.get_output().print_md("> **Warning:** Invalid profile geometry detected at station {0:.3f} ft. Skipping truss generation.".format(t_station))
                except Exception:
                    pass
                continue
            
            for i in range(len(filtered_nodes)-1):
                n1 = filtered_nodes[i]
                n2 = filtered_nodes[i+1]
                pt1 = slice_origin + truss_dir.Multiply(n1["x"]) + XYZ.BasisZ.Multiply(n1["z"])
                pt2 = slice_origin + truss_dir.Multiply(n2["x"]) + XYZ.BasisZ.Multiply(n2["z"])
                if (pt2 - pt1).GetLength() < MIN_MEMBER_LENGTH:
                    continue
                    
                m_tc = FramingMember(FramingMember.STUD, pt1, pt2)
                m_tc.member_type = "TOP_CHORD"
                m_tc.family_name = family_top[0]
                m_tc.type_name = family_top[1]
                m_tc.rotation = _rotation_from_up(pt2 - pt1, n1["face"].normal)
                m_tc.host_kind = roof_info.kind
                m_tc.host_id = roof_info.element_id
                m_tc.disallow_end_joins = True
                members.append(m_tc)
                
            candidate_xs = []
            for n in filtered_nodes:
                if n["z"] >= y_bc - 1e-5:
                    candidate_xs.append(n["x"])
            for i in range(len(filtered_nodes) - 1):
                n1 = filtered_nodes[i]
                n2 = filtered_nodes[i+1]
                z_min = min(n1["z"], n2["z"])
                z_max = max(n1["z"], n2["z"])
                if z_min <= y_bc <= z_max and abs(n2["z"] - n1["z"]) > 1e-5:
                    t = (y_bc - n1["z"]) / (n2["z"] - n1["z"])
                    x_int = n1["x"] + t * (n2["x"] - n1["x"])
                    candidate_xs.append(x_int)
                    
            if not candidate_xs:
                continue
                
            heel_x_L = min(candidate_xs)
            heel_x_R = max(candidate_xs)
            
            if abs(heel_x_R - heel_x_L) < MIN_MEMBER_LENGTH:
                continue
                
            p_bc_start = slice_origin + truss_dir.Multiply(heel_x_L) + XYZ.BasisZ.Multiply(y_bc)
            p_bc_end = slice_origin + truss_dir.Multiply(heel_x_R) + XYZ.BasisZ.Multiply(y_bc)
            
            m_bc = FramingMember(FramingMember.STUD, p_bc_start, p_bc_end)
            m_bc.member_type = "BOTTOM_CHORD"
            m_bc.family_name = family_bottom[0]
            m_bc.type_name = family_bottom[1]
            m_bc.rotation = -math.pi / 2.0
            m_bc.host_kind = roof_info.kind
            m_bc.host_id = roof_info.element_id
            m_bc.disallow_end_joins = True
            members.append(m_bc)
            
            # Web Bracing Generation based on selected Truss Type
            # Find the best ridge line to align the truss peak horizontally
            king_x = None
            ridge_edges = [edge for edge in graph.edges.values() if edge.edge_type == "RIDGE"]
            if ridge_edges:
                crossings = []
                for edge in ridge_edges:
                    A = edge.node_a.pt
                    B = edge.node_b.pt
                    da = (A - slice_origin).DotProduct(slice_normal)
                    db = (B - slice_origin).DotProduct(slice_normal)
                    if abs(da - db) > 1e-7:
                        t = da / (da - db)
                        pt_int = A + (B - A).Multiply(t)
                        x_val = (pt_int - slice_origin).DotProduct(truss_dir)
                        # Ensure the crossing is within or very close to the truss span
                        if heel_x_L - 1.0 <= x_val <= heel_x_R + 1.0:
                            # Distance from actual segment (prioritize actual crossings where 0 <= t <= 1)
                            dist_to_segment = 0.0
                            if t < 0.0:
                                dist_to_segment = abs(t) * (B - A).GetLength()
                            elif t > 1.0:
                                dist_to_segment = (t - 1.0) * (B - A).GetLength()
                            crossings.append((x_val, pt_int, dist_to_segment))
                
                if crossings:
                    # Sort crossings: prioritize actual crossings first, then closest extension
                    crossings.sort(key=lambda c: (c[2], abs(c[0] - (heel_x_L + heel_x_R)/2.0)))
                    king_x = crossings[0][0]
                    
            if king_x is None:
                highest_node = max(filtered_nodes, key=lambda n: n["z"])
                king_x = highest_node["x"]
                
            highest_node = max(filtered_nodes, key=lambda n: n["z"])

            # Phase 1 - Diagnostic Instrumentation
            try:
                from pyrevit import script
                output = script.get_output()
                support_xs = [(pt - slice_origin).DotProduct(truss_dir) for pt in intersections_support]
                sup_min_str = "{0:.3f}".format(min(support_xs)) if support_xs else "None"
                sup_max_str = "{0:.3f}".format(max(support_xs)) if support_xs else "None"
                
                output.print_md("### Truss Debug")
                output.print_md("- **Station:** {0:.3f} ft".format(t_station))
                output.print_md("- **Roof Bounds (Local):** {0:.3f} -> {1:.3f} ft".format(heel_x_L, heel_x_R))
                output.print_md("- **Support Bounds (Local):** {0} -> {1} ft".format(sup_min_str, sup_max_str))
                output.print_md("- **Profile Points:**")
                for n in filtered_nodes:
                    output.print_md("  - ({0:.3f}, {1:.3f})".format(n["x"], n["z"]))
                output.print_md("- **Segments:** {0}".format(len(segments)))
                output.print_md("- **Ridge Location (Local):** {0:.3f}".format(king_x))
            except Exception:
                pass

            def get_top_z(x):
                best_z_val = -1e9
                for s in segments:
                    if s["x_min"] - 1e-3 <= x <= s["x_max"] + 1e-3:
                        z_val = s["A"] * x + s["B"]
                        if z_val > best_z_val:
                            best_z_val = z_val
                if best_z_val > -1e8:
                    return best_z_val
                # Fallback: extrapolate using the nearest boundary segment
                if not segments:
                    return highest_node["z"]
                if x < segments[0]["x_min"]:
                    return segments[0]["A"] * x + segments[0]["B"]
                elif x > segments[-1]["x_max"]:
                    return segments[-1]["A"] * x + segments[-1]["B"]
                return highest_node["z"]

            def add_web(pt_start, pt_end, mtype="WEB_BRACING"):
                if (pt_end - pt_start).GetLength() < MIN_MEMBER_LENGTH:
                    return
                m_web = FramingMember(FramingMember.STUD, pt_start, pt_end)
                m_web.member_type = mtype
                m_web.family_name = family_web[0]
                m_web.type_name = family_web[1]
                m_web.rotation = _rotation_from_up(pt_end - pt_start, XYZ.BasisZ)
                m_web.host_kind = roof_info.kind
                m_web.host_id = roof_info.element_id
                m_web.disallow_end_joins = True
                members.append(m_web)

            selected_truss_type = getattr(self.config, 'truss_type', 'Dynamic')
            span = abs(heel_x_R - heel_x_L)
            
            # Determine dynamic layout if set to Dynamic
            if selected_truss_type == "Dynamic":
                if span < 16.0:
                    truss_type_to_build = "KingPost"
                elif span < 28.0:
                    truss_type_to_build = "Fink"
                else:
                    truss_type_to_build = "Pratt"
            else:
                truss_type_to_build = selected_truss_type

            # 1. King Post Truss Topology
            if truss_type_to_build == "KingPost":
                # Vertical King Post in the center
                pt_bot = slice_origin + truss_dir.Multiply(king_x) + XYZ.BasisZ.Multiply(y_bc)
                pt_top = slice_origin + truss_dir.Multiply(king_x) + XYZ.BasisZ.Multiply(get_top_z(king_x))
                add_web(pt_bot, pt_top, "KING_POST")
                
                # Left diagonal strut: bottom center to left rafter midpoint
                x_mid_L = heel_x_L + (king_x - heel_x_L) * 0.5
                z_mid_L = get_top_z(x_mid_L)
                pt_mid_L = slice_origin + truss_dir.Multiply(x_mid_L) + XYZ.BasisZ.Multiply(z_mid_L)
                add_web(pt_bot, pt_mid_L, "WEB_BRACING")
                
                # Right diagonal strut: bottom center to right rafter midpoint
                x_mid_R = king_x + (heel_x_R - king_x) * 0.5
                z_mid_R = get_top_z(x_mid_R)
                pt_mid_R = slice_origin + truss_dir.Multiply(x_mid_R) + XYZ.BasisZ.Multiply(z_mid_R)
                add_web(pt_bot, pt_mid_R, "WEB_BRACING")

            # 2. Fink Truss (W-Truss) Topology
            elif truss_type_to_build == "Fink":
                pt_bot_ctr = slice_origin + truss_dir.Multiply(king_x) + XYZ.BasisZ.Multiply(y_bc)
                
                # Left side
                x_bot_L = heel_x_L + (king_x - heel_x_L) * 0.5
                x_top_L1 = heel_x_L + (king_x - heel_x_L) * 0.33
                x_top_L2 = heel_x_L + (king_x - heel_x_L) * 0.67
                
                pt_bot_L = slice_origin + truss_dir.Multiply(x_bot_L) + XYZ.BasisZ.Multiply(y_bc)
                pt_top_L1 = slice_origin + truss_dir.Multiply(x_top_L1) + XYZ.BasisZ.Multiply(get_top_z(x_top_L1))
                pt_top_L2 = slice_origin + truss_dir.Multiply(x_top_L2) + XYZ.BasisZ.Multiply(get_top_z(x_top_L2))
                
                add_web(pt_top_L1, pt_bot_L, "WEB_BRACING")
                add_web(pt_bot_L, pt_top_L2, "WEB_BRACING")
                add_web(pt_top_L2, pt_bot_ctr, "WEB_BRACING")
                
                # Right side
                x_bot_R = king_x + (heel_x_R - king_x) * 0.5
                x_top_R1 = king_x + (heel_x_R - king_x) * 0.33
                x_top_R2 = king_x + (heel_x_R - king_x) * 0.67
                
                pt_bot_R = slice_origin + truss_dir.Multiply(x_bot_R) + XYZ.BasisZ.Multiply(y_bc)
                pt_top_R1 = slice_origin + truss_dir.Multiply(x_top_R1) + XYZ.BasisZ.Multiply(get_top_z(x_top_R1))
                pt_top_R2 = slice_origin + truss_dir.Multiply(x_top_R2) + XYZ.BasisZ.Multiply(get_top_z(x_top_R2))
                
                add_web(pt_top_R2, pt_bot_R, "WEB_BRACING")
                add_web(pt_bot_R, pt_top_R1, "WEB_BRACING")
                add_web(pt_top_R1, pt_bot_ctr, "WEB_BRACING")

            # 3. Pratt Truss Topology
            elif truss_type_to_build == "Pratt":
                pt_bot_ctr = slice_origin + truss_dir.Multiply(king_x) + XYZ.BasisZ.Multiply(y_bc)
                pt_top_ctr = slice_origin + truss_dir.Multiply(king_x) + XYZ.BasisZ.Multiply(get_top_z(king_x))
                
                # Center vertical King Post
                add_web(pt_bot_ctr, pt_top_ctr, "KING_POST")
                
                # Left side vertical posts and diagonals (slanted up-right towards center)
                x_L1 = heel_x_L + (king_x - heel_x_L) * 0.33
                x_L2 = heel_x_L + (king_x - heel_x_L) * 0.67
                
                pt_bot_L1 = slice_origin + truss_dir.Multiply(x_L1) + XYZ.BasisZ.Multiply(y_bc)
                pt_top_L1 = slice_origin + truss_dir.Multiply(x_L1) + XYZ.BasisZ.Multiply(get_top_z(x_L1))
                pt_bot_L2 = slice_origin + truss_dir.Multiply(x_L2) + XYZ.BasisZ.Multiply(y_bc)
                pt_top_L2 = slice_origin + truss_dir.Multiply(x_L2) + XYZ.BasisZ.Multiply(get_top_z(x_L2))
                
                add_web(pt_bot_L1, pt_top_L1, "WEB_BRACING")
                add_web(pt_bot_L2, pt_top_L2, "WEB_BRACING")
                
                pt_bot_heel_L = slice_origin + truss_dir.Multiply(heel_x_L) + XYZ.BasisZ.Multiply(y_bc)
                add_web(pt_bot_heel_L, pt_top_L1, "WEB_BRACING")
                add_web(pt_bot_L1, pt_top_L2, "WEB_BRACING")
                add_web(pt_bot_L2, pt_top_ctr, "WEB_BRACING")
                
                # Right side vertical posts and diagonals (slanted up-left towards center)
                x_R1 = king_x + (heel_x_R - king_x) * 0.33
                x_R2 = king_x + (heel_x_R - king_x) * 0.67
                
                pt_bot_R1 = slice_origin + truss_dir.Multiply(x_R1) + XYZ.BasisZ.Multiply(y_bc)
                pt_top_R1 = slice_origin + truss_dir.Multiply(x_R1) + XYZ.BasisZ.Multiply(get_top_z(x_R1))
                pt_bot_R2 = slice_origin + truss_dir.Multiply(x_R2) + XYZ.BasisZ.Multiply(y_bc)
                pt_top_R2 = slice_origin + truss_dir.Multiply(x_R2) + XYZ.BasisZ.Multiply(get_top_z(x_R2))
                
                add_web(pt_bot_R1, pt_top_R1, "WEB_BRACING")
                add_web(pt_bot_R2, pt_top_R2, "WEB_BRACING")
                
                pt_bot_heel_R = slice_origin + truss_dir.Multiply(heel_x_R) + XYZ.BasisZ.Multiply(y_bc)
                add_web(pt_bot_heel_R, pt_top_R2, "WEB_BRACING")
                add_web(pt_bot_R2, pt_top_R1, "WEB_BRACING")
                add_web(pt_bot_R1, pt_top_ctr, "WEB_BRACING")
                    
        # 4. Generate continuous horizontal purlins on top of trusses!
        try:
            members.extend(self._make_purlins(graph, roof_info, mode="truss"))
        except Exception:
            pass
            
        return members
