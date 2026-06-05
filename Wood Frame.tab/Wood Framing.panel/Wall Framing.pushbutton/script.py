# -*- coding: utf-8 -*-
"""Wall Framing command."""

import json
import os
import sys

from pyrevit import DB, forms, revit, script
from pyrevit.forms import WPFWindow
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType


_ext_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
while _ext_dir and not _ext_dir.lower().endswith(".extension"):
    _parent = os.path.dirname(_ext_dir)
    if _parent == _ext_dir:
        break
    _ext_dir = _parent
_lib_dir = os.path.join(_ext_dir, "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

# Force reload wood framing library modules to ensure changes are picked up without restarting Revit
for m in list(sys.modules.keys()):
    if m.startswith("wf_"):
        del sys.modules[m]

from wf_config import (
    FramingConfig,
    SPACING_16OC,
    SPACING_24OC,
    WALL_BASE_MODE_SUPPORT_TOP,
    WALL_BASE_MODE_WALL,
)
from wf_families import (
    get_available_types_flat,
    get_column_types_flat,
    parse_family_type_label,
)
from wf_tracking import get_tracking_data
from wf_wall_framing_v4 import ENGINE_NAME, WallCavityFramingV4Engine
from wf_wall_topology import WallTopologyGraph
from wf_wall_validation import FramingValidator


logger = script.get_logger()
output = script.get_output()
COMMAND_TITLE = "Wall Framing"
_XAML = os.path.join(os.path.dirname(__file__), "FrameWall20Config.xaml")
_CFG_PATH = os.path.join(
    os.environ.get("APPDATA", ""),
    "pyRevit",
    "WoodFraming_Wall20LastConfig.json",
)


class WallFraming20Dialog(WPFWindow):
    def __init__(self, column_types, framing_types, wall_count):
        WPFWindow.__init__(self, _XAML)
        self.result_config = None
        self._column_types = column_types
        self._framing_types = framing_types

        self.cb_stud_type.ItemsSource = column_types
        self.cb_plate_type.ItemsSource = framing_types
        self.cb_header_type.ItemsSource = framing_types

        if column_types:
            self.cb_stud_type.SelectedIndex = 0
        if framing_types:
            self.cb_plate_type.SelectedIndex = 0
            self.cb_header_type.SelectedIndex = 0

        self.tb_summary.Text = (
            "Frame {0} selected wall(s)."
            .format(wall_count)
        )

        self.rb_custom.Checked += self._on_custom_checked
        self.rb_custom.Unchecked += self._on_custom_unchecked
        self.rb_16oc.Checked += self._on_custom_unchecked
        self.rb_24oc.Checked += self._on_custom_unchecked
        self.chk_mid_plates.Checked += self._on_mid_plates_checked
        self.chk_mid_plates.Unchecked += self._on_mid_plates_unchecked

        self._restore_last()

    def _on_custom_checked(self, sender, args):
        self.tb_custom_spacing.IsEnabled = True

    def _on_custom_unchecked(self, sender, args):
        self.tb_custom_spacing.IsEnabled = False

    def _on_mid_plates_checked(self, sender, args):
        self.tb_mid_plate_height.IsEnabled = True

    def _on_mid_plates_unchecked(self, sender, args):
        self.tb_mid_plate_height.IsEnabled = False

    def btn_ok_click(self, sender, args):
        config = self._build_config()
        if config is None:
            return
        self.result_config = config
        self._save_last()
        self.Close()

    def btn_cancel_click(self, sender, args):
        self.result_config = None
        self.Close()

    def _build_config(self):
        config = FramingConfig()

        if self.rb_24oc.IsChecked:
            config.stud_spacing = SPACING_24OC
        elif self.rb_custom.IsChecked:
            try:
                config.stud_spacing = float(self.tb_custom_spacing.Text)
            except Exception:
                forms.alert("Invalid custom stud spacing.", title=COMMAND_TITLE)
                return None
        else:
            config.stud_spacing = SPACING_16OC

        stud_sel = self.cb_stud_type.SelectedItem
        if not stud_sel:
            forms.alert("Select a stud column family.", title=COMMAND_TITLE)
            return None
        config.stud_family_name, config.stud_type_name = parse_family_type_label(
            str(stud_sel)
        )

        if self.cb_plate_type.SelectedItem:
            family, type_name = parse_family_type_label(str(self.cb_plate_type.SelectedItem))
            config.bottom_plate_family_name = family
            config.bottom_plate_type_name = type_name
            config.top_plate_family_name = family
            config.top_plate_type_name = type_name

        if self.cb_header_type.SelectedItem:
            family, type_name = parse_family_type_label(str(self.cb_header_type.SelectedItem))
            config.header_family_name = family
            config.header_type_name = type_name

        config.top_plate_count = 1 if self.rb_single_top.IsChecked else 2
        config.include_mid_plates = bool(self.chk_mid_plates.IsChecked)
        try:
            config.mid_plate_interval_ft = float(self.tb_mid_plate_height.Text)
        except Exception:
            forms.alert("Invalid mid-plate interval.", title=COMMAND_TITLE)
            return None
        config.include_king_studs = bool(self.chk_king_studs.IsChecked)
        config.include_jack_studs = bool(self.chk_jack_studs.IsChecked)
        config.include_cripple_studs = bool(self.chk_cripple_studs.IsChecked)
        config.wall_base_mode = (
            WALL_BASE_MODE_SUPPORT_TOP
            if bool(self.chk_support_top.IsChecked)
            else WALL_BASE_MODE_WALL
        )
        config.clean_existing_wall_members = bool(self.chk_clean_existing.IsChecked)
        config.track_members = True
        return config

    def _save_last(self):
        data = {
            "stud": str(self.cb_stud_type.SelectedItem or ""),
            "plate": str(self.cb_plate_type.SelectedItem or ""),
            "header": str(self.cb_header_type.SelectedItem or ""),
            "spacing_16": bool(self.rb_16oc.IsChecked),
            "spacing_24": bool(self.rb_24oc.IsChecked),
            "spacing_custom": bool(self.rb_custom.IsChecked),
            "custom_spacing": self.tb_custom_spacing.Text,
            "single_top": bool(self.rb_single_top.IsChecked),
            "mid_plates": bool(self.chk_mid_plates.IsChecked),
            "mid_interval": self.tb_mid_plate_height.Text,
            "king": bool(self.chk_king_studs.IsChecked),
            "jack": bool(self.chk_jack_studs.IsChecked),
            "cripple": bool(self.chk_cripple_studs.IsChecked),
            "support_top": bool(self.chk_support_top.IsChecked),
            "clean_existing": bool(self.chk_clean_existing.IsChecked),
        }
        try:
            cfg_dir = os.path.dirname(_CFG_PATH)
            if not os.path.exists(cfg_dir):
                os.makedirs(cfg_dir)
            with open(_CFG_PATH, "w") as cfg_file:
                json.dump(data, cfg_file, indent=2)
        except Exception:
            pass

    def _restore_last(self):
        try:
            if not os.path.exists(_CFG_PATH):
                return
            with open(_CFG_PATH, "r") as cfg_file:
                data = json.load(cfg_file)
        except Exception:
            return

        self._select_if_present(self.cb_stud_type, data.get("stud"))
        self._select_if_present(self.cb_plate_type, data.get("plate"))
        self._select_if_present(self.cb_header_type, data.get("header"))

        if data.get("spacing_24"):
            self.rb_24oc.IsChecked = True
        elif data.get("spacing_custom"):
            self.rb_custom.IsChecked = True
            self.tb_custom_spacing.Text = str(data.get("custom_spacing", "16"))
        else:
            self.rb_16oc.IsChecked = True

        self.rb_single_top.IsChecked = bool(data.get("single_top", False))
        self.rb_double_top.IsChecked = not self.rb_single_top.IsChecked
        self.chk_mid_plates.IsChecked = bool(data.get("mid_plates", True))
        self.tb_mid_plate_height.Text = str(data.get("mid_interval", "8"))
        self.tb_mid_plate_height.IsEnabled = bool(self.chk_mid_plates.IsChecked)
        self.chk_king_studs.IsChecked = bool(data.get("king", True))
        self.chk_jack_studs.IsChecked = bool(data.get("jack", True))
        self.chk_cripple_studs.IsChecked = bool(data.get("cripple", True))
        self.chk_support_top.IsChecked = bool(data.get("support_top", False))
        self.chk_clean_existing.IsChecked = True

    @staticmethod
    def _select_if_present(combo, value):
        if not value:
            return
        try:
            for item in combo.Items:
                if str(item) == str(value):
                    combo.SelectedItem = item
                    return
        except Exception:
            pass


class _WallFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, DB.Wall)

    def AllowReference(self, reference, point):
        return False


