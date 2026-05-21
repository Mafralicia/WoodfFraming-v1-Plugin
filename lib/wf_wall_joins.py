# -*- coding: utf-8 -*-
"""Wall-join helpers for corner and partition-backing stud placement."""

from wf_geometry import inches_to_feet
from wf_host import analyze_wall_host


END_JOIN_TOL = inches_to_feet(2.5)
INTERSECTION_TOL = inches_to_feet(1.0)
END_CLEARANCE = inches_to_feet(4.5)
PARALLEL_DOT_TOL = 0.98


class EndJoinPlan(object):
    """Stud placement plan for one wall end."""

    def __init__(self):
        self.has_join = False
        self.join_kind = None
        self.is_owner = True
        self.other_wall_id = None
        self.other_layer_width = 0.0
        self.inset = 0.0
        self.positions = []


class IntersectionJoinPlan(object):
    """Stud placement plan for a wall intersection along the wall run."""

    def __init__(self):
        self.distance = 0.0
        self.other_wall_id = None
        self.other_layer_width = 0.0
        self.positions = []


class WallJoinPlan(object):
    """All join-driven stud positions for a wall."""

    def __init__(self):
        self.ends = {0: EndJoinPlan(), 1: EndJoinPlan()}
        self.intersections = []


def build_wall_join_plan(doc, wall_info, config, stud_thickness):
    """Return corner and T-junction stud positions for a wall.

    Uses Revit's LocationCurve.ElementsAtJoin when available for end joins,
    then falls back to geometric matching for the rest.
    """
    plan = WallJoinPlan()
    if wall_info is None or wall_info.element is None:
        return plan

    this_id = _element_id_value(wall_info.element_id)
    joined_ids = _joined_wall_ids(wall_info.element)

    try:
        from Autodesk.Revit.DB import FilteredElementCollector, Line, Wall

        walls = (
            FilteredElementCollector(doc)
            .OfClass(Wall)
            .WhereElementIsNotElementType()
        )
    except Exception:
        return plan

    for other in walls:
        other_id = _element_id_value(other.Id)
        if other_id == this_id:
            continue

        other_loc = other.Location
        if other_loc is None:
            continue
        other_curve = other_loc.Curve
        if not isinstance(other_curve, Line):
            continue

        try:
            other_info = analyze_wall_host(doc, other, config)
        except Exception:
            other_info = None
        if other_info is None:
            continue

        other_dir = other_info.direction
        if _is_parallel(wall_info.direction, other_dir):
            continue

        # Check endpoints of this wall against other wall's raw_curve
        for end_index in (0, 1):
            is_official_join = other_id in joined_ids[end_index]
            if joined_ids[end_index] and not is_official_join:
                continue

            pt_A = wall_info.raw_curve.point_at(0.0 if end_index == 0 else wall_info.raw_curve.length)
            dist_along, perp_dist, proj_pt = other_info.raw_curve.project(pt_A)

            # Check if within tolerance
            if perp_dist <= END_JOIN_TOL:
                # Must be on or near the wall segment B
                if dist_along >= -END_JOIN_TOL and dist_along <= other_info.raw_curve.length + END_JOIN_TOL:
                    # Corner join if near either end of B
                    is_corner = (dist_along <= END_JOIN_TOL or dist_along >= other_info.raw_curve.length - END_JOIN_TOL)
                    
                    _merge_end_plan(
                        plan.ends[end_index],
                        this_id,
                        other_id,
                        other_info,
                        stud_thickness,
                        "corner" if is_corner else "termination",
                    )

        # Check if endpoints of other wall project onto this wall's interior
        for other_pt in (other_info.raw_curve.start_point, other_info.raw_curve.end_point):
            dist_along, perp_dist, proj_pt = wall_info.raw_curve.project(other_pt)
            if perp_dist <= INTERSECTION_TOL:
                domain_start = getattr(wall_info, "domain_start", 0.0)
                framing_distance = dist_along - domain_start
                if END_CLEARANCE < framing_distance < (wall_info.length - END_CLEARANCE):
                    _merge_intersection_plan(
                        plan.intersections,
                        framing_distance,
                        other_id,
                        other_info,
                        stud_thickness,
                    )

    for end_index in (0, 1):
        end_plan = plan.ends[end_index]
        if not end_plan.has_join:
            continue

        if end_plan.join_kind == "termination":
            end_plan.positions = _end_positions(
                wall_info.length,
                end_index,
                (0.0,),
            )
        elif end_plan.is_owner:
            end_plan.positions = _end_positions(
                wall_info.length,
                end_index,
                (0.0, stud_thickness),
            )
        else:
            inset = max(stud_thickness, end_plan.inset)
            end_plan.positions = _end_positions(
                wall_info.length,
                end_index,
                (inset,),
            )

    return plan


