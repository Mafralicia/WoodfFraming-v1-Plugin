# -*- coding: utf-8 -*-
"""Joining generated framing members to each other in Revit.

Placed framing members were never joined. The analytical geometry is
correct -- a stud's ends land exactly on the plate faces, with no gap and
no overlap -- but two abutting structural solids that Revit has not been
told to join still read as two separate objects: a seam line in 3D, and
separate hatched outlines in a section or detail. In a framing model that
is what "the connections are not right" looks like, even when every
member is in exactly the right place.

Joining is a finishing step, not a geometry fix, so it changes nothing
about member positions, lengths or the take-off. It only tells Revit that
where two members meet they are one continuous piece of construction.

Only vertical-to-horizontal pairs are considered -- studs against plates
and tracks. That is where the visible seams are, and it keeps the work
proportional: a wall has a handful of horizontal members and many
vertical ones, so the pairing stays small instead of growing with the
square of the member count.
"""


def _bboxes_touch(box_a, box_b, tolerance):
    """True when two bounding boxes overlap or come within tolerance.

    Members that merely pass near each other are excluded; only members
    that actually meet are worth joining.
    """
    if box_a is None or box_b is None:
        return False
    try:
        return (box_a.Min.X - tolerance <= box_b.Max.X
                and box_b.Min.X - tolerance <= box_a.Max.X
                and box_a.Min.Y - tolerance <= box_b.Max.Y
                and box_b.Min.Y - tolerance <= box_a.Max.Y
                and box_a.Min.Z - tolerance <= box_b.Max.Z
                and box_b.Min.Z - tolerance <= box_a.Max.Z)
    except Exception:
        return False


def join_placed_members(doc, placed_pairs, tolerance=0.02):
    """Join each vertical member to the horizontal members it meets.

    placed_pairs is the engine's [(member, instance), ...] list, so the
    calculated member tells us which instances are vertical without
    re-deriving it from Revit geometry.

    Returns (joined, failed). Never raises: joining is cosmetic, and a
    model where some pair refuses to join must not lose the framing that
    was already placed successfully.
    """
    try:
        from Autodesk.Revit.DB import JoinGeometryUtils
    except Exception:
        return (0, 0)

    verticals, horizontals = [], []
    for member, instance in placed_pairs or ():
        if instance is None:
            continue
        target = verticals if getattr(member, "is_column", False) else horizontals
        target.append(instance)

    if not verticals or not horizontals:
        return (0, 0)

    # Bounding boxes are read once per instance rather than per pair.
    def _box(instance):
        try:
            return instance.get_BoundingBox(None)
        except Exception:
            return None

    horizontal_boxes = [(item, _box(item)) for item in horizontals]

    joined = failed = 0
    for vertical in verticals:
        vertical_box = _box(vertical)
        if vertical_box is None:
            continue
        for horizontal, horizontal_box in horizontal_boxes:
            if not _bboxes_touch(vertical_box, horizontal_box, tolerance):
                continue
            try:
                if JoinGeometryUtils.AreElementsJoined(doc, vertical, horizontal):
                    continue
                JoinGeometryUtils.JoinGeometry(doc, vertical, horizontal)
                joined += 1
            except Exception:
                # Revit refuses some pairs -- non-intersecting solids, or
                # members already joined through a third element. Neither
                # is worth interrupting the run for.
                failed += 1
    return (joined, failed)