class _WallSupportFilter(ISelectionFilter):
    def AllowElement(self, element):
        if isinstance(element, (DB.Floor, DB.RoofBase)):
            return True
        category = getattr(element, "Category", None)
        if category is None:
            return False
        try:
            return int(category.Id.IntegerValue) == int(DB.BuiltInCategory.OST_StructuralFoundation)
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return False


def _pick_support(doc):
    try:
        ref = revit.uidoc.Selection.PickObject(
            ObjectType.Element,
            _WallSupportFilter(),
            "Pick floor, roof, or foundation for wall base",
        )
    except Exception:
        return None
    if ref is None:
        return None
    return doc.GetElement(ref.ElementId)


def _support_top_elevation(element):
    if element is None:
        return None
    try:
        bbox = element.get_BoundingBox(None)
        if bbox is not None:
            return bbox.Max.Z
    except Exception:
        pass
    return None


def _check_walls_intersect_geometrically(wall_a, wall_b, tol=0.5):
    """Check if two walls intersect or join geometrically using XY projection and Z-overlap."""
    loc_a = getattr(wall_a, "Location", None)
    loc_b = getattr(wall_b, "Location", None)
    if not loc_a or not loc_b:
        return False
    if not hasattr(loc_a, "Curve") or not hasattr(loc_b, "Curve"):
        return False
        
    curve_a = loc_a.Curve
    curve_b = loc_b.Curve
    if curve_a is None or curve_b is None:
        return False
        
    try:
        pa0 = curve_a.GetEndPoint(0)
        pa1 = curve_a.GetEndPoint(1)
        pb0 = curve_b.GetEndPoint(0)
        pb1 = curve_b.GetEndPoint(1)
    except Exception:
        return False
        
    # Directions in 2D
    dir_a = (pa1 - pa0).Normalize()
    dir_b = (pb1 - pb0).Normalize()
    
    # Are they parallel?
    try:
        dot = abs(dir_a.DotProduct(dir_b))
    except Exception:
        return False
        
    if dot >= 0.999: # Parallel within ~2 degrees
        # Check distance between endpoints
        for pt_a in (pa0, pa1):
            for pt_b in (pb0, pb1):
                dx = pt_a.X - pt_b.X
                dy = pt_a.Y - pt_b.Y
                dist_xy = (dx*dx + dy*dy) ** 0.5
                if dist_xy <= tol:
                    # Check Z overlap
                    z_min_a = min(pa0.Z, pa1.Z)
                    z_max_a = max(pa0.Z, pa1.Z)
                    z_min_b = min(pb0.Z, pb1.Z)
                    z_max_b = max(pb0.Z, pb1.Z)
                    if max(z_min_a, z_min_b) - min(z_max_a, z_max_b) < 2.0:
                        return True
        return False
        
    # Intersection in XY projection
    denom = dir_a.X * dir_b.Y - dir_a.Y * dir_b.X
    if abs(denom) < 1e-9:
        return False
        
    dx = pb0.X - pa0.X
    dy = pb0.Y - pa0.Y
    t_a = (dx * dir_b.Y - dy * dir_b.X) / denom
    
    pt_intersect = DB.XYZ(pa0.X + dir_a.X * t_a, pa0.Y + dir_a.Y * t_a, pa0.Z + dir_a.Z * t_a)
    
    # Check if pt_intersect lies within segment boundaries (with tolerance)
    len_a = curve_a.Length
    len_b = curve_b.Length
    
    d_a = (pt_intersect - pa0).DotProduct(dir_a)
    d_b = (pt_intersect - pb0).DotProduct(dir_b)
    
    if -tol <= d_a <= len_a + tol and -tol <= d_b <= len_b + tol:
        # Check vertical/Z overlap (within 8 feet)
        z_min_a = min(pa0.Z, pa1.Z)
        z_max_a = max(pa0.Z, pa1.Z)
        z_min_b = min(pb0.Z, pb1.Z)
        z_max_b = max(pb0.Z, pb1.Z)
        
        # Check overlap
        overlap = max(0.0, min(z_max_a, z_max_b) - max(z_min_a, z_min_b))
        if overlap > 0.0 or abs(max(z_min_a, z_min_b) - min(z_max_a, z_max_b)) < 8.0:
            return True
            
    return False


