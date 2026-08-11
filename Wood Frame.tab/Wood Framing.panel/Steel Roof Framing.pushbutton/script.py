# -*- coding: utf-8 -*-
"""Steel Roof Framing - cold-formed steel (CFS) variant of Frame Roof."""

import os
import sys
import json

_ext_dir = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(__file__)
)))
while _ext_dir and not _ext_dir.lower().endswith(".extension"):
    _parent = os.path.dirname(_ext_dir)
    if _parent == _ext_dir:
        break
    _ext_dir = _parent
_lib_dir = os.path.join(_ext_dir, "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

# Force reload of project modules to ensure latest changes are used
for mod in list(sys.modules.keys()):
    if mod.startswith("wf_") or mod == "wf_roof":
        del sys.modules[mod]

from pyrevit import revit, DB, script, forms
from pyrevit.forms import WPFWindow
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter

from wf_config import FramingConfig, SPACING_12OC, SPACING_16OC, SPACING_24OC
from wf_families import get_available_types_flat, parse_family_type_label
from wf_materials import (
    MATERIAL_STEEL,
    SPACING_400MM,
    SPACING_600MM,
    config_profile_labels,
    guess_steel_material_id,
    list_materials,
    mm_to_spacing_in,
    warn_nbr15253_compliance,
)
from wf_roof import RoofFramingEngine
from wf_tracking import delete_tracked_members_for_hosts


from wf_detail_spec import HOST_ROOF, describe_markdown
output = script.get_output()
_XAML = os.path.join(os.path.dirname(__file__), "FrameRoofConfig.xaml")
_CFG_PATH = os.path.join(
    os.environ.get("APPDATA", ""),
    "pyRevit", "WoodFraming_SteelRoofLastConfig.json",
)
_NO_MATERIAL_CHANGE = "(Don't change)"


class _RoofFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return isinstance(elem, DB.RoofBase)

    def AllowReference(self, ref, pt):
        return False


class FrameRoofDialog(WPFWindow):
    def __init__(self, doc):
        WPFWindow.__init__(self, _XAML)
        self.doc = doc
        self.result = None

        self._framing_labels = get_available_types_flat(doc)
        self.cb_tc_type.ItemsSource = self._framing_labels
        self.cb_bc_type.ItemsSource = self._framing_labels
        self.cb_web_type.ItemsSource = self._framing_labels
        self.cb_ridge_type.ItemsSource = self._framing_labels

        if self._framing_labels:
            self.cb_tc_type.SelectedIndex = 0
            self.cb_bc_type.SelectedIndex = min(1, len(self._framing_labels)-1) if len(self._framing_labels) > 1 else 0
            self.cb_web_type.SelectedIndex = min(2, len(self._framing_labels)-1) if len(self._framing_labels) > 2 else 0
            self.cb_ridge_type.SelectedIndex = 0

        self._material_pairs = list_materials(doc)
        material_labels = [_NO_MATERIAL_CHANGE] + [
            name for name, _id in self._material_pairs
        ]
        self.cb_revit_material.ItemsSource = material_labels
        self.cb_revit_material.SelectedItem = _NO_MATERIAL_CHANGE
        guessed_id = guess_steel_material_id(doc)
        if guessed_id is not None:
            for name, material_id in self._material_pairs:
                if material_id == guessed_id:
                    self.cb_revit_material.SelectedItem = name
                    break

        # Style changed events
        self.rb_style_truss.Checked += self._on_style_changed
        self.rb_style_stick.Checked += self._on_style_changed

        # Custom spacing events
        self.rb_truss_custom.Checked += self._on_truss_custom_checked
        self.rb_truss_custom.Unchecked += self._on_truss_custom_unchecked
        for rb in (self.rb_truss_12oc, self.rb_truss_16oc, self.rb_truss_24oc,
                   self.rb_truss_400mm, self.rb_truss_600mm):
            rb.Checked += self._on_truss_custom_unchecked

        self.rb_ceil_custom.Checked += self._on_ceil_custom_checked
        self.rb_ceil_custom.Unchecked += self._on_ceil_custom_unchecked
        for rb in (self.rb_ceil_12oc, self.rb_ceil_16oc, self.rb_ceil_24oc,
                   self.rb_ceil_400mm, self.rb_ceil_600mm):
            rb.Checked += self._on_ceil_custom_unchecked

        self.btn_ok.Click += self._on_ok
        self.btn_cancel.Click += self._on_cancel

        self._restore_last()
        self._on_style_changed(None, None)

    def _on_style_changed(self, sender, args):
        is_truss = self.rb_style_truss.IsChecked
        if hasattr(self, "gb_truss_options") and self.gb_truss_options:
            self.gb_truss_options.IsEnabled = is_truss
        if hasattr(self, "sp_ceiling_spacing") and self.sp_ceiling_spacing:
            self.sp_ceiling_spacing.IsEnabled = is_truss
        if hasattr(self, "cb_bc_type") and self.cb_bc_type:
            self.cb_bc_type.IsEnabled = is_truss
        if hasattr(self, "cb_web_type") and self.cb_web_type:
            self.cb_web_type.IsEnabled = is_truss
        if hasattr(self, "chk_include_kickers") and self.chk_include_kickers:
            self.chk_include_kickers.IsEnabled = not is_truss
        if hasattr(self, "chk_include_king_posts") and self.chk_include_king_posts:
            self.chk_include_king_posts.IsEnabled = not is_truss

    def _on_truss_custom_checked(self, sender, args):
        self.tb_truss_custom.IsEnabled = True

    def _on_truss_custom_unchecked(self, sender, args):
        self.tb_truss_custom.IsEnabled = False

    def _on_ceil_custom_checked(self, sender, args):
        self.tb_ceil_custom.IsEnabled = True

    def _on_ceil_custom_unchecked(self, sender, args):
        self.tb_ceil_custom.IsEnabled = False

    def _on_ok(self, sender, args):
        style = "truss" if self.rb_style_truss.IsChecked else "stick"

        # Truss spacing
        if self.rb_truss_12oc.IsChecked:
            truss_sp = 12.0
        elif self.rb_truss_16oc.IsChecked:
            truss_sp = 16.0
        elif self.rb_truss_24oc.IsChecked:
            truss_sp = 24.0
        elif self.rb_truss_400mm.IsChecked:
            truss_sp = mm_to_spacing_in(SPACING_400MM)
        elif self.rb_truss_600mm.IsChecked:
            truss_sp = mm_to_spacing_in(SPACING_600MM)
        else:
            try:
                truss_sp = float(self.tb_truss_custom.Text)
            except ValueError:
                truss_sp = 24.0

        # Ceiling spacing
        if self.rb_ceil_12oc.IsChecked:
            ceil_sp = 12.0
        elif self.rb_ceil_16oc.IsChecked:
            ceil_sp = 16.0
        elif self.rb_ceil_24oc.IsChecked:
            ceil_sp = 24.0
        elif self.rb_ceil_400mm.IsChecked:
            ceil_sp = mm_to_spacing_in(SPACING_400MM)
        elif self.rb_ceil_600mm.IsChecked:
            ceil_sp = mm_to_spacing_in(SPACING_600MM)
        else:
            try:
                ceil_sp = float(self.tb_ceil_custom.Text)
            except ValueError:
                ceil_sp = 16.0

        # Truss topology
        truss_idx = self.cb_truss_type.SelectedIndex
        truss_types_map = {0: "KingPost", 1: "Fink", 2: "Pratt", 3: "Dynamic"}
        selected_truss_type = truss_types_map.get(truss_idx, "Dynamic")

        try:
            web_sp = float(self.tb_web_spacing.Text)
        except ValueError:
            web_sp = 48.0

        tc_sel = self.cb_tc_type.SelectedItem
        bc_sel = self.cb_bc_type.SelectedItem
        web_sel = self.cb_web_type.SelectedItem
        ridge_sel = self.cb_ridge_type.SelectedItem

        family_tc = parse_family_type_label(str(tc_sel)) if tc_sel else ("", "")
        family_bc = parse_family_type_label(str(bc_sel)) if bc_sel else ("", "")
        family_web = parse_family_type_label(str(web_sel)) if web_sel else ("", "")
        family_ridge = parse_family_type_label(str(ridge_sel)) if ridge_sel else ("", "")

        cfg = FramingConfig()
        cfg.framing_material = MATERIAL_STEEL
        cfg.stud_spacing = truss_sp
        cfg.stud_family_name = family_tc[0]
        cfg.stud_type_name = family_tc[1]
        cfg.header_family_name = family_ridge[0]
        cfg.header_type_name = family_ridge[1]

        # Inject additional parameters used by the engine
        cfg.truss_spacing = truss_sp
        cfg.ceiling_spacing = ceil_sp
        cfg.truss_type = selected_truss_type
        cfg.web_spacing = web_sp
        cfg.include_roof_kickers = self.chk_include_kickers.IsChecked
        cfg.include_king_posts = self.chk_include_king_posts.IsChecked
        cfg.family_top_chords = family_tc
        cfg.family_bottom_chords = family_bc
        cfg.family_web_bracing = family_web
        cfg.family_hips_ridges = family_ridge

        material_sel = self.cb_revit_material.SelectedItem
        if material_sel and str(material_sel) != _NO_MATERIAL_CHANGE:
            for name, material_id in self._material_pairs:
                if name == str(material_sel):
                    cfg.target_material_id = material_id
                    break

        self.result = {
            "config": cfg,
            "mode": style
        }
        self._save_last(cfg, style)
        self.Close()

    def _on_cancel(self, sender, args):
        self.result = None
        self.Close()

    def _save_last(self, cfg, style):
        try:
            data = cfg.to_dict()
            data["_mode"] = style
            data["_truss_type_idx"] = self.cb_truss_type.SelectedIndex
            data["_web_spacing"] = self.tb_web_spacing.Text
            data["_include_kickers"] = self.chk_include_kickers.IsChecked
            data["_include_king_posts"] = self.chk_include_king_posts.IsChecked
            data["_tc_label"] = str(self.cb_tc_type.SelectedItem or "")
            data["_bc_label"] = str(self.cb_bc_type.SelectedItem or "")
            data["_web_label"] = str(self.cb_web_type.SelectedItem or "")
            data["_ridge_label"] = str(self.cb_ridge_type.SelectedItem or "")
            data["_material"] = str(
                self.cb_revit_material.SelectedItem or _NO_MATERIAL_CHANGE
            )

            if self.rb_truss_12oc.IsChecked:
                data["_truss_sp_state"] = "12"
            elif self.rb_truss_16oc.IsChecked:
                data["_truss_sp_state"] = "16"
            elif self.rb_truss_24oc.IsChecked:
                data["_truss_sp_state"] = "24"
            elif self.rb_truss_400mm.IsChecked:
                data["_truss_sp_state"] = "400mm"
            elif self.rb_truss_600mm.IsChecked:
                data["_truss_sp_state"] = "600mm"
            else:
                data["_truss_sp_state"] = "custom"
            data["_truss_custom_text"] = self.tb_truss_custom.Text

            if self.rb_ceil_12oc.IsChecked:
                data["_ceil_sp_state"] = "12"
            elif self.rb_ceil_16oc.IsChecked:
                data["_ceil_sp_state"] = "16"
            elif self.rb_ceil_24oc.IsChecked:
                data["_ceil_sp_state"] = "24"
            elif self.rb_ceil_400mm.IsChecked:
                data["_ceil_sp_state"] = "400mm"
            elif self.rb_ceil_600mm.IsChecked:
                data["_ceil_sp_state"] = "600mm"
            else:
                data["_ceil_sp_state"] = "custom"
            data["_ceil_custom_text"] = self.tb_ceil_custom.Text

            directory = os.path.dirname(_CFG_PATH)
            if not os.path.exists(directory):
                os.makedirs(directory)
            with open(_CFG_PATH, "w") as stream:
                json.dump(data, stream, indent=2)
        except Exception:
            pass

    def _restore_last(self):
        try:
            if not os.path.exists(_CFG_PATH):
                return
            with open(_CFG_PATH, "r") as stream:
                data = json.load(stream)

            mode = data.get("_mode", "truss")
            if mode == "truss":
                self.rb_style_truss.IsChecked = True
            else:
                self.rb_style_stick.IsChecked = True

            truss_type_idx = data.get("_truss_type_idx", 3)
            self.cb_truss_type.SelectedIndex = truss_type_idx
            self.tb_web_spacing.Text = data.get("_web_spacing", "48")
            self.chk_include_kickers.IsChecked = data.get("_include_kickers", False)
            self.chk_include_king_posts.IsChecked = data.get("_include_king_posts", False)

            tc_label = data.get("_tc_label", "")
            if tc_label and tc_label in self._framing_labels:
                self.cb_tc_type.SelectedItem = tc_label
            bc_label = data.get("_bc_label", "")
            if bc_label and bc_label in self._framing_labels:
                self.cb_bc_type.SelectedItem = bc_label
            web_label = data.get("_web_label", "")
            if web_label and web_label in self._framing_labels:
                self.cb_web_type.SelectedItem = web_label
            ridge_label = data.get("_ridge_label", "")
            if ridge_label and ridge_label in self._framing_labels:
                self.cb_ridge_type.SelectedItem = ridge_label

            material_label = data.get("_material", "")
            if material_label:
                for name, _id in self._material_pairs:
                    if name == material_label:
                        self.cb_revit_material.SelectedItem = name
                        break

            truss_sp_state = data.get("_truss_sp_state", "24")
            if truss_sp_state == "12":
                self.rb_truss_12oc.IsChecked = True
            elif truss_sp_state == "16":
                self.rb_truss_16oc.IsChecked = True
            elif truss_sp_state == "24":
                self.rb_truss_24oc.IsChecked = True
            elif truss_sp_state == "400mm":
                self.rb_truss_400mm.IsChecked = True
            elif truss_sp_state == "600mm":
                self.rb_truss_600mm.IsChecked = True
            else:
                self.rb_truss_custom.IsChecked = True
                self.tb_truss_custom.Text = data.get("_truss_custom_text", "24")

            ceil_sp_state = data.get("_ceil_sp_state", "16")
            if ceil_sp_state == "12":
                self.rb_ceil_12oc.IsChecked = True
            elif ceil_sp_state == "16":
                self.rb_ceil_16oc.IsChecked = True
            elif ceil_sp_state == "24":
                self.rb_ceil_24oc.IsChecked = True
            elif ceil_sp_state == "400mm":
                self.rb_ceil_400mm.IsChecked = True
            elif ceil_sp_state == "600mm":
                self.rb_ceil_600mm.IsChecked = True
            else:
                self.rb_ceil_custom.IsChecked = True
                self.tb_ceil_custom.Text = data.get("_ceil_custom_text", "16")
        except Exception:
            pass


def main():
    doc = revit.doc

    framing_types = get_available_types_flat(doc)
    if not framing_types:
        forms.alert(
            "No structural framing families are loaded.\n"
            "Load a framing family before running this command.",
            title="Steel Framing",
        )
        return

    selected = revit.get_selection().elements
    roofs = [element for element in selected if isinstance(element, DB.RoofBase)]

    if not roofs:
        try:
            refs = revit.uidoc.Selection.PickObjects(
                ObjectType.Element,
                _RoofFilter(),
                "Select roofs to frame",
            )
            roofs = [doc.GetElement(ref.ElementId) for ref in refs]
        except Exception:
            return

    if not roofs:
        forms.alert("No roofs selected.", title="Steel Framing")
        return

    dialog = FrameRoofDialog(doc)
    dialog.ShowDialog()
    if dialog.result is None:
        return

    config = dialog.result["config"]
    mode = dialog.result["mode"]

    # NBR 15253: flag any selected profile thinner than the
    # standard's structural minimum before generating framing.
    warn_nbr15253_compliance(config_profile_labels(config))

    # Record exactly which members this run will draw, and what it will
    # not, so the output is a self-contained account of the framing.
    output.print_md(
        "### Construction details\n" + describe_markdown(HOST_ROOF, config, mode=mode)
    )

    engine = RoofFramingEngine(doc, config)

    total_placed = 0
    total_calculated = 0
    total_roofs = 0
    skipped_roofs = 0
    deleted_existing = 0
    errors = []

    with revit.Transaction("WF: Steel Roof Framing"):
        deleted_existing = delete_tracked_members_for_hosts(
            doc,
            roofs,
            ("roof",),
        )
        for roof in roofs:
            try:
                members, roof_info = engine.calculate_members(roof, mode=mode)
            except Exception as calc_err:
                errors.append(
                    "Roof {0} calc error: {1}".format(roof.Id.Value, calc_err)
                )
                continue

            if roof_info is None:
                errors.append(
                    "Roof {0}: analyze_roof_host returned None".format(roof.Id.Value)
                )
                continue

            # Bypass single-slope check since upgraded truss engine supports complex roof geometries.
            pass

            total_calculated += len(members)

            for plane_index, plane in enumerate(roof_info.planes):
                output.print_md(
                    "  - Plane {0}: normal=({1:.3f},{2:.3f},{3:.3f}), "
                    "bounds=({4:.1f},{5:.1f},{6:.1f},{7:.1f}), loops={8}".format(
                        plane_index,
                        plane.normal.X, plane.normal.Y, plane.normal.Z,
                        plane.bounds[0], plane.bounds[1],
                        plane.bounds[2], plane.bounds[3],
                        len(plane.boundary_loops_local),
                    )
                )

            try:
                placed = engine.place_members(members, roof_info)
            except Exception as place_err:
                errors.append(
                    "Roof {0} place error: {1}".format(roof.Id.Value, place_err)
                )
                placed = []

            total_placed += len(placed)
            total_roofs += 1


    # Members that could not be placed are quantities the take-off will
    # under-report, so a run never ends silently short.
    _placement = getattr(engine, "run_report", None)
    if _placement is not None:
        _placement_md = _placement.markdown()
        if _placement_md:
            output.print_md("\n" + _placement_md)

    output.print_md(
        "## Steel Roof Framing Complete\n"
        "- **Roofs framed:** {0}\n"
        "- **Roofs skipped:** {1}\n"
        "- **Previous members replaced:** {2}\n"
        "- **Members calculated:** {3}\n"
        "- **Members placed:** {4}".format(
            total_roofs,
            skipped_roofs,
            deleted_existing,
            total_calculated,
            total_placed,
        )
    )

    if errors:
        output.print_md("\n### Errors")
        for line in errors:
            output.print_md("- " + str(line))

    if total_calculated > 0 and total_placed == 0:
        output.print_md(
            "\n> **Warning:** Members were calculated but none could be "
            "placed. Check that the selected family types exist in the "
            "project and that the roof has a valid level assignment."
        )
    elif total_calculated == 0 and total_roofs > 0:
        output.print_md(
            "\n> **Warning:** No framing members were generated. "
            "Check the plane diagnostics above for geometry details."
        )
    elif skipped_roofs and total_roofs == 0:
        output.print_md(
            "\n> **Note:** Multi-slope roofs are intentionally blocked while "
            "the roof framing workflow stays focused on the single-slope tool."
        )


if __name__ == "__main__":
    main()