def _merge_end_plan(end_plan, this_id, other_id, other_info, stud_thickness, join_kind):
    width = _layer_width(other_info, stud_thickness)
    inset = max(stud_thickness, (width / 2.0) + (stud_thickness / 2.0))

    if not end_plan.has_join:
        end_plan.is_owner = True

    end_plan.has_join = True
    if _join_kind_rank(join_kind) > _join_kind_rank(end_plan.join_kind):
        end_plan.join_kind = join_kind

    if end_plan.join_kind == "corner":
        end_plan.is_owner = end_plan.is_owner and (this_id <= other_id)
    else:
        end_plan.is_owner = False

    if end_plan.other_wall_id is None or other_id < end_plan.other_wall_id:
        end_plan.other_wall_id = other_id
    if width > end_plan.other_layer_width:
        end_plan.other_layer_width = width
    if inset > end_plan.inset:
        end_plan.inset = inset


def _join_kind_rank(join_kind):
    if join_kind == "corner":
        return 2
    if join_kind == "termination":
        return 1
    return 0


def _merge_intersection_plan(items, distance, other_id, other_info, stud_thickness):
    width = _layer_width(other_info, stud_thickness)
    offset = max(stud_thickness, (width / 2.0) + (stud_thickness / 2.0))

    target = None
    for item in items:
        if abs(item.distance - distance) < stud_thickness:
            target = item
            break

    if target is None:
        target = IntersectionJoinPlan()
        target.distance = distance
        items.append(target)

    if target.other_wall_id is None or other_id < target.other_wall_id:
        target.other_wall_id = other_id
    if width > target.other_layer_width:
        target.other_layer_width = width

    offset = max(
        stud_thickness,
        (target.other_layer_width / 2.0) + (stud_thickness / 2.0),
    )
    target.positions = [distance - offset, distance + offset]


def _intersection_distance(raw_start_pt, wall_info, point):
    distance = _projection_distance(raw_start_pt, wall_info.direction, point)
    perp = _perpendicular_distance(raw_start_pt, wall_info.direction, point)
    if perp > INTERSECTION_TOL:
        return None

    # Check clearance and bounds relative to the framing line
    domain_start = getattr(wall_info, "domain_start", 0.0)
    framing_distance = distance - domain_start
    if framing_distance <= END_CLEARANCE or framing_distance >= wall_info.length - END_CLEARANCE:
        return None

    return framing_distance


def _matching_end_index(target, start_pt, end_pt):
    if _xy_distance(target, start_pt) <= END_JOIN_TOL:
        return 0
    if _xy_distance(target, end_pt) <= END_JOIN_TOL:
        return 1
    return None


def _end_positions(length, end_index, offsets):
    positions = []
    for offset in offsets:
        pos = offset if end_index == 0 else length - offset
        if pos < -1e-9 or pos > length + 1e-9:
            continue
        if not _contains_close(positions, pos):
            positions.append(pos)
    return positions


def _contains_close(values, target):
    for value in values:
        if abs(value - target) < 1e-6:
            return True
    return False


def _joined_wall_ids(wall):
    result = {0: set(), 1: set()}
    loc = getattr(wall, "Location", None)
    if loc is None:
        return result

    for end_index in (0, 1):
        joined = None
        try:
            joined = loc.get_ElementsAtJoin(end_index)
        except Exception:
            try:
                joined = loc.ElementsAtJoin[end_index]
            except Exception:
                joined = None
        if joined is None:
            continue

        for elem in _iter_joined_elements(joined):
            elem_id = getattr(elem, "Id", None)
            if elem_id is None:
                continue
            result[end_index].add(_element_id_value(elem_id))

    return result


def _iter_joined_elements(joined):
    try:
        for elem in joined:
            yield elem
        return
    except Exception:
        pass

    try:
        count = joined.Size
    except Exception:
        count = 0
    for index in range(count):
        try:
            yield joined[index]
        except Exception:
            continue


def _layer_width(wall_info, stud_thickness):
    target_layer = getattr(wall_info, "target_layer", None)
    if target_layer is not None and target_layer.width > 1e-9:
        return target_layer.width

    base_info = getattr(wall_info, "wall_info", None)
    width = getattr(base_info, "width", 0.0)
    if width > 1e-9:
        return width

    return stud_thickness


def _framing_line_endpoints(wall_info):
    offset = getattr(wall_info, "target_layer_offset", 0.0)
    start = _shift_point(wall_info.start_point, wall_info.normal, offset)
    end = _shift_point(wall_info.end_point, wall_info.normal, offset)
    return start, end