def _delete_existing_wall_members(doc, walls, include_legacy):
    selected_wall_ids = set()
    joined_wall_ids = set()
    for wall in walls or []:
        wall_id = _element_id_text(getattr(wall, "Id", None))
        if wall_id:
            selected_wall_ids.add(wall_id)
    if not selected_wall_ids:
        return {"wall_v4": 0, "wall_v2": 0, "wall": 0, "wall_join": 0}

    # 1. Quick check using Revit join endpoints
    for wall in walls or []:
        loc_curve = wall.Location
        if loc_curve and hasattr(loc_curve, "Curve"):
            for end_idx in (0, 1):
                joined_elems = loc_curve.get_ElementsAtJoin(end_idx)
                if joined_elems:
                    for elem_id in joined_elems:
                        eid_str = _element_id_text(elem_id)
                        if eid_str and eid_str not in selected_wall_ids:
                            joined_wall_ids.add(eid_str)

    # 2. Geometric intersection check for unselected walls
    all_db_walls = list(DB.FilteredElementCollector(doc).OfClass(DB.Wall).WhereElementIsNotElementType())
    for db_wall in all_db_walls:
        db_wall_id = _element_id_text(db_wall.Id)
        if db_wall_id in selected_wall_ids or db_wall_id in joined_wall_ids:
            continue
        for sel_wall in walls:
            if _check_walls_intersect_geometrically(sel_wall, db_wall, tol=0.5):
                joined_wall_ids.add(db_wall_id)
                break

    allowed_kinds = set(["wall_v4", "wall_v2", "wall_join"])
    if include_legacy:
        allowed_kinds.add("wall")

    delete_items = []
    for category in (
        DB.BuiltInCategory.OST_StructuralFraming,
        DB.BuiltInCategory.OST_StructuralColumns,
    ):
        try:
            collector = (
                DB.FilteredElementCollector(doc)
                .OfCategory(category)
                .WhereElementIsNotElementType()
            )
        except Exception:
            continue
        for element in collector:
            tracking = get_tracking_data(element)
            if not tracking:
                continue
            kind = tracking.get("kind")
            if kind not in allowed_kinds:
                continue
            
            host_id = tracking.get("host")
            if host_id in selected_wall_ids:
                delete_items.append((element.Id, kind))

    deleted = {"wall_v4": 0, "wall_v2": 0, "wall": 0, "wall_join": 0}
    for element_id, kind in delete_items:
        try:
            doc.Delete(element_id)
            deleted[kind] = deleted.get(kind, 0) + 1
        except Exception:
            pass
    return deleted


