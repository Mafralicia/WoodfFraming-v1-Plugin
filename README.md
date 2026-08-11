# swich-wood-framing

`swich-wood-framing` is a pyRevit extension for Autodesk Revit framing workflows, covering both wood dimensional-lumber and cold-formed steel (CFS) framing.

This repository contains one pyRevit extension folder:

```text
WoodFraming.extension/
```

When loaded in Revit, the extension adds a `Wood Framing` tab with a `Wood Framing` panel and `Material Takeoffs` panel.

## Current status

- Work in progress.
- Tested only in Autodesk Revit 2026.
- Not verified in any other Revit version.
- Not perfect. Review the model output before using it for production, estimating, fabrication, coordination, or construction decisions.

## Wood and steel (CFS) framing

Steel (cold-formed steel / CFS) framing has its own **dedicated tools**, separate from the wood tools -- there is no material toggle inside a shared dialog. Each wood tool has a `Steel <Name>` counterpart with its own ribbon button, icon, and dialog:

| Wood tool | Steel counterpart |
| --- | --- |
| `Wall Framing` | `Steel Wall Framing` |
| `Floor Framing` | `Steel Floor Framing` |
| `Ceiling Framing` | `Steel Ceiling Framing` |
| `Single-Slope Roof Framing` | `Steel Roof Framing` |
| `Wall Join Cleanup` | `Steel Wall Join Cleanup` |

- **Family/type selection is unchanged between the wood and steel tools.** Every tool's family and type dropdowns list whatever structural framing/column families are already loaded in your Revit project, so pick and load your own wood or steel stud/track/joist families as usual -- the extension does not ship its own family content. Nothing stops you from picking a steel family in a wood tool or vice versa; the buttons only differ in which material's geometry defaults and fallback sizing they use.
- **The tool you pick affects geometry math and fallback sizing**, not family discovery: stud/track spacing math, plate/track stacking heights, and header/joist depth fallbacks all read the real dimensions off whichever family/type you selected first, and only fall back to material-appropriate defaults (e.g. 1-5/8" CFS stud flange vs. 1-1/2" wood stud thickness) when a family's dimensions can't be read.
- Steel family/type names in the standard SSMA format (e.g. `350S162-33`, `600T125-54`) are recognized automatically wherever the tools need to infer a nominal size from a name (in addition to wood nominal sizes like `2x6`). Dimension lookup also works purely off the family's real `d`/`b` (or `Profundidade`/`Largura`) type parameters, so metric families authored in millimeters -- e.g. Brazilian LSF profiles from Perfilor, Metsa, Eberle, etc. -- size and place correctly without any name matching or manufacturer-specific recognition.
- Spacing options include 12"/16"/24" O.C. on every tool, plus **400mm and 600mm** presets (the standard on-center spacings for Brazilian Light Steel Framing under NBR 15253 / NBR 15980) on every steel tool; the steel tools default to 12" O.C., the wood tools default to 16" O.C.
- **Every steel tool has a "Material (Revit):" dropdown** listing the Materials already loaded in the project. Picking one assigns it to the `Structural Material` parameter of every placed member's family type -- so schedules, renders, and takeoffs reflect a real steel Material asset, not just steel-sized geometry. It defaults to "(Don't change)" (the family's existing Material is left alone), but if a loaded Material's name contains a steel-suggestive term (`steel`, `galvanized`, `cfs`, `aço`, `galvanizado`, `metálico`, etc., case-insensitive) it's pre-selected automatically. Since `Structural Material` is a type parameter, it's applied once per unique family type per run, not once per instance.
- The steel dialogs relabel a couple of fields to match CFS terminology (e.g. "Plate Type" becomes "Track Type", "Mid Plates" becomes "Mid Blocking"); the underlying fields and engine behavior are otherwise identical to the wood tools.

### Brazilian (ABNT/NBR) profiles and mass take-off

