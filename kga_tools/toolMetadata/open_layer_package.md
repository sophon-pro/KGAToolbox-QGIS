# Open Layer Package

|  |  |
|---|---|
| **Algorithm ID** | `kga:open_layer_package` |
| **Group** | KGA Data Management |
| **Source** | `kga_tools/algorithms/layer_package.py` |
| **Icon** | `icons/alg_package_in.png` |
| **Type** | Batch algorithm (runs on the GUI thread — it adds layers to the project) |

## Overview

Unpacks a `.kgalp` written by **[Create Layer Package](create_layer_package.md)**
and adds its layers, styles and group structure to the current project.

The package is extracted to a real folder on disk, never to a temporary one,
because the layers point at the extracted files — a temp folder would leave a
project full of broken layers after the next restart. SVG and image paths inside
each style are rewritten to the extracted copies, so the symbology renders the
same as it did on the machine that built the package.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Layer package** | File (`.kgalp`) | — | The package to open. |
| **Extract to** | Folder destination | not created | Where the contents are unpacked. Leave it empty and a `KGA Packages/<package name>` folder is created beside the project file (or in your home folder if the project has never been saved); the log says where everything went. |
| **Add the layers to the project** | Boolean | `True` | Untick to extract only, without touching the Layers panel. |
| **Group name (the package name if empty)** | String | empty | The Layers-panel group the layers are placed in. Defaults to the package file name. |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Extraction folder** | String | The folder the package was unpacked into. |
| **Layers added** | Number | How many layers reached the project. `0` when *Add the layers to the project* is off. |

## How to use

1. Open **KGA Data Management > Open Layer Package**.
2. Point **Layer package** at the `.kgalp` you received.
3. Choose an **Extract to** folder somewhere permanent — beside the project is
   the usual choice. Skipping this is fine; the default is also permanent.
4. Press **Run**.
5. The layers appear in a group named after the package, styled, with the group
   structure of the original project rebuilt.
6. Read the log for warnings carried over from the packaging machine — a missing
   font is the common one.

## Outputs

- The extracted package on disk: a GeoPackage of vector layers, raster files,
  QML styles, and the collected SVG/marker resources.
- The layers in the project, inside one group, with their styles applied and
  their symbol paths repointed at the extracted resources.

## Notes and limits

- **Do not extract to a temporary folder.** The layers reference the extracted
  files; if the folder disappears the project breaks. This is why the tool
  never uses a temp folder even when you leave the parameter empty.
- **Fonts still have to be installed by hand.** The package carries a warning
  naming any font marker it could not include; it is repeated in the log here.
- A package whose writing was interrupted has no manifest and is refused with a
  clear error, rather than opening half-loaded.
- Archive entries that would be written outside the extraction folder are
  refused — a package cannot write elsewhere on your disk.
- Layers listed in the manifest but missing from the archive are warned about and
  skipped; anything not mentioned by the recorded group tree is still hung off
  the group directly, so no layer is silently dropped.