def _audit_line(audit, placed_count):
    wall_id = audit.get("wall_id", "?")
    source_side = audit.get("source_side") or ""
    target_offset = float(audit.get("target_offset_from_face") or 0.0)
    first_rejection = audit.get("first_rejection") or ""
    if first_rejection:
        first_rejection = str(first_rejection).replace("|", "/").replace("\n", " ")[:80]
    return (
        "| {0} | {1:.3f} | {2:.3f} | {3} | {4} | {5} | {6} | {7} | {8} | {9} | {10} | {11} | {12:.3f} | {13} |\n"
        .format(
            wall_id,
            float(audit.get("location_length") or 0.0),
            float(audit.get("face_length") or 0.0),
            audit.get("wall_solid_count", 0),
            audit.get("face_loop_count", 0),
            audit.get("merged_opening_count", 0),
            audit.get("perimeter_segment_count", 0),
            audit.get("candidate_count", 0),
            audit.get("validated_count", 0),
            audit.get("rejected_count", 0),
            placed_count,
            source_side,
            target_offset,
            first_rejection,
        )
    )


def _audit_text(value, limit):
    text = str(value or "")
    text = text.replace("|", "/").replace("\n", " ")
    if len(text) > limit:
        return text[:max(0, limit - 3)] + "..."
    return text


def _side_stud_attempt_summary(audit):
    attempts = audit.get("side_stud_attempts") or []
    parts = []
    for attempt in attempts[:6]:
        try:
            raw_d = float(attempt.get("raw_d") or 0.0)
            stud_d = float(attempt.get("stud_d") or 0.0)
        except Exception:
            raw_d = 0.0
            stud_d = 0.0
        result = attempt.get("result") or "?"
        label = "{0:.3f}->{1:.3f} {2}".format(raw_d, stud_d, result)
        reason = attempt.get("reason") or ""
        if reason:
            label = "{0} ({1})".format(label, reason)
        parts.append(label)
    if len(attempts) > 6:
        parts.append("+{0} more".format(len(attempts) - 6))
    return _audit_text("; ".join(parts), 120)


