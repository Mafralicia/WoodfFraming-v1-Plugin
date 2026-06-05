# -*- coding: utf-8 -*-
"""Wall topology graph for topology-aware framing decisions.

Builds a graph of wall connectivity by analysing pairs of walls for
end-to-end, corner, T-intersection, and cross-intersection joins.
Cross intersections are decomposed into two T-intersections.

Classes
-------
MemberKind          -- Explicit taxonomy of all framing member types.
ReservationPriority -- Priority constants for the reservation system.
LayoutPhase         -- Continuous stud-spacing coordinate system.
WallTopologyNode    -- Graph node wrapping a wall host and layout state.
WallTopologyEdge    -- Graph edge representing a wall-to-wall join.
WallTopologyGraph   -- Full connectivity graph for a set of walls.
"""

import math
import collections

from wf_geometry import inches_to_feet


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOPOLOGY_JOIN_TOL = inches_to_feet(6.0)   # max distance for walls to "meet"
TOPOLOGY_ANGLE_DOT_PARALLEL = 0.97        # |cos θ| above this → parallel
TOPOLOGY_ANGLE_DOT_CROSS = 0.25           # |cos θ| below this → 90°-ish join


# ---------------------------------------------------------------------------
# Member taxonomy
# ---------------------------------------------------------------------------

class MemberKind(object):
    """Explicit taxonomy of every framing member type used by the engine.

    Using string constants (not an Enum) keeps this compatible with
    IronPython 2.7 running inside pyRevit.
    """
    # Standard layout members
    STUD = "STUD"
    SIDE_STUD = "SIDE_STUD"
    KING_STUD = "KING_STUD"
    JACK_STUD = "JACK_STUD"
    CRIPPLE_STUD = "CRIPPLE_STUD"

    # Plate members
    BOTTOM_PLATE = "BOTTOM_PLATE"
    TOP_PLATE = "TOP_PLATE"
    MID_PLATE = "MID_PLATE"
    SILL_PLATE = "SILL_PLATE"
    BLOCKING = "BLOCKING"

    # Header package
    HEADER = "HEADER"
    HEADER_BOTTOM_PLATE = "HEADER_BOTTOM_PLATE"
    HEADER_TOP_PLATE = "HEADER_TOP_PLATE"

    # Corner assembly members
    CORNER_STUD = "CORNER_STUD"
    CORNER_BACKING_STUD = "CORNER_BACKING_STUD"
    CORNER_RETURN_STUD = "CORNER_RETURN_STUD"

    # T-intersection members
    T_BRANCH_STUD = "T_BRANCH_STUD"
    T_BACKING_STUD = "T_BACKING_STUD"
    T_NAILER = "T_NAILER"
    T_BLOCKING = "T_BLOCKING"

    # Groupings used by validation
    CORNER_KINDS = frozenset([
        CORNER_STUD,
        CORNER_BACKING_STUD,
        CORNER_RETURN_STUD,
    ])
    T_KINDS = frozenset([
        T_BRANCH_STUD,
        T_BACKING_STUD,
        T_NAILER,
        T_BLOCKING,
    ])
    OPENING_KINDS = frozenset([
        HEADER,
        HEADER_BOTTOM_PLATE,
        HEADER_TOP_PLATE,
        JACK_STUD,
        KING_STUD,
        SILL_PLATE,
        CRIPPLE_STUD,
    ])
    ASSEMBLY_KINDS = frozenset([
        CORNER_STUD, CORNER_BACKING_STUD, CORNER_RETURN_STUD,
        T_BRANCH_STUD, T_BACKING_STUD, T_NAILER, T_BLOCKING,
        HEADER, HEADER_BOTTOM_PLATE, HEADER_TOP_PLATE,
        JACK_STUD, KING_STUD, SILL_PLATE, CRIPPLE_STUD,
    ])


# ---------------------------------------------------------------------------
# Reservation priority
# ---------------------------------------------------------------------------

class ReservationPriority(object):
    """Numeric priority constants for the reserved-region system.

    Higher numbers win when reservations overlap.
    """
    CORNER = 40
    T = 30
    OPENING = 20
    # Cross intersections are decomposed into two T-intersections, so they
    # inherit PRIORITY_T naturally.


# ---------------------------------------------------------------------------
# Layout phase
# ---------------------------------------------------------------------------

