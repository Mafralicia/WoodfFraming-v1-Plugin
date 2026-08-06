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
- Steel family/type names in the standard SSMA format (e.g. `350S162-33`, `600T125-54`) are recognized automatically wherever the tools need to infer a nominal size from a name (in addition to wood nominal sizes like `2x6`).
- Spacing options include 12"/16"/24" O.C. on every tool; the steel tools default to 12" O.C. (common for steel), the wood tools default to 16" O.C.
- The steel dialogs relabel a couple of fields to match CFS terminology (e.g. "Plate Type" becomes "Track Type", "Mid Plates" becomes "Mid Blocking"); the underlying fields and engine behavior are otherwise identical to the wood tools.
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