def _raw_line_endpoints(wall_info):
    if hasattr(wall_info, "raw_curve") and wall_info.raw_curve is not None:
        return wall_info.raw_curve.start_point, wall_info.raw_curve.end_point
    return wall_info.start_point, wall_info.end_point


def _element_id_value(element_id):
    return getattr(element_id, "Value", getattr(element_id, "IntegerValue", element_id))


def _shift_point(point, normal, offset):
    if point is None or normal is None or abs(offset) < 1e-9:
        return point
    return point + normal.Multiply(offset)


def _projection_distance(start_pt, direction, point):
    vec = point - start_pt
    return vec.DotProduct(direction)


def _perpendicular_distance(start_pt, direction, point):
    distance = _projection_distance(start_pt, direction, point)
    projected = start_pt + direction.Multiply(distance)
    return _xy_distance(projected, point)


def _is_parallel(dir_a, dir_b):
    try:
        return abs(dir_a.DotProduct(dir_b)) >= PARALLEL_DOT_TOL
    except Exception:
        return False


def _xy_distance(a, b):
    dx = a.X - b.X
    dy = a.Y - b.Y
    return (dx * dx + dy * dy) ** 0.5


def visualize_wall_joins_in_revit(doc, host, join_plan):
    """Draw model lines in Revit for debug visualization of joins, curves and axes."""
    try:
        from Autodesk.Revit.DB import XYZ, Plane, SketchPlane, Line, FilteredElementCollector, GraphicsStyle
        
        base_z = host.base_elevation + 1.0 # 1 foot above wall base to be visible
        
        plane_origin = XYZ(host.start_point.X, host.start_point.Y, base_z)
        normal = XYZ.BasisZ
        
        try:
            geom_plane = Plane.CreateByNormalAndOrigin(normal, plane_origin)
        except Exception:
            geom_plane = Plane(normal, plane_origin)
            
        sketch_plane = SketchPlane.Create(doc, geom_plane)
        
        # Try to find colored lines styles
        styles = FilteredElementCollector(doc).OfClass(GraphicsStyle).ToElements()
        style_map = {}
        for s in styles:
            name = s.Name.lower()
            if "blue" in name:
                style_map["blue"] = s
            elif "red" in name:
                style_map["red"] = s
            elif "green" in name:
                style_map["green"] = s
        
        def draw_segment(p0, p1, style_name=None):
            start = XYZ(p0.X, p0.Y, base_z)
            end = XYZ(p1.X, p1.Y, base_z)
            if start.DistanceTo(end) < 0.001:
                return None
            line = Line.CreateBound(start, end)
            model_curve = doc.Create.NewModelCurve(line, sketch_plane)
            if style_name in style_map:
                try:
                    model_curve.LineStyle = style_map[style_name]
                except Exception:
                    pass
            return model_curve
            
        # Draw Raw Curve (represents raw location line) - Blue
        if host.raw_curve is not None:
            draw_segment(host.raw_curve.start_point, host.raw_curve.end_point, "blue")
            
        # Draw Framing Curve - Red
        if host.framing_curve is not None:
            draw_segment(host.framing_curve.start_point, host.framing_curve.end_point, "red")
            
        # Draw Local Axes at start point
        if host.direction is not None and host.normal is not None:
            start_pt = host.start_point
            x_end = start_pt + host.direction.Multiply(2.0)
            y_end = start_pt + host.normal.Multiply(1.0)
            # Longitudinal X is Blue/Green/Default
            draw_segment(start_pt, x_end, "green")
            # Lateral Y is Red/Default
            draw_segment(start_pt, y_end, "red")
            
        # Draw Join match lines/points - Green
        if join_plan is not None:
            for end_index in (0, 1):
                end_plan = join_plan.ends[end_index]
                if end_plan.has_join:
                    pt = host.framing_curve.point_at(0.0 if end_index == 0 else host.framing_curve.length)
                    cross_1_start = pt - host.normal.Multiply(0.5)
                    cross_1_end = pt + host.normal.Multiply(0.5)
                    cross_2_start = pt - host.direction.Multiply(0.5)
                    cross_2_end = pt + host.direction.Multiply(0.5)
                    draw_segment(cross_1_start, cross_1_end, "green")
                    draw_segment(cross_2_start, cross_2_end, "green")
                    
            for intersection in join_plan.intersections:
                pt = host.framing_curve.point_at(intersection.distance)
                cross_1_start = pt - host.normal.Multiply(0.5)
                cross_1_end = pt + host.normal.Multiply(0.5)
                cross_2_start = pt - host.direction.Multiply(0.5)
                cross_2_end = pt + host.direction.Multiply(0.5)
                draw_segment(cross_1_start, cross_1_end, "green")
                draw_segment(cross_2_start, cross_2_end, "green")
                
    except Exception:
        pass