class LayoutPhase(object):
    """Describes the stud-spacing coordinate frame for a wall.

    Parameters
    ----------
    station_origin : float
        The distance along the wall (in Revit feet) that is treated as the
        global origin for spacing purposes.  A spacing station exists at
        every ``station_origin + n * spacing`` for integer *n*.
    station_direction : int
        +1 or -1.  Direction along the wall in which station numbers
        increase.  For the root wall this is always +1.  When propagating
        through a corner the direction may flip so that spacing is
        continuous in the physical coordinate frame.
    station_offset : float
        Additional sub-spacing offset applied when the propagation of
        stations through a join does not land exactly on a nominal
        multiple.  Typically 0.0 for the root wall and small for
        downstream walls.
    """

    def __init__(self, station_origin=0.0, station_direction=1, station_offset=0.0):
        self.station_origin = float(station_origin)
        self.station_direction = int(station_direction)
        self.station_offset = float(station_offset)

    def stations_in_range(self, d_start, d_end, spacing):
        """Yield every spacing station that falls inside [d_start, d_end].

        Parameters
        ----------
        d_start, d_end : float
            Range along the wall in feet.
        spacing : float
            Nominal on-centre spacing in feet.

        Yields
        ------
        float
            Distance along the wall for each station.
        """
        if spacing <= 1e-9:
            return
        # Convert to a 1-D number line aligned with the spacing grid.
        # The global layout origin (d=station_origin) corresponds to grid 0.
        # direction accounts for walls that run "backwards" in phase.
        origin = self.station_origin + self.station_offset
        first_index = int(math.ceil((d_start - origin) / spacing))
        d = origin + first_index * spacing
        while d <= d_end + 1e-9:
            if d >= d_start - 1e-9:
                yield d
            d += spacing

    def propagate_to_corner(self, d_join_on_self, d_join_on_other, spacing):
        """Return a new LayoutPhase for a wall that joins at a corner.

        The idea: the stud station that corresponds to d_join_on_self on
        this wall should map to d_join_on_other on the other wall.

        Parameters
        ----------
        d_join_on_self : float
            Distance along THIS wall to the join point.
        d_join_on_other : float
            Distance along the OTHER wall to the join point.
        spacing : float
            Nominal on-centre spacing.
        """
        # The phase "at the corner" in this wall's coordinate frame.
        phase_at_join = self.station_origin + self.station_offset
        # How far into the spacing cycle is the join?
        if spacing > 1e-9:
            frac = (d_join_on_self - phase_at_join) % spacing
        else:
            frac = 0.0
        # Propagate: other wall's origin should be shifted so that the same
        # fractional position is preserved at d_join_on_other.
        new_origin = d_join_on_other - frac
        # Direction stays +1 for simple cases; can be extended if needed.
        return LayoutPhase(
            station_origin=new_origin,
            station_direction=1,
            station_offset=0.0,
        )

    def propagate_to_branch(self, _d_join_on_main, _d_join_on_branch, _spacing):
        """Return a new LayoutPhase for a T-intersection branch wall.

        Branch walls start their own independent layout from their end-stud
        position.  We return a default phase anchored at the branch's start.
        """
        return LayoutPhase(station_origin=0.0, station_direction=1, station_offset=0.0)

    def __repr__(self):
        return "LayoutPhase(origin={0:.4f}, dir={1}, offset={2:.4f})".format(
            self.station_origin, self.station_direction, self.station_offset
        )


# ---------------------------------------------------------------------------
# Reservation
# ---------------------------------------------------------------------------

class Reservation(object):
    """A reserved interval along a wall's d-axis.

    Infill and layout studs may not be placed within this interval.
    If two reservations overlap the one with higher priority wins (higher
    numbers = higher priority, see :class:`ReservationPriority`).
    """

    def __init__(self, d_start, d_end, priority, description=""):
        self.d_start = float(min(d_start, d_end))
        self.d_end = float(max(d_start, d_end))
        self.priority = int(priority)
        self.description = str(description)

    def contains(self, d):
        return self.d_start - 1e-9 <= d <= self.d_end + 1e-9

    def overlaps(self, other):
        return self.d_start < other.d_end and other.d_start < self.d_end

    def __repr__(self):
        return "Reservation([{0:.3f}, {1:.3f}] p={2} '{3}')".format(
            self.d_start, self.d_end, self.priority, self.description
        )


def is_d_reserved(d, reservations):
    """Return True if *d* falls inside ANY active reservation."""
    for r in reservations:
        if r.contains(d):
            return True
    return False