- **ABNT profile designations are decoded natively.** The NBR 6355 / NBR 15253 forms are recognized wherever a profile has to be inferred from a family/type name, in millimeters and with the Brazilian decimal comma:
  - `Ue <web> x <flange> x <lip> x <thickness>` — montante / lipped channel, e.g. `Ue 90x40x12x0,95`
  - `U <web> x <flange> x <thickness>` — guia / track, e.g. `U 90x40x0,95`
  - `PGC <web>` / `PGU <web>` — commercial naming; the flange (40 mm) and lip (12 mm) are supplied as documented market-standard assumptions, and the thickness is left **unknown** because the name does not carry it.
- **The BOM reports mass, not just length.** Steel framing in Brazil is specified, bought and priced by weight, so `Generate BOM` now emits `Profile`, `Thickness (mm)`, `Mass (kg/m)` and `Total Mass (kg)` alongside count and length, grouped by profile and totalled. Mass is derived from the flat developed width of the section (`web + 2·flange + 2·lip` for `Ue`, `web + 2·flange` for `U`) × base steel thickness × 7850 kg/m³. The result is validated in `tests/` against published Brazilian LSF profile tables and agrees to within 1%.
- **No weight is ever invented.** When the profile can't be decoded, isn't steel, or carries no thickness in its name (`PGC 90`), the mass columns are left blank rather than filled with a guess — a blank prompts you to check the family, a fabricated number silently corrupts the take-off.
- **Mass columns are unitless `number` parameters with the unit named in the heading.** This is deliberate: a unit-typed parameter would make Revit convert between internal and display units, and a silent conversion error is exactly the failure a quantity take-off cannot absorb. What is written is what is shown.
- **NBR 15253 minimum thickness is checked.** Every steel tool warns before generating framing if a selected profile's base steel thickness falls below the standard's structural minimum. A thickness that cannot be determined is not reported — an unknown value is not evidence of a violation.
- Thickness throughout is the **base steel thickness** (espessura da chapa base, excluding the zinc coating), which is the basis both NBR 6355 designations and published kg/m tables use.

### Brazilian Wood Frame sections and volume take-off

- **Metric lumber sections are decoded natively.** Brazilian Wood Frame names sections by their *actual* milled size in millimeters — `38x90`, `38x140`, `38x190`, `38x240`, `45x90`, `45x140` — so unlike the North American `2x4` convention there is no nominal-to-actual translation. Sizes outside the standard ladder still resolve; the guards only reject implausible sections.
- **The BOM reports volume in m³ for timber**, because lumber in Brazil is bought and priced by the cubic meter exactly as steel is by the kilogram. Volume is taken from the member's real cross-section (read off the family type's `b`/`d`, or `Largura`/`Altura`, parameters — falling back to decoding the type name) times its length.
- **Each member carries the quantity it is actually purchased by**: a recognized steel profile gets mass and leaves volume blank; everything else is treated as sawn timber and gets volume. Volume is written only for solid rectangular sections, since for a thin-walled steel profile the same multiplication would describe the bounding box rather than any real quantity.
- **Steel designations are never mistaken for lumber.** A name like `Ue 90x40x12x0,95` contains a digit pair that would otherwise read as a 90×40 mm timber, so the steel notations are always resolved first; imperial section names (`C12x20.7`, `HSS2X2X1/4`) are rejected by dimensional guards.
- Each tool remembers its own last-used settings independently (separate per-tool config files), so switching between a wood and a steel tool never clobbers the other's saved settings.

## Disclaimer

Use this repository and pyRevit extension at your own risk. The author and contributors accept no responsibility for model changes, data loss, incorrect quantities, incorrect framing, project delays, construction issues, or any other direct or indirect damages from using this work.

## Active tools

These are the active pyRevit button titles currently defined by the enabled `*.pushbutton` bundles in this repo:

| Tool name in Revit | Bundle folder |
| --- | --- |
| `Wall Framing` | `Wall Framing.pushbutton` |
| `Steel Wall Framing` | `Steel Wall Framing.pushbutton` |
| `Wall Join Cleanup` | `Wall Join Cleanup.pushbutton` |
| `Steel Wall Join Cleanup` | `Steel Wall Join Cleanup.pushbutton` |
| `Floor Framing` | `Frame Floor.pushbutton` |
| `Steel Floor Framing` | `Steel Floor Framing.pushbutton` |
| `Ceiling Framing` | `Frame Ceiling.pushbutton` |
| `Steel Ceiling Framing` | `Steel Ceiling Framing.pushbutton` |
| `Single-Slope Roof Framing` | `Frame Roof.pushbutton` |
| `Steel Roof Framing` | `Steel Roof Framing.pushbutton` |
| `Multi-Slope Roof` | `Frame Multi-Slope Roof.pushbutton` |
| `Split Sheathing` | `Split Sheathing.pushbutton` |
| `Number Members` | `Number Members.pushbutton` |
| `Material List` | `Generate BOM.pushbutton` |

Disabled or legacy folders may exist in the repo, but they are not listed above as active tools.

## Prerequisites

- Autodesk Revit 2026.
- pyRevit installed and attached to Revit 2026.
- Revit family/content setup that matches the framing workflow used by the tools.

## Installation option 1: install into the pyRevit extensions folder

Use this option when you want pyRevit to load the extension from its default user extensions location.

1. Close Revit.
2. Open this folder in Windows Explorer:

   ```text
   %APPDATA%\pyRevit\Extensions
   ```

   Create the `Extensions` folder if it does not exist.

3. Copy the `WoodFraming.extension` folder from this repo into that folder.
4. Confirm the final structure looks like this:

   ```text
   %APPDATA%\pyRevit\Extensions\
     WoodFraming.extension\
       extension.json
       lib\
       Wood Framing.tab\
   ```

5. Open Revit 2026.
6. Reload pyRevit or restart Revit if the `Wood Framing` tab does not appear.

## Installation option 2: connect this repo through pyRevit settings

Use this option when you want to keep the repo in place and have pyRevit load it from this working folder.

1. Keep the repo folder in a stable location.
2. In Revit, open pyRevit settings.
3. Add the parent folder that contains `WoodFraming.extension` to the custom extension directories list.

   To complete this checkout, utilize the file explorer to identify and choose the directory where the folder was previously saved.

   Do not add the `WoodFraming.extension` folder itself. Add the folder that contains it.

4. Save the settings.
5. Reload pyRevit or restart Revit.
6. Look for the `Wood Framing` tab and `Wood Framing` pulldown.

## Repository layout

```text
WoodFraming.extension/
  extension.json
  lib/
  Wood Framing.tab/
```

- `extension.json` contains the pyRevit extension metadata.
- `lib/` contains shared Python modules for framing, geometry, family handling, schedules, tracking, host analysis, floors, ceilings, roofs, and wall framing.
- `Wood Framing.tab/` contains the pyRevit ribbon structure and active tool buttons.
- `tests/` contains automated tests for the parts of `lib/` that have no Revit API dependency (currently `wf_materials.py`, the wood/steel dimension-resolution logic). These run with a plain CPython interpreter -- no Revit, pyRevit, or IronPython needed:

  ```text
  python -m unittest discover -s tests -v
  ```

  Most of `lib/` does depend on the Revit API and can only be exercised by actually running the tools inside Revit -- this suite is a narrow, fast regression net for the one module that doesn't.

## pyRevit references checked

- pyRevit official API docs define the default third-party extension location as `%APPDATA%\pyRevit\Extensions`: <https://docs.pyrevitlabs.io/reference/pyrevit/>
- pyRevit official user configuration docs describe user extension root directories: <https://docs.pyrevitlabs.io/reference/pyrevit/userconfig/>
- pyRevit extension docs identify `.extension`, `.tab`, `.panel`, `.pulldown`, and `.pushbutton` bundle naming: <https://docs.pyrevitlabs.io/reference/pyrevit/extensions/>
- pyRevit manual extension guidance says to add the directory containing the extension folder: <https://pyrevitlabs.notion.site/Install-Extensions-0753ab78c0ce46149f962acc50892491>
