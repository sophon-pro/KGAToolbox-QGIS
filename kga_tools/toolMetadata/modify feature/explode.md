# Explode

|  |  |
|---|---|
| **Category** | Modify Features > Divide |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/explode.py` (ExplodeTool) |
| **Icon** | `kga_tools/editor/modify/icons/explode.svg` |

## Overview

Breaks multi-part features into single parts, or pulls single parts out of them. Works on multi-part features only.

## How to use

1. **Features tab:** select multi-part features and press **Explode**. Each part becomes a feature of its own. The button is enabled only while every selected feature is multi-part (a single-part one is flagged: unselect it).
2. **Parts tab:** hover a part on the map (it is outlined) and click it to make it a feature of its own.

## Options

| Option | Meaning |
|---|---|
| **Keep original features** | Leaves the multi-part features as they are and adds every part as a new feature. |
| **Allow explode without selection** | With nothing selected, click any part of an editable multi-part feature. |

## Notes and limits

* New features copy the attributes; primary key and unique fields get fresh values.
* One undoable edit per layer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.