def merge_reservations(reservations):
    """Merge overlapping reservations, keeping the highest priority.

    Returns a new sorted, merged list.
    """
    if not reservations:
        return []
    sorted_res = sorted(reservations, key=lambda r: r.d_start)
    merged = []
    for res in sorted_res:
        if not merged or res.d_start > merged[-1].d_end + 1e-9:
            merged.append(Reservation(
                res.d_start, res.d_end, res.priority, res.description
            ))
        else:
            last = merged[-1]
            new_end = max(last.d_end, res.d_end)
            new_pri = max(last.priority, res.priority)
            new_desc = last.description if last.priority >= res.priority else res.description
            merged[-1] = Reservation(last.d_start, new_end, new_pri, new_desc)
    return merged


# ---------------------------------------------------------------------------
# Join kinds
# ---------------------------------------------------------------------------

JOIN_CORNER = "corner"
JOIN_T = "t_intersection"
# Cross joins are decomposed into two T-intersections during graph build.


# ---------------------------------------------------------------------------
# Graph nodes and edges
# ---------------------------------------------------------------------------

class WallTopologyNode(object):
    """One wall in the connectivity graph.

    Parameters
    ----------
    host : WallCavityHostInfoV4
        The analysed wall geometry produced by the framing engine.
    """

    def __init__(self, host):
        self.host = host
        self.element_id = host.element_id
        # Sorted list of WallTopologyEdge objects that touch this node.
        self.edges = []
        # Continuous layout phase (set by WallTopologyGraph.compute_layout).
        self.layout_phase = LayoutPhase()
        self.layout_phase_set = False
        # Reservations along this wall's d-axis.
        self.reservations = []

    @property
    def element_id_int(self):
        try:
            return int(_element_id_value(self.element_id))
        except Exception:
            return 0

    def add_reservation(self, reservation):
        self.reservations.append(reservation)

    def is_d_reserved(self, d):
        return is_d_reserved(d, self.reservations)

    def __repr__(self):
        return "WallTopologyNode(id={0}, edges={1})".format(
            _element_id_text(self.element_id), len(self.edges)
        )


class WallTopologyEdge(object):
    """A join between two walls in the connectivity graph.

    Parameters
    ----------
    node_a, node_b : WallTopologyNode
    join_kind : str
        One of JOIN_CORNER or JOIN_T.
    point : XYZ
        The physical intersection point in model space.
    d_a : float
        Distance along node_a's wall where the join occurs.
    d_b : float
        Distance along node_b's wall where the join occurs.
    end_index_a : int or None
        0 or 1 if node_a meets at an end, None if node_a is the main wall.
    end_index_b : int or None
        As above for node_b.
    """

    def __init__(self, node_a, node_b, join_kind, point,
                 d_a, d_b, end_index_a, end_index_b):
        self.node_a = node_a
        self.node_b = node_b
        self.join_kind = join_kind
        self.point = point
        self.d_a = float(d_a)
        self.d_b = float(d_b)
        self.end_index_a = end_index_a
        self.end_index_b = end_index_b
        # Set during ownership resolution.
        self.owner_node = None
        self.secondary_node = None

    def other_node(self, node):
        return self.node_b if node is self.node_a else self.node_a

    def d_on(self, node):
        return self.d_a if node is self.node_a else self.d_b

    def end_index_on(self, node):
        return self.end_index_a if node is self.node_a else self.end_index_b

    @property
    def is_corner(self):
        return self.join_kind == JOIN_CORNER

    @property
    def is_t(self):
        return self.join_kind == JOIN_T

    def __repr__(self):
        return "WallTopologyEdge({0} -- {1} [{2}])".format(
            _element_id_text(self.node_a.element_id),
            _element_id_text(self.node_b.element_id),
            self.join_kind,
        )


# ---------------------------------------------------------------------------
# Topology graph
# ---------------------------------------------------------------------------

