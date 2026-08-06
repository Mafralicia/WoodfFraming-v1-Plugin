# -*- coding: utf-8 -*-
"""Ceiling framing engine using the shared host-local framing core."""

from wf_geometry import FramingMember, inches_to_feet
from wf_config import (
    CEILING_DIRECTION_AUTO,
    CEILING_DIRECTION_X,
    CEILING_DIRECTION_Y,
    CEILING_DIRECTION_BOTH,
    CEILING_PLACEMENT_ABOVE,
    CEILING_PLACEMENT_CENTER,
    MATERIAL_WOOD,
)
from wf_host import analyze_ceiling_host
from wf_materials import (
    MATERIAL_STEEL,
    actual_dims_from_text,
    fallback_depth_ft,
    warn_unresolved_steel_dims,
)
from wf_placement import BaseFramingEngine


MIN_MEMBER_LENGTH = inches_to_feet(1.0)


class CeilingFramingEngine(BaseFramingEngine):
    """Calculates and places joists and rim joists for a ceiling."""

    def calculate_members(self, ceiling):
        """Calculate framing members for a ceiling."""
        ceiling_info = analyze_ceiling_host(self.doc, ceiling, self.config)
        if ceiling_info is None:
            return [], None

        self._warn_if_steel_joist_dims_unresolved()

        members = []
        members.extend(self._calc_joists(ceiling_info))
        members.extend(self._calc_rim_joists(ceiling_info))
        return members, ceiling_info

    def _calc_joists(self, ceiling_info):
        """Place joists along the shorter span, clipped to the host profile."""
        members = []
        spacing = self.config.stud_spacing_ft
        if spacing <= 0.0:
            return members

        min_x, max_x, min_y, max_y = ceiling_info.bounds
        span_x = max_x - min_x
        span_y = max_y - min_y
        if span_x < MIN_MEMBER_LENGTH or span_y < MIN_MEMBER_LENGTH:
            return members

        layout_axis = self._resolve_layout_axis(span_x, span_y)

        # Determine run axes
        if layout_axis == "both":
            primary_axis = "x" if span_x <= span_y else "y"
            secondary_axis = "y" if primary_axis == "x" else "x"
        else:
            primary_axis = layout_axis
            secondary_axis = None

        grid_style = getattr(self.config, "ceiling_grid_style", "split")

        # Track the coordinates of the primary joists for splitting secondary joists
        primary_coords = []

        if primary_axis == "x":
            coords = self._get_layout_coords(min_y, max_y, spacing)
            primary_coords = coords
            for coord in coords:
                intervals = ceiling_info.scanline_intervals("y", coord)
                for start_x, end_x in intervals:
                    if end_x - start_x < MIN_MEMBER_LENGTH:
                        continue
                    start_pt = self._member_point(
                        ceiling_info,
                        start_x,
                        coord,
                        self.config.stud_family_name,
                        self.config.stud_type_name,
                    )
                    end_pt = self._member_point(
                        ceiling_info,
                        end_x,
                        coord,
                        self.config.stud_family_name,
                        self.config.stud_type_name,
                    )
                    member = FramingMember(FramingMember.STUD, start_pt, end_pt)
                    member.member_type = "CEILING_JOIST"
                    member.family_name = self.config.stud_family_name
                    member.type_name = self.config.stud_type_name
                    self._apply_member_rule(member, ceiling_info)
                    members.append(member)
        else:
            coords = self._get_layout_coords(min_x, max_x, spacing)
            primary_coords = coords
            for coord in coords:
                intervals = ceiling_info.scanline_intervals("x", coord)
                for start_y, end_y in intervals:
                    if end_y - start_y < MIN_MEMBER_LENGTH:
                        continue
                    start_pt = self._member_point(
                        ceiling_info,
                        coord,
                        start_y,
                        self.config.stud_family_name,
                        self.config.stud_type_name,
                    )
                    end_pt = self._member_point(
                        ceiling_info,
                        coord,
                        end_y,
                        self.config.stud_family_name,
                        self.config.stud_type_name,
                    )
                    member = FramingMember(FramingMember.STUD, start_pt, end_pt)
                    member.member_type = "CEILING_JOIST"
                    member.family_name = self.config.stud_family_name
                    member.type_name = self.config.stud_type_name
                    self._apply_member_rule(member, ceiling_info)
                    members.append(member)

        # Place secondary joists (if in both-directions mode)
        if secondary_axis is not None:
            if secondary_axis == "y":
                coords = self._get_layout_coords(min_x, max_x, spacing)
                for coord in coords:
                    intervals = ceiling_info.scanline_intervals("x", coord)
                    for start_y, end_y in intervals:
                        # Determine sub-intervals (split at primary coords or continuous)
                        sub_intervals = []
                        if grid_style == "split":
                            # Find all primary joist Y coords between start_y and end_y
                            split_pts = sorted([y for y in primary_coords if start_y + 1e-3 < y < end_y - 1e-3])
                            y_prev = start_y
                            for y in split_pts:
                                if y - y_prev >= MIN_MEMBER_LENGTH:
                                    sub_intervals.append((y_prev, y))
                                y_prev = y
                            if end_y - y_prev >= MIN_MEMBER_LENGTH:
                                sub_intervals.append((y_prev, end_y))
                        else:
                            sub_intervals = [(start_y, end_y)]

                        for sub_start_y, sub_end_y in sub_intervals:
                            start_pt = self._member_point(
                                ceiling_info,
                                coord,
                                sub_start_y,
                                self.config.stud_family_name,
                                self.config.stud_type_name,
                            )
                            end_pt = self._member_point(
                                ceiling_info,
                                coord,
                                sub_end_y,
                                self.config.stud_family_name,
                                self.config.stud_type_name,
                            )
                            member = FramingMember(FramingMember.STUD, start_pt, end_pt)
                            member.member_type = "CEILING_JOIST"
                            member.family_name = self.config.stud_family_name
                            member.type_name = self.config.stud_type_name
                            self._apply_member_rule(member, ceiling_info)
                            members.append(member)
            else:
                coords = self._get_layout_coords(min_y, max_y, spacing)
                for coord in coords:
                    intervals = ceiling_info.scanline_intervals("y", coord)
                    for start_x, end_x in intervals:
                        # Determine sub-intervals (split at primary coords or continuous)
                        sub_intervals = []
                        if grid_style == "split":
                            # Find all primary joist X coords between start_x and end_x
                            split_pts = sorted([x for x in primary_coords if start_x + 1e-3 < x < end_x - 1e-3])
                            x_prev = start_x
                            for x in split_pts:
                                if x - x_prev >= MIN_MEMBER_LENGTH:
                                    sub_intervals.append((x_prev, x))
                                x_prev = x
                            if end_x - x_prev >= MIN_MEMBER_LENGTH:
                                sub_intervals.append((x_prev, end_x))
                        else:
                            sub_intervals = [(start_x, end_x)]

                        for sub_start_x, sub_end_x in sub_intervals:
                            start_pt = self._member_point(
                                ceiling_info,
                                sub_start_x,
                                coord,
                                self.config.stud_family_name,
                                self.config.stud_type_name,
                            )
                            end_pt = self._member_point(
                                ceiling_info,
                                sub_end_x,
                                coord,
                                self.config.stud_family_name,
                                self.config.stud_type_name,
                            )
                            member = FramingMember(FramingMember.STUD, start_pt, end_pt)
                            member.member_type = "CEILING_JOIST"
                            member.family_name = self.config.stud_family_name
                            member.type_name = self.config.stud_type_name
                            self._apply_member_rule(member, ceiling_info)
                            members.append(member)

        return members

    def _calc_rim_joists(self, ceiling_info):
        """Place rim joists along each boundary segment."""
        members = []
        family_name = (
            self.config.bottom_plate_family_name or self.config.stud_family_name
        )
        type_name = (
            self.config.bottom_plate_type_name or self.config.stud_type_name
        )

        for loop in ceiling_info.boundary_loops_local:
            count = len(loop)
            for index in range(count):
                start_local = loop[index]
                end_local = loop[(index + 1) % count]
                dx = end_local[0] - start_local[0]
                dy = end_local[1] - start_local[1]
                if (dx * dx + dy * dy) ** 0.5 < MIN_MEMBER_LENGTH:
                    continue

                start_pt = self._member_point(
                    ceiling_info,
                    start_local[0],
                    start_local[1],
                    family_name,
                    type_name,
                )
                end_pt = self._member_point(
                    ceiling_info,
                    end_local[0],
                    end_local[1],
                    family_name,
                    type_name,
                )
                member = FramingMember(FramingMember.BOTTOM_PLATE, start_pt, end_pt)
                member.member_type = "CEILING_RIM_JOIST"
                member.family_name = family_name
                member.type_name = type_name
                self._apply_member_rule(member, ceiling_info)
                members.append(member)

        return members

    def _apply_member_rule(self, member, ceiling_info):
        """Attach ceiling host placement metadata to a generated member."""
        member.host_kind = ceiling_info.kind
        member.host_id = ceiling_info.element_id
        if ceiling_info.target_layer is not None:
            member.layer_index = ceiling_info.target_layer.index

    def _get_layout_coords(self, min_value, max_value, spacing):
        """Resolve layout coordinates based on config style."""
        mode = getattr(self.config, "ceiling_layout_mode", "standard")
        if mode == "centered":
            return self._centered_coords(min_value, max_value, spacing)
        return self._layout_coords(min_value, max_value, spacing)

    @staticmethod
    def _layout_coords(min_value, max_value, spacing):
        """Generate centered framing coordinates from both edges inward."""
        span = max_value - min_value
        if span <= 1e-9 or spacing <= 1e-9:
            return []

        interval_count = int(span / spacing)
        if interval_count <= 1:
            return [(min_value + max_value) / 2.0]

        edge_gap = (span - (interval_count * spacing)) / 2.0
        coords = []
        coord = min_value + edge_gap + spacing
        limit = max_value - edge_gap - 1e-9
        while coord < limit:
            coords.append(coord)
            coord += spacing
        if not coords:
            coords.append((min_value + max_value) / 2.0)
        return coords

    @staticmethod
    def _centered_coords(min_value, max_value, spacing):
        """Generate mathematically centered framing coordinates with symmetric edge gaps."""
        span = max_value - min_value
        if span <= 1e-9 or spacing <= 1e-9:
            return []

        center = (min_value + max_value) / 2.0
        import math
        N = int(math.ceil(span / spacing)) - 1
        if N <= 0:
            coords = [center]
        else:
            coords = []
            if N % 2 == 1:
                half = N // 2
                for i in range(-half, half + 1):
                    coords.append(center + i * spacing)
            else:
                half = N // 2
                for i in range(-half, half):
                    coords.append(center + (i + 0.5) * spacing)

        # Filter out coordinates that are extremely close to the edges
        tol = 1e-3
        filtered = [c for c in coords if min_value + tol < c < max_value - tol]
        if not filtered:
            filtered = [center]

        # Output pyRevit diagnostics
        try:
            from pyrevit import script
            output = script.get_output()
            left_gap = filtered[0] - min_value
            right_gap = max_value - filtered[-1]
            center_offset = min(abs(c - center) for c in filtered)
            output.print_md(
                "#### Centered Joist Layout Diagnostics\n"
                "- **Span Width**: {:.3f} ft\n"
                "- **Requested Spacing**: {:.3f} ft\n"
                "- **Joist Count**: {}\n"
                "- **Left Edge Gap**: {:.3f} ft\n"
                "- **Right Edge Gap**: {:.3f} ft\n"
                "- **Center Offset**: {:.3f} ft".format(
                    span, spacing, len(filtered), left_gap, right_gap, center_offset
                )
            )
        except Exception:
            pass

        return sorted(filtered)

    def _resolve_layout_axis(self, span_x, span_y):
        """Return the joist run axis in host-local coordinates."""
        mode = getattr(self.config, "ceiling_direction_mode", CEILING_DIRECTION_AUTO)
        if mode == CEILING_DIRECTION_X:
            return "x"
        if mode == CEILING_DIRECTION_Y:
            return "y"
        if mode == CEILING_DIRECTION_BOTH:
            return "both"
        return "x" if span_x <= span_y else "y"

    def _member_point(self, ceiling_info, local_x, local_y, family_name, type_name):
        """Return the member centerline point for ceiling framing placement."""
        placement_mode = getattr(
            self.config,
            "ceiling_placement_mode",
            CEILING_PLACEMENT_ABOVE,
        )
        if placement_mode == CEILING_PLACEMENT_CENTER:
            return ceiling_info.point_at(local_x, local_y)

        member_depth = self._resolve_member_depth(family_name, type_name)
        depth_offset = -ceiling_info.target_layer_depth - (member_depth / 2.0)
        return ceiling_info.point_at(local_x, local_y, depth_offset)

    def _resolve_member_depth(self, family_name, type_name):
        """Resolve member depth from the family symbol or nominal size (wood or steel)."""
        depth = self.get_type_depth(family_name, type_name)
        if depth is not None and depth > 0.0:
            return depth

        text = "{0} {1}".format(family_name or "", type_name or "")
        dims = actual_dims_from_text(text)
        if dims is not None:
            return inches_to_feet(dims[1])

        material = getattr(self.config, "framing_material", MATERIAL_WOOD)
        return fallback_depth_ft(material, "stud")

    def _warn_if_steel_joist_dims_unresolved(self):
        """Once per ceiling, warn if steel joist dimensions can't be read.

        Deliberately a single pre-flight check here rather than inside
        _resolve_member_depth (which runs once per joist/rim-joist member --
        dozens of times per ceiling) so the warning doesn't get repeated.
        """
        material = getattr(self.config, "framing_material", MATERIAL_WOOD)
        if material != MATERIAL_STEEL:
            return
        family_name = self.config.stud_family_name
        type_name = self.config.stud_type_name
        depth = self.get_type_depth(family_name, type_name)
        if depth is not None and depth > 0.0:
            return
        text = "{0} {1}".format(family_name or "", type_name or "")
        if actual_dims_from_text(text) is not None:
            return
        warn_unresolved_steel_dims("ceiling joist", family_name, type_name)