# -*- coding: utf-8 -*-
"""Assembly generators for topology-aware wall framing.

Provides:
    JoinBoundaryResolver     -- Single source of truth for join footprints.
    CornerAssemblyGenerator  -- Two-stud, California, three-stud corners.
    TIntersectionGenerator   -- Ladder, backing-stud, three-stud-T joins.
    WindowAssembly           -- Complete window framing package.
    DoorAssembly             -- Complete door framing package.

All generators return plain lists of FramingMember objects.  They never
touch the Revit document; placement is handled by the engine.
"""

import math

from wf_geometry import FramingMember, inches_to_feet
from wf_wall_topology import (
    MemberKind,
    Reservation,
    ReservationPriority,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

PLATE_THICKNESS = inches_to_feet(1.5)
STUD_THICKNESS = inches_to_feet(1.5)
DEFAULT_LUMBER_DEPTH = inches_to_feet(3.5)
DEFAULT_LUMBER_WIDTH = inches_to_feet(1.5)
DEFAULT_HEADER_DEPTH = inches_to_feet(5.5)   # typical double-ply header
MIN_MEMBER_LENGTH = inches_to_feet(1.0)
HEADER_PLY_SPACER = inches_to_feet(0.5)
MIN_HEADER_PLY_COUNT = 2
PLATE_ROTATION = -math.pi / 2.0
HEADER_ROTATION = 0.0

# Corner footprint padding on each side of the physical corner.
CORNER_FOOTPRINT_PAD = STUD_THICKNESS * 3.0
# T-intersection footprint half-width on the main wall.
T_FOOTPRINT_HALF = STUD_THICKNESS * 2.0


# ---------------------------------------------------------------------------
# JoinBoundaryResolver
# ---------------------------------------------------------------------------

class JoinBoundaryResolver(object):
    """Single source of truth for joint physical footprints.

    Computes the d-axis intervals to reserve on each participating wall and
    the stud centerline positions the assembly will occupy.

    All distances are in Revit internal units (feet).
    """

    # ---- Corners --------------------------------------------------------

    @staticmethod
    def corner_footprint(host_owner, d_owner, host_secondary, d_secondary,
                         corner_style):
        """Return (owner_reservation, secondary_reservation, stud_positions).

        stud_positions is a dict:
            {
              "owner": [d_1, d_2, ...],
              "secondary": [d_1, ...],
            }
        containing the stud centerlines for each wall.
        """
        # The owner wall generates the corner post flush with its end.
        # The secondary wall butts into the owner.
        stud_half = STUD_THICKNESS * 0.5

        # Owner end stud is at d_owner ± stud_half from the true end.
        # We place it inboard by half-stud so it is fully inside the wall.
        owner_sign = 1.0 if d_owner <= host_owner.length * 0.5 else -1.0
        # Retrieve target layer thicknesses (framing depth)
        owner_depth = host_owner.target_layer.width if getattr(host_owner, "target_layer", None) else DEFAULT_LUMBER_DEPTH
        sec_depth = host_secondary.target_layer.width if getattr(host_secondary, "target_layer", None) else DEFAULT_LUMBER_DEPTH

        # The owner wall framing extends to the outer framing face of the secondary wall.
        # owner_sign points inboard. So -owner_sign points outboard (towards the exterior face).
        sec_outer_face_offset = -owner_sign * (sec_depth * 0.5)
        owner_outer_boundary = d_owner + sec_outer_face_offset
        owner_end_d = owner_outer_boundary + owner_sign * stud_half
        
        # Allow d to extend past analytical endpoints by the secondary depth,
        # since the physical wall solid extends through the corner join.
        owner_lower = -sec_depth
        owner_upper = host_owner.length + sec_depth
        owner_end_d = max(owner_lower, min(owner_upper, owner_end_d))

        # The secondary wall terminates flush against the inner framing face of the owner wall.
        # The inner face of the owner wall framing is in the inboard direction relative to secondary centerline.
        secondary_sign = 1.0 if d_secondary <= host_secondary.length * 0.5 else -1.0
        owner_inner_face_offset = secondary_sign * (owner_depth * 0.5)
        sec_inner_boundary = d_secondary + owner_inner_face_offset
        secondary_end_d = sec_inner_boundary + secondary_sign * stud_half
        
        sec_lower = -owner_depth
        sec_upper = host_secondary.length + owner_depth
        secondary_end_d = max(sec_lower, min(sec_upper, secondary_end_d))

        owner_studs = [owner_end_d]
        secondary_studs = [secondary_end_d]

        if corner_style in ("california", "three_stud"):
            # Add a backing stud one stud-thickness inboard on the owner.
            backing_d = owner_end_d + owner_sign * STUD_THICKNESS
            backing_d = max(owner_lower, min(owner_upper, backing_d))
            owner_studs.append(backing_d)

        if corner_style == "three_stud":
            # Add a return stud one stud-thickness inboard on the secondary.
            return_d = secondary_end_d + secondary_sign * STUD_THICKNESS
            return_d = max(sec_lower, min(sec_upper, return_d))
            secondary_studs.append(return_d)

        # Build reservations wide enough to exclude infill from this zone.
        pad = CORNER_FOOTPRINT_PAD
        if owner_sign > 0:
            owner_res = Reservation(0.0, max(0.0, owner_end_d + pad),
                                    ReservationPriority.CORNER,
                                    "corner_owner_end")
        else:
            owner_res = Reservation(min(host_owner.length, owner_end_d - pad), host_owner.length,
                                    ReservationPriority.CORNER,
                                    "corner_owner_end")

        if secondary_sign > 0:
            sec_res = Reservation(0.0, max(0.0, secondary_end_d + pad),
                                  ReservationPriority.CORNER,
                                  "corner_secondary_end")
        else:
            sec_res = Reservation(min(host_secondary.length, secondary_end_d - pad), host_secondary.length,
                                  ReservationPriority.CORNER,
                                  "corner_secondary_end")

        return (owner_res, sec_res,
                {"owner": owner_studs, "secondary": secondary_studs})

    # ---- T-intersections ------------------------------------------------

    @staticmethod
    def t_footprint(host_main, d_main, host_branch, d_branch,
                    t_style, branch_depth=None):
        """Return (main_reservation, branch_reservation, stud_positions).

        stud_positions = {"main": [...], "branch": [...]}
        """
        stud_half = STUD_THICKNESS * 0.5
        if branch_depth is None:
            tl = getattr(getattr(host_branch, "target_layer", None), "width", 0.0) or 0.0
            branch_depth = max(DEFAULT_LUMBER_DEPTH, float(tl))

        backing_offset = max(STUD_THICKNESS,
                             branch_depth * 0.5 + stud_half)

        # Main wall: two backing studs bracketing d_main.
        main_left = max(stud_half, d_main - backing_offset)
        main_right = min(host_main.length - stud_half, d_main + backing_offset)

        main_studs = []
        branch_studs = []

        if t_style == "ladder_blocking":
            main_studs.append(d_main)   # single nailer on main

        elif t_style == "backing_stud":
            main_studs.append(main_left)
            main_studs.append(main_right)

        else:  # three_stud_t (default)
            main_studs.append(main_left)
            main_studs.append(d_main)
            main_studs.append(main_right)

        # Branch wall: end stud at d_branch.
        branch_sign = 1.0 if d_branch <= host_branch.length * 0.5 else -1.0
        branch_end_d = d_branch + branch_sign * stud_half
        branch_end_d = max(stud_half, min(host_branch.length - stud_half, branch_end_d))
        branch_studs.append(branch_end_d)

        # Reservations.
        main_res = Reservation(
            d_main - backing_offset - STUD_THICKNESS,
            d_main + backing_offset + STUD_THICKNESS,
            ReservationPriority.T,
            "t_main",
        )
        if branch_sign > 0:
            branch_res = Reservation(0.0, branch_end_d + CORNER_FOOTPRINT_PAD,
                                     ReservationPriority.T, "t_branch_end")
        else:
            branch_res = Reservation(
                branch_end_d - CORNER_FOOTPRINT_PAD,
                host_branch.length,
                ReservationPriority.T,
                "t_branch_end",
            )

        return (main_res, branch_res,
                {"main": main_studs, "branch": branch_studs})

    # ---- Openings -------------------------------------------------------

    @staticmethod
    def opening_footprint(opening, header_depth):
        """Return (reservation, stud_positions) for a window/door opening.

        stud_positions = {"king": [left, right], "jack": [left, right]}
        """
        stud_half = STUD_THICKNESS * 0.5
        king_left = opening.left_edge - STUD_THICKNESS * 1.5
        king_right = opening.right_edge + STUD_THICKNESS * 1.5
        jack_left = opening.left_edge - stud_half
        jack_right = opening.right_edge + stud_half

        res = Reservation(
            max(0.0, king_left - STUD_THICKNESS),
            king_right + STUD_THICKNESS,
            ReservationPriority.OPENING,
            "opening",
        )
        return (res, {"king": [king_left, king_right],
                      "jack": [jack_left, jack_right]})


# ---------------------------------------------------------------------------
# Member factory helpers
# ---------------------------------------------------------------------------

def _make_vertical(host, kind, d, bottom_abs_z, top_abs_z,
                   family, type_name, is_column, section_height, section_depth):
    """Create a FramingMember for a vertical stud/column."""
    if top_abs_z - bottom_abs_z < MIN_MEMBER_LENGTH:
        return None
    start = host.point_at_abs(d, bottom_abs_z)
    end = host.point_at_abs(d, top_abs_z)
    if start is None or end is None:
        return None
    m = FramingMember(kind, start, end)
    m.member_type = kind
    m.family_name = family
    m.type_name = type_name
    m.rotation = host.angle
    m.is_column = bool(is_column)
    m.host_kind = "wall_v4"
    m.host_id = host.element_id
    tl = getattr(host, "target_layer", None)
    if tl is not None:
        m.layer_index = getattr(tl, "index", None)
    m.disallow_end_joins = False
    m.section_height = max(STUD_THICKNESS, float(section_height or STUD_THICKNESS))
    m.section_depth = max(STUD_THICKNESS, float(section_depth or DEFAULT_LUMBER_DEPTH))
    return m


def _make_horizontal(host, kind, start_d, end_d, z_abs,
                     family, type_name, section_height, section_depth,
                     rotation=None):
    """Create a FramingMember for a horizontal plate/header/blocking."""
    if end_d - start_d < MIN_MEMBER_LENGTH:
        return None
    if rotation is None:
        rotation = PLATE_ROTATION
    start = host.point_at_abs(start_d, z_abs)
    end = host.point_at_abs(end_d, z_abs)
    if start is None or end is None:
        return None
    m = FramingMember(kind, start, end)
    m.member_type = kind
    m.family_name = family
    m.type_name = type_name
    m.rotation = rotation
    m.is_column = False
    m.host_kind = "wall_v4"
    m.host_id = host.element_id
    tl = getattr(host, "target_layer", None)
    if tl is not None:
        m.layer_index = getattr(tl, "index", None)
    m.disallow_end_joins = True
    m.section_height = max(STUD_THICKNESS, float(section_height or STUD_THICKNESS))
    m.section_depth = max(STUD_THICKNESS, float(section_depth or DEFAULT_LUMBER_DEPTH))
    return m


def _stud_z_range(host, plate_bottom_count, plate_top_count):
    bottom_abs_z = host.base_elevation + PLATE_THICKNESS * plate_bottom_count
    top_abs_z = (max(z for _, z in host.outer_loop)
                 - PLATE_THICKNESS * plate_top_count)
    return bottom_abs_z, top_abs_z


# ---------------------------------------------------------------------------
# Corner assembly generator
# ---------------------------------------------------------------------------

class CornerAssemblyGenerator(object):
    """Generate corner stud assemblies.

    Styles
    ------
    "two_stud"    -- One end stud per wall.  Minimal lumber.
    "california"  -- Owner wall gets a backing stud for drywall nailing.
    "three_stud"  -- Both walls get backing/return studs (maximum backing).
    """

    def __init__(self, config):
        self.config = config

    def generate(self, engine, edge):
        """Return list of FramingMember objects for the corner assembly.

        Parameters
        ----------
        engine : WallCavityFramingV4Engine
        edge : WallTopologyEdge  (must be a corner edge, owner_node set)
        """
        owner_node = edge.owner_node
        secondary_node = edge.secondary_node
        owner_host = owner_node.host
        secondary_host = secondary_node.host

        d_owner = edge.d_on(owner_node)
        d_secondary = edge.d_on(secondary_node)

        corner_style = getattr(self.config, "corner_style", "three_stud")

        owner_res, sec_res, positions = JoinBoundaryResolver.corner_footprint(
            owner_host, d_owner, secondary_host, d_secondary, corner_style
        )

        # Register reservations on both walls.
        owner_node.add_reservation(owner_res)
        secondary_node.add_reservation(sec_res)

        family = self.config.stud_family_name
        type_name = self.config.stud_type_name
        plate_bottom = int(getattr(self.config, "bottom_plate_count", 1))
        plate_top = int(getattr(self.config, "top_plate_count", 2))

        members = []
        bottom_z, top_z = _stud_z_range(owner_host, plate_bottom, plate_top)

        # Owner studs.
        role_map = [MemberKind.CORNER_STUD, MemberKind.CORNER_BACKING_STUD]
        for idx, d in enumerate(positions["owner"]):
            role = role_map[min(idx, len(role_map) - 1)]
            m = _make_vertical(
                owner_host, role, d, bottom_z, top_z,
                family, type_name, True,
                engine._wall_member_depth(owner_host, family, type_name, True),
                engine._wall_member_depth(owner_host, family, type_name, True),
            )
            if m:
                members.append(m)

        # Secondary studs.
        bottom_z_s, top_z_s = _stud_z_range(secondary_host, plate_bottom, plate_top)
        role_map_s = [MemberKind.CORNER_STUD, MemberKind.CORNER_RETURN_STUD]
        for idx, d in enumerate(positions["secondary"]):
            role = role_map_s[min(idx, len(role_map_s) - 1)]
            m = _make_vertical(
                secondary_host, role, d, bottom_z_s, top_z_s,
                family, type_name, True,
                engine._wall_member_depth(secondary_host, family, type_name, True),
                engine._wall_member_depth(secondary_host, family, type_name, True),
            )
            if m:
                members.append(m)

        return members


# ---------------------------------------------------------------------------
# T-intersection generator
# ---------------------------------------------------------------------------

class TIntersectionGenerator(object):
    """Generate T-intersection (and cross-decomposed T) assemblies.

    Styles
    ------
    "ladder_blocking"  -- Single nailer + horizontal blocking ladder.
    "backing_stud"     -- Two flanking studs on the main wall.
    "three_stud_t"     -- Two flanking + one centre stud (maximum nailing).
    """

    def __init__(self, config):
        self.config = config

    def generate(self, engine, edge):
        """Return list of FramingMember objects for the T assembly.

        The owner node is treated as the main wall.
        The secondary node is the branch wall.
        """
        # Dynamically resolve main and branch nodes from T-intersection geometry.
        # The main wall has end_index as None (interior join), while the branch wall meets at an end.
        if edge.end_index_a is None and edge.end_index_b is not None:
            main_node = edge.node_a
            branch_node = edge.node_b
        elif edge.end_index_b is None and edge.end_index_a is not None:
            main_node = edge.node_b
            branch_node = edge.node_a
        else:
            # Decomposed cross intersection or fallback: use owner/secondary nodes
            main_node = edge.owner_node
            branch_node = edge.secondary_node

        main_host = main_node.host
        branch_host = branch_node.host

        d_main = edge.d_on(main_node)
        d_branch = edge.d_on(branch_node)
        t_style = getattr(self.config, "t_intersection_style", "three_stud_t")

        branch_depth = engine._wall_member_depth(
            branch_host, self.config.stud_family_name, self.config.stud_type_name, True
        )

        main_res, branch_res, positions = JoinBoundaryResolver.t_footprint(
            main_host, d_main, branch_host, d_branch, t_style, branch_depth
        )

        main_node.add_reservation(main_res)
        branch_node.add_reservation(branch_res)

        family = self.config.stud_family_name
        type_name = self.config.stud_type_name
        plate_bottom = int(getattr(self.config, "bottom_plate_count", 1))
        plate_top = int(getattr(self.config, "top_plate_count", 2))
        plate_family = self.config.bottom_plate_family_name or family
        plate_type = self.config.bottom_plate_type_name or type_name

        members = []
        bottom_m, top_m = _stud_z_range(main_host, plate_bottom, plate_top)
        bottom_b, top_b = _stud_z_range(branch_host, plate_bottom, plate_top)

        # Main wall members.
        for idx, d in enumerate(positions["main"]):
            if t_style == "ladder_blocking":
                role = MemberKind.T_NAILER
            elif t_style == "backing_stud":
                role = MemberKind.T_BACKING_STUD
            else:
                role = MemberKind.T_BACKING_STUD if idx != 1 else MemberKind.T_NAILER
            m = _make_vertical(
                main_host, role, d, bottom_m, top_m,
                family, type_name, False,
                STUD_THICKNESS,
                engine._wall_member_depth(main_host, family, type_name, False),
            )
            if m:
                members.append(m)

        # Horizontal blocking on main wall for ladder style.
        if t_style == "ladder_blocking" and positions["main"]:
            nailer_d = positions["main"][0]
            half_span = max(inches_to_feet(8.0),
                            min(inches_to_feet(12.0), engine.config.stud_spacing_ft * 0.5))
            clear_h = top_m - bottom_m
            if clear_h > inches_to_feet(16.0):
                for factor in (0.25, 0.5, 0.75):
                    z_abs = bottom_m + clear_h * factor
                    m = _make_horizontal(
                        main_host, MemberKind.T_BLOCKING,
                        max(0.0, nailer_d - half_span),
                        min(main_host.length, nailer_d + half_span),
                        z_abs,
                        plate_family, plate_type,
                        PLATE_THICKNESS,
                        engine._wall_member_depth(main_host, plate_family, plate_type, False),
                    )
                    if m:
                        members.append(m)

        # Branch wall end stud.
        for d in positions["branch"]:
            m = _make_vertical(
                branch_host, MemberKind.T_BRANCH_STUD, d, bottom_b, top_b,
                family, type_name, True,
                engine._wall_member_depth(branch_host, family, type_name, True),
                engine._wall_member_depth(branch_host, family, type_name, True),
            )
            if m:
                members.append(m)

        return members


# ---------------------------------------------------------------------------
# WindowAssembly
# ---------------------------------------------------------------------------

class WindowAssembly(object):
    """Complete framing package for a window opening.

    Always generates:
    - King studs (left and right)
    - Jack studs (left and right)
    - Sill plate
    - Header (double-ply by default)
    - Cripple studs above the header
    - Cripple studs below the sill
    """

    def __init__(self, config):
        self.config = config

    def generate(self, engine, host, node, opening, spacing, occupied):
        """Return members and a Reservation for this opening.

        Parameters
        ----------
        engine : WallCavityFramingV4Engine
        host : WallCavityHostInfoV4
        node : WallTopologyNode or None
        opening : WallCavityOpeningV4
        spacing : float
        occupied : set[float]  -- will be updated in-place with new d positions
        """
        left = opening.left_edge
        right = opening.right_edge
        if right - left < MIN_MEMBER_LENGTH:
            return [], None

        header_family = self.config.header_family_name or self.config.stud_family_name
        header_type = self.config.header_type_name or self.config.stud_type_name
        header_depth = engine._header_depth(header_family, header_type)
        header_width = engine._type_width(header_family, header_type, False)

        plate_family = self.config.bottom_plate_family_name or self.config.stud_family_name
        plate_type = self.config.bottom_plate_type_name or self.config.stud_type_name
        plate_depth = engine._wall_member_depth(host, plate_family, plate_type, False)

        stud_family = self.config.stud_family_name
        stud_type = self.config.stud_type_name
        stud_depth = engine._wall_member_depth(host, stud_family, stud_type, True)

        plate_bottom = int(getattr(self.config, "bottom_plate_count", 1))
        plate_top = int(getattr(self.config, "top_plate_count", 2))
        bottom_z = host.base_elevation + PLATE_THICKNESS * plate_bottom
        top_z_loop = max(z for _, z in host.outer_loop)
        top_z = top_z_loop - PLATE_THICKNESS * plate_top

        header_res, positions = JoinBoundaryResolver.opening_footprint(opening, header_depth)
        members = []

        stud_half = STUD_THICKNESS * 0.5

        king_left = positions["king"][0]
        king_right = positions["king"][1]
        jack_left = positions["jack"][0]
        jack_right = positions["jack"][1]

        # King studs – full height.
        for d, label in ((king_left, "king_L"), (king_right, "king_R")):
            if 0.0 <= d <= host.length:
                m = _make_vertical(host, MemberKind.KING_STUD, d, bottom_z, top_z,
                                   stud_family, stud_type, True, STUD_THICKNESS, stud_depth)
                if m:
                    members.append(m)
                    occupied.add(round(d, 4))

        # Jack studs – up to head height.
        jack_top = host.base_elevation + opening.head_height
        for d in (jack_left, jack_right):
            if 0.0 <= d <= host.length:
                m = _make_vertical(host, MemberKind.JACK_STUD, d, bottom_z, jack_top,
                                   stud_family, stud_type, True, STUD_THICKNESS, stud_depth)
                if m:
                    members.append(m)
                    occupied.add(round(d, 4))

        # Sill plates (configurable count).
        sill_count = int(getattr(self.config, "sill_plate_count", 2))
        placed_sill_count = 0
        for i in range(sill_count):
            if opening.sill_height >= PLATE_THICKNESS * (plate_bottom + i + 1) + MIN_MEMBER_LENGTH:
                offset = (i + 0.5) * PLATE_THICKNESS
                sill_center_z = host.base_elevation + opening.sill_height - offset
                # Spans from left - STUD_THICKNESS to right + STUD_THICKNESS (across jack studs, butting into king studs)
                m = _make_horizontal(host, MemberKind.SILL_PLATE,
                                      left - STUD_THICKNESS, right + STUD_THICKNESS, sill_center_z,
                                      plate_family, plate_type, PLATE_THICKNESS, plate_depth)
                if m:
                    members.append(m)
                    placed_sill_count += 1

        # Header bottom plate.
        header_bp_z = host.base_elevation + opening.head_height + PLATE_THICKNESS * 0.5
        span_left = max(0.0, left - STUD_THICKNESS)
        span_right = min(host.length, right + STUD_THICKNESS)
        m = _make_horizontal(host, MemberKind.HEADER_BOTTOM_PLATE,
                              span_left, span_right, header_bp_z,
                              plate_family, plate_type, PLATE_THICKNESS, plate_depth)
        if m:
            members.append(m)

        # Header plies.
        header_bottom = host.base_elevation + opening.head_height + PLATE_THICKNESS
        header_center = header_bottom + header_depth * 0.5
        header_count = max(MIN_HEADER_PLY_COUNT,
                           int(getattr(self.config, "header_count", MIN_HEADER_PLY_COUNT)))
        for lateral in _header_ply_offsets(host, header_count, header_width):
            start = host.point_at_abs(span_left, header_center, lateral)
            end = host.point_at_abs(span_right, header_center, lateral)
            if start and end:
                m = FramingMember(MemberKind.HEADER, start, end)
                m.member_type = MemberKind.HEADER
                m.family_name = header_family
                m.type_name = header_type
                m.rotation = HEADER_ROTATION
                m.is_column = False
                m.host_kind = "wall_v4"
                m.host_id = host.element_id
                m.disallow_end_joins = True
                m.section_height = max(STUD_THICKNESS, header_depth)
                m.section_depth = max(STUD_THICKNESS, header_width)
                members.append(m)

        header_top = header_bottom + header_depth
        header_tp_z = header_top + PLATE_THICKNESS * 0.5
        m = _make_horizontal(host, MemberKind.HEADER_TOP_PLATE,
                              span_left, span_right, header_tp_z,
                              plate_family, plate_type, PLATE_THICKNESS, plate_depth)
        if m:
            members.append(m)

        header_stack_top = header_top + PLATE_THICKNESS

        # Cripple studs above header.
        if getattr(self.config, "include_cripple_studs", True):
            top_bound = top_z
            if header_stack_top < top_bound - MIN_MEMBER_LENGTH:
                members.extend(_cripples(
                    engine, host, node, left, right, header_stack_top, top_bound,
                    spacing, occupied, stud_family, stud_type, stud_depth,
                ))

        # Cripple studs below sill.
        if getattr(self.config, "include_cripple_studs", True):
            sill_stack_bottom = host.base_elevation + opening.sill_height - PLATE_THICKNESS * placed_sill_count
            if sill_stack_bottom > bottom_z + MIN_MEMBER_LENGTH:
                members.extend(_cripples(
                    engine, host, node, left, right, bottom_z, sill_stack_bottom,
                    spacing, occupied, stud_family, stud_type, stud_depth,
                ))

        return members, header_res


# ---------------------------------------------------------------------------
# DoorAssembly
# ---------------------------------------------------------------------------

class DoorAssembly(object):
    """Complete framing package for a door opening.

    Always generates:
    - King studs
    - Jack studs
    - Header (double-ply)
    - Cripple studs above header (if required)
    """

    def __init__(self, config):
        self.config = config

    def generate(self, engine, host, node, opening, spacing, occupied):
        left = opening.left_edge
        right = opening.right_edge
        if right - left < MIN_MEMBER_LENGTH:
            return [], None

        header_family = self.config.header_family_name or self.config.stud_family_name
        header_type = self.config.header_type_name or self.config.stud_type_name
        header_depth = engine._header_depth(header_family, header_type)
        header_width = engine._type_width(header_family, header_type, False)

        plate_family = self.config.bottom_plate_family_name or self.config.stud_family_name
        plate_type = self.config.bottom_plate_type_name or self.config.stud_type_name
        plate_depth = engine._wall_member_depth(host, plate_family, plate_type, False)

        stud_family = self.config.stud_family_name
        stud_type = self.config.stud_type_name
        stud_depth = engine._wall_member_depth(host, stud_family, stud_type, True)

        plate_bottom = int(getattr(self.config, "bottom_plate_count", 1))
        plate_top = int(getattr(self.config, "top_plate_count", 2))
        bottom_z = host.base_elevation + PLATE_THICKNESS * plate_bottom
        top_z_loop = max(z for _, z in host.outer_loop)
        top_z = top_z_loop - PLATE_THICKNESS * plate_top

        header_res, positions = JoinBoundaryResolver.opening_footprint(opening, header_depth)
        members = []

        king_left = positions["king"][0]
        king_right = positions["king"][1]
        jack_left = positions["jack"][0]
        jack_right = positions["jack"][1]

        # King studs.
        for d in (king_left, king_right):
            if 0.0 <= d <= host.length:
                m = _make_vertical(host, MemberKind.KING_STUD, d, bottom_z, top_z,
                                   stud_family, stud_type, True, STUD_THICKNESS, stud_depth)
                if m:
                    members.append(m)
                    occupied.add(round(d, 4))

        # Jack studs.
        jack_top = host.base_elevation + opening.head_height
        for d in (jack_left, jack_right):
            if 0.0 <= d <= host.length:
                m = _make_vertical(host, MemberKind.JACK_STUD, d, bottom_z, jack_top,
                                   stud_family, stud_type, True, STUD_THICKNESS, stud_depth)
                if m:
                    members.append(m)
                    occupied.add(round(d, 4))

        # Header package.
        span_left = max(0.0, left - STUD_THICKNESS)
        span_right = min(host.length, right + STUD_THICKNESS)
        header_bp_z = host.base_elevation + opening.head_height + PLATE_THICKNESS * 0.5
        m = _make_horizontal(host, MemberKind.HEADER_BOTTOM_PLATE,
                              span_left, span_right, header_bp_z,
                              plate_family, plate_type, PLATE_THICKNESS, plate_depth)
        if m:
            members.append(m)

        header_bottom = host.base_elevation + opening.head_height + PLATE_THICKNESS
        header_center = header_bottom + header_depth * 0.5
        header_count = max(MIN_HEADER_PLY_COUNT,
                           int(getattr(self.config, "header_count", MIN_HEADER_PLY_COUNT)))
        for lateral in _header_ply_offsets(host, header_count, header_width):
            start = host.point_at_abs(span_left, header_center, lateral)
            end = host.point_at_abs(span_right, header_center, lateral)
            if start and end:
                m = FramingMember(MemberKind.HEADER, start, end)
                m.member_type = MemberKind.HEADER
                m.family_name = header_family
                m.type_name = header_type
                m.rotation = HEADER_ROTATION
                m.is_column = False
                m.host_kind = "wall_v4"
                m.host_id = host.element_id
                m.disallow_end_joins = True
                m.section_height = max(STUD_THICKNESS, header_depth)
                m.section_depth = max(STUD_THICKNESS, header_width)
                members.append(m)

        header_top = header_bottom + header_depth
        header_tp_z = header_top + PLATE_THICKNESS * 0.5
        m = _make_horizontal(host, MemberKind.HEADER_TOP_PLATE,
                              span_left, span_right, header_tp_z,
                              plate_family, plate_type, PLATE_THICKNESS, plate_depth)
        if m:
            members.append(m)

        header_stack_top = header_top + PLATE_THICKNESS

        # Cripples above header.
        if getattr(self.config, "include_cripple_studs", True):
            if header_stack_top < top_z - MIN_MEMBER_LENGTH:
                members.extend(_cripples(
                    engine, host, node, left, right, header_stack_top, top_z,
                    spacing, occupied, stud_family, stud_type, stud_depth,
                ))

        return members, header_res


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _cripples(engine, host, node, left, right, bottom_abs_z, top_abs_z,
              spacing, occupied, family, type_name, section_depth):
    """Generate cripple studs between left and right aligned with layout phase."""
    members = []
    if spacing <= 0.0 or top_abs_z - bottom_abs_z < MIN_MEMBER_LENGTH:
        return members

    d_min = left + STUD_THICKNESS * 0.5
    d_max = right - STUD_THICKNESS * 0.5

    if node is not None and getattr(node, "layout_phase_set", False):
        stations = list(node.layout_phase.stations_in_range(d_min, d_max, spacing))
    else:
        stations = []
        d = _next_spacing_station(left, spacing)
        while d < d_max:
            if d > d_min:
                stations.append(d)
            d += spacing

    for d in stations:
        if not _near(d, occupied, STUD_THICKNESS):
            m = _make_vertical(
                host, MemberKind.CRIPPLE_STUD, d,
                bottom_abs_z, top_abs_z,
                family, type_name, True,
                STUD_THICKNESS, section_depth,
            )
            if m:
                members.append(m)
    return members


def _next_spacing_station(start_d, spacing):
    if spacing <= 0.0:
        return start_d
    return math.ceil((start_d + STUD_THICKNESS * 0.5) / spacing) * spacing


def _near(value, occupied, tolerance):
    for existing in occupied:
        if abs(value - existing) < tolerance:
            return True
    return False


def _header_ply_offsets(host, count, ply_width=None):
    count = max(MIN_HEADER_PLY_COUNT, int(count or MIN_HEADER_PLY_COUNT))
    if count <= 1:
        return [0.0]
    ply_width = max(DEFAULT_LUMBER_WIDTH, float(ply_width or DEFAULT_LUMBER_WIDTH))
    tl = getattr(host, "target_layer", None)
    layer_width = float(getattr(tl, "width", 0.0) or 0.0)
    if layer_width <= ply_width:
        layer_width = max(DEFAULT_LUMBER_DEPTH, ply_width * count)
    step = min(ply_width + HEADER_PLY_SPACER,
               max(ply_width, (layer_width - ply_width) / float(count - 1)))
    center = (count - 1) * 0.5
    return [(i - center) * step for i in range(count)]