class WallTopologyGraph(object):
    """Connectivity graph for a set of walls.

    Usage
    -----
    Build the graph before running any framing calculation::

        graph = WallTopologyGraph(doc, hosts)
        graph.build()
        graph.determine_ownership()
        graph.compute_layout(config.stud_spacing_ft)

    Then pass the graph into every ``calculate_members`` call so that each
    wall can consult the topology.
    """

    def __init__(self, hosts):
        """
        Parameters
        ----------
        hosts : list[WallCavityHostInfoV4]
            Analysed wall host infos — one per wall.
        """
        self._nodes_by_id = {}
        for host in hosts:
            node = WallTopologyNode(host)
            key = _element_id_text(host.element_id)
            self._nodes_by_id[key] = node
        self.nodes = list(self._nodes_by_id.values())
        self.edges = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def node_for_host(self, host):
        """Return the node for the given host, or None."""
        key = _element_id_text(host.element_id)
        return self._nodes_by_id.get(key)

    def build(self):
        """Detect all joins and populate the edge list."""
        # Compare every unique pair of walls.
        for i in range(len(self.nodes)):
            for j in range(i + 1, len(self.nodes)):
                self._analyse_pair(self.nodes[i], self.nodes[j])

    def determine_ownership(self):
        """Assign owner/secondary to each edge geometrically, falling back to lowest ElementId."""
        for edge in self.edges:
            node_a = edge.node_a
            node_b = edge.node_b
            id_a = node_a.element_id_int
            id_b = node_b.element_id_int

            owner = None
            if edge.is_t:
                # Main wall is owner, branch wall is secondary
                if edge.end_index_a is None and edge.end_index_b is not None:
                    owner = node_a
                elif edge.end_index_b is None and edge.end_index_a is not None:
                    owner = node_b
            elif edge.is_corner:
                # Compare physical outboard extensions to see which wall is the through wall
                ha = node_a.host
                hb = node_b.host
                if ha is not None and hb is not None:
                    ext_a = (edge.d_a - ha.raw_domain_start) if edge.end_index_a == 0 else (ha.raw_domain_end - edge.d_a)
                    ext_b = (edge.d_b - hb.raw_domain_start) if edge.end_index_b == 0 else (hb.raw_domain_end - edge.d_b)
                    
                    if ext_a > ext_b + 0.05:
                        owner = node_a
                    elif ext_b > ext_a + 0.05:
                        owner = node_b

            if owner is None:
                # Fallback to lowest ElementId
                owner = node_a if id_a <= id_b else node_b

            edge.owner_node = owner
            edge.secondary_node = node_b if owner is node_a else node_a

    def compute_layout(self, spacing):
        """BFS propagation of LayoutPhase from the root wall outward.

        The root wall is the node with the lowest ElementId (arbitrary but
        deterministic).  Its phase is the default LayoutPhase(origin=0).
        """
        if not self.nodes:
            return
        sorted_nodes = sorted(self.nodes, key=lambda n: n.element_id_int)
        root = sorted_nodes[0]
        root.layout_phase = LayoutPhase(station_origin=0.0)
        root.layout_phase_set = True

        queue = collections.deque([root])
        while queue:
            current = queue.popleft()
            for edge in current.edges:
                neighbour = edge.other_node(current)
                if neighbour.layout_phase_set:
                    continue
                d_self = edge.d_on(current)
                d_other = edge.d_on(neighbour)
                if edge.is_corner:
                    neighbour.layout_phase = current.layout_phase.propagate_to_corner(
                        d_self, d_other, spacing
                    )
                else:
                    neighbour.layout_phase = current.layout_phase.propagate_to_branch(
                        d_self, d_other, spacing
                    )
                neighbour.layout_phase_set = True
                queue.append(neighbour)

        # Any disconnected node gets a default phase.
        for node in self.nodes:
            if not node.layout_phase_set:
                node.layout_phase = LayoutPhase(station_origin=0.0)
                node.layout_phase_set = True

    def edges_for_node(self, node):
        """Return all edges connected to *node*."""
        return node.edges

    def corner_edges_for_node(self, node):
        return [e for e in node.edges if e.is_corner]

    def t_edges_for_node(self, node):
        return [e for e in node.edges if e.is_t]

    def audit_summary(self):
        """Return a short text summary for the pyRevit output pane."""
        lines = [
            "WallTopologyGraph: {0} nodes, {1} edges".format(
                len(self.nodes), len(self.edges)
            )
        ]
        for edge in self.edges:
            lines.append(
                "  {0} -- {1} [{2}] point=({3:.3f},{4:.3f}) owner={5}".format(
                    _element_id_text(edge.node_a.element_id),
                    _element_id_text(edge.node_b.element_id),
                    edge.join_kind,
                    edge.point.X if edge.point else 0.0,
                    edge.point.Y if edge.point else 0.0,
                    _element_id_text(edge.owner_node.element_id) if edge.owner_node else "?",
                )
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _analyse_pair(self, node_a, node_b):
        """Detect joins between two walls and add edges to the graph."""
        ha = node_a.host
        hb = node_b.host
        dir_a = ha.direction
        dir_b = hb.direction

        # Parallel walls do not form joins.
        try:
            dot = abs(dir_a.DotProduct(dir_b))
        except Exception:
            return
        if dot >= TOPOLOGY_ANGLE_DOT_PARALLEL:
            return

        # Find XY intersection of the two wall centrelines.
        point = _line_intersection_xy(ha.start_point, dir_a, hb.start_point, dir_b)
        if point is None:
            return

        # Project the intersection onto each wall.
        d_a = _projection_distance(ha.start_point, dir_a, point)
        d_b = _projection_distance(hb.start_point, dir_b, point)

        # Classify ends or interior.
        state_a, end_a = _wall_state(ha, d_a)
        state_b, end_b = _wall_state(hb, d_b)

        if state_a == "outside" or state_b == "outside":
            return

        # Determine join kind.
        if state_a == "end" and state_b == "end":
            # Corner: both walls terminate at the join.
            edge = WallTopologyEdge(
                node_a, node_b, JOIN_CORNER, point,
                d_a, d_b, end_a, end_b
            )
            self._add_edge(edge)

        elif state_a == "interior" and state_b == "end":
            # T-intersection: a is the main wall, b is the branch.
            edge = WallTopologyEdge(
                node_a, node_b, JOIN_T, point,
                d_a, d_b, None, end_b
            )
            self._add_edge(edge)

        elif state_b == "interior" and state_a == "end":
            # T-intersection: b is the main wall, a is the branch.
            edge = WallTopologyEdge(
                node_a, node_b, JOIN_T, point,
                d_a, d_b, end_a, None
            )
            self._add_edge(edge)

        elif state_a == "interior" and state_b == "interior":
            # Cross intersection: decompose into two T-intersections.
            # We treat node_a as main for the first T (b terminates into a)
            # and node_b as main for the second T (a terminates into b).
            # This mirrors how real framing handles 4-way joints.
            edge1 = WallTopologyEdge(
                node_a, node_b, JOIN_T, point,
                d_a, d_b, None, None
            )
            # Encode cross by leaving both end_indices as None.
            edge2 = WallTopologyEdge(
                node_b, node_a, JOIN_T, point,
                d_b, d_a, None, None
            )
            self._add_edge(edge1)
            self._add_edge(edge2)

    def _add_edge(self, edge):
        self.edges.append(edge)
        edge.node_a.edges.append(edge)
        if edge.node_b is not edge.node_a:
            edge.node_b.edges.append(edge)


# ---------------------------------------------------------------------------
# Module-level geometry helpers
# ---------------------------------------------------------------------------

def _line_intersection_xy(point_a, dir_a, point_b, dir_b):
    """XY line intersection.  Returns None if lines are parallel."""
    denom = dir_a.X * dir_b.Y - dir_a.Y * dir_b.X
    if abs(denom) < 1e-9:
        return None
    dx = point_b.X - point_a.X
    dy = point_b.Y - point_a.Y
    t = (dx * dir_b.Y - dy * dir_b.X) / denom
    try:
        from Autodesk.Revit.DB import XYZ
        return XYZ(point_a.X + dir_a.X * t,
                   point_a.Y + dir_a.Y * t,
                   point_a.Z)
    except Exception:
        return None


def _projection_distance(origin, direction, point):
    """Signed distance of *point* projected onto a ray."""
    try:
        return (point - origin).DotProduct(direction)
    except Exception:
        return 0.0


def _wall_state(host, d):
    """Return (state, end_index) for a projected distance on a wall.

    Returns
    -------
    state : str
        ``"end"``, ``"interior"``, or ``"outside"``
    end_index : int or None
    """
    tol = TOPOLOGY_JOIN_TOL
    if d < -tol or d > host.length + tol:
        return "outside", None
    if d <= tol:
        return "end", 0
    if d >= host.length - tol:
        return "end", 1
    return "interior", None


def _element_id_text(element_id):
    if element_id is None:
        return "None"
    v = getattr(element_id, "IntegerValue", getattr(element_id, "Value", None))
    return str(v) if v is not None else "None"


def _element_id_value(element_id):
    if element_id is None:
        return None
    return getattr(element_id, "IntegerValue", getattr(element_id, "Value", None))