def _side_stud_audit_line(audit):
    wall_id = audit.get("wall_id", "?")
    return (
        "| {0} | {1} | {2} | {3} | {4} | {5} |\n"
        .format(
            wall_id,
            audit.get("side_segment_count", 0),
            audit.get("side_stud_attempted_count", 0),
            audit.get("side_stud_placed_count", 0),
            audit.get("side_stud_rejected_count", 0),
            _side_stud_attempt_summary(audit),
        )
    )


def _element_id_text(element_id):
    if element_id is None:
        return None
    value = getattr(element_id, "IntegerValue", getattr(element_id, "Value", None))
    if value is None:
        return None
    return str(value)


def main():
    doc = revit.doc

    column_types = get_column_types_flat(doc)
    framing_types = get_available_types_flat(doc)
    if not column_types:
        forms.alert("No Structural Column families loaded.", title=COMMAND_TITLE)
        return
    if not framing_types:
        forms.alert("No Structural Framing families loaded.", title=COMMAND_TITLE)
        return

    selected = revit.get_selection().elements
    walls = [element for element in selected if isinstance(element, DB.Wall)]
    if not walls:
        try:
            refs = revit.uidoc.Selection.PickObjects(
                ObjectType.Element,
                _WallFilter(),
                "Select walls for wall framing",
            )
            walls = [doc.GetElement(ref.ElementId) for ref in refs]
        except Exception:
            return
    if not walls:
        return

    dialog = WallFraming20Dialog(column_types, framing_types, len(walls))
    dialog.ShowDialog()
    config = dialog.result_config
    if config is None:
        return

    if config.wall_base_mode == WALL_BASE_MODE_SUPPORT_TOP:
        support = _pick_support(doc)
        if support is None:
            forms.alert("Wall base selection was cancelled.", title=COMMAND_TITLE)
            return
        config.wall_base_override_z = _support_top_elevation(support)

    engine = WallCavityFramingV4Engine(doc, config)
    wall_count = 0
    member_count = 0
    skipped = 0
    deleted_counts = {"wall_v4": 0, "wall_v2": 0, "wall": 0, "wall_join": 0}
    audit_rows = []
    topology_summary = ""

    with revit.Transaction("WF: Wall Framing"):
        deleted_counts = _delete_existing_wall_members(
            doc,
            walls,
            bool(getattr(config, "clean_existing_wall_members", True)),
        )

        # ----------------------------------------------------------------
        # Build the topology graph for all selected walls and their unselected joins.
        # ----------------------------------------------------------------
        # Collect all walls in the document to find joins/intersections
        all_db_walls = list(DB.FilteredElementCollector(doc).OfClass(DB.Wall).WhereElementIsNotElementType())
        all_db_walls_by_id = {str(_element_id_text(w.Id)): w for w in all_db_walls}
        selected_wall_ids = {_element_id_text(w.Id) for w in walls}
        context_wall_ids = set()

        # 1. Quick check using Revit join endpoints
        for wall in walls:
            loc_curve = wall.Location
            if loc_curve and hasattr(loc_curve, "Curve"):
                for end_idx in (0, 1):
                    joined_elems = loc_curve.get_ElementsAtJoin(end_idx)
                    if joined_elems:
                        for elem_id in joined_elems:
                            eid_str = _element_id_text(elem_id)
                            if eid_str and eid_str not in selected_wall_ids:
                                context_wall_ids.add(eid_str)

        # 2. Geometric intersection check for unselected walls
        for db_wall in all_db_walls:
            db_wall_id = _element_id_text(db_wall.Id)
            if db_wall_id in selected_wall_ids or db_wall_id in context_wall_ids:
                continue
            for sel_wall in walls:
                if _check_walls_intersect_geometrically(sel_wall, db_wall, tol=0.5):
                    context_wall_ids.add(db_wall_id)
                    break

        context_walls = [all_db_walls_by_id[wid] for wid in context_wall_ids if wid in all_db_walls_by_id]

        hosts = []
        host_map = {}  # wall_id_text -> (members, host)
        for wall in walls:
            _members, host = engine.calculate_members(wall)  # analyse geometry
            if host is not None:
                host.is_selected = True
                hosts.append(host)
                host_map[_element_id_text(getattr(host, "element_id", None))] = ([], host)

        for wall in context_walls:
            _members, host = engine.calculate_members(wall)  # analyse geometry
            if host is not None:
                host.is_selected = False
                hosts.append(host)

        graph = None
        if len(hosts) > 1:
            try:
                graph = WallTopologyGraph(hosts)
                graph.build()
                graph.determine_ownership()
                graph.compute_layout(config.stud_spacing_ft)
                topology_summary = graph.audit_summary()
            except Exception as exc:
                topology_summary = "Topology graph error: {0}".format(exc)
                graph = None

        # ----------------------------------------------------------------
        # Generate all framing members (topology-aware when graph is set).
        # ----------------------------------------------------------------
        wall_members_map = {
            _element_id_text(wall.Id): [] for wall in walls
        }
        host_info_map = {}     # wall_id_text -> host
        skipped_walls = []

        for wall in walls:
            members, host = engine.calculate_members(wall, graph=graph)
            if host is None:
                skipped += 1
                skipped_walls.append(getattr(wall, "Id", None))
                continue
            wid = _element_id_text(getattr(host, "element_id", None))
            host_info_map[wid] = host
            for m in members:
                if m is None:
                    continue
                m_wid = _element_id_text(getattr(m, "host_id", None))
                if m_wid in wall_members_map:
                    wall_members_map[m_wid].append(m)
                elif m_wid is None:
                    # Fallback for members missing host_id (should not happen, but safe)
                    wall_members_map[wid].append(m)
                # If m_wid is set but not in wall_members_map, it belongs to an unselected
                # context wall, so we discard/skip it on this run.

        # ----------------------------------------------------------------
        # Pass 1 – Analytical validation (before placement).
        # ----------------------------------------------------------------
        validator = FramingValidator(graph, wall_members_map, host_info_map)
        pass1_ok = validator.run_pass1()

        # ----------------------------------------------------------------
        # Place members in Revit.
        # ----------------------------------------------------------------
        placed_instances_map = {}
        for wid, members in wall_members_map.items():
            if wid not in host_info_map:
                continue
            host = host_info_map[wid]
            placed = engine.place_members(members, host)
            wall_count += 1
            member_count += len(placed)
            placed_instances_map[wid] = placed
            audit_rows.append((getattr(host, "audit", {}), len(placed)))

        # ----------------------------------------------------------------
        # Pass 2 – Placement validation (after placement).
        # ----------------------------------------------------------------
        validator.run_pass2(placed_instances_map)

    validation_text = validator.report.summary_text() if 'validator' in dir() else ""

    audit_table = (
        "\n### Source Geometry Audit\n"
        "| Wall Id | Location Len | Face Len | Solids | Face Loops | Openings | Perimeter Segs | Candidates | Validated | Rejected | Placed | Source Side | Target Offset | First Rejection |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- |\n"
    )
    for audit, placed_count in audit_rows:
        audit_table += _audit_line(audit, placed_count)

    side_stud_table = (
        "\n### Side Stud Audit\n"
        "| Wall Id | Side Segs | Attempted | Placed | Rejected | Attempts |\n"
        "| --- | ---: | ---: | ---: | ---: | --- |\n"
    )
    for audit, _placed_count in audit_rows:
        side_stud_table += _side_stud_audit_line(audit)

    deleted_total = (
        deleted_counts.get("wall_v4", 0)
        + deleted_counts.get("wall_v2", 0)
        + deleted_counts.get("wall", 0)
        + deleted_counts.get("wall_join", 0)
    )

    output.print_md(
        "## Wall Framing Complete\n"
        "- **Engine:** {0}\n"
        "- **Walls framed:** {1}\n"
        "- **Walls skipped:** {2}\n"
        "- **Previous members replaced:** {3}\n"
        "- **Members placed:** {4}\n"
        "{5}"
        "{6}"
        "{7}"
        "{8}".format(
            ENGINE_NAME,
            wall_count,
            skipped,
            deleted_total,
            member_count,
            ("\n### Topology Graph\n```\n" + topology_summary + "\n```\n") if topology_summary else "",
            ("\n" + validation_text + "\n") if validation_text else "",
            audit_table,
            side_stud_table,
        )
    )


if __name__ == "__main__":
    main()
