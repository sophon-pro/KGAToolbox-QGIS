# Transfer Attributes

|  |  |
|---|---|
| **Category** | Modify Features > Attributes |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/transfer.py` (TransferAttributesTool) |
| **Icon** | `kga_tools/editor/modify/icons/transfer_attributes.svg` |

## Overview

Copies attribute values from one feature to another, laid out like the Transfer Attributes pane of ArcGIS Pro.

## How to use

1. Click a **source** feature, then click a **target** feature. A tip beside the cursor names the step and the source.
2. The values go across and the tool is ready for the next source. Right-click or Esc goes back to choosing a source.

## Options

| Option | Meaning |
|---|---|
| **Field list** | Every field with the same name in both layers is copied. Untick a field on the page to leave it out. |

## Notes and limits

* Values are converted to the target field's type. The primary key, unique and calculated fields are never touched.
* A green strip says "Attribute transfer completed." One undoable edit per transfer.
* Needs **Edit Session** On; only editable layers shown on the map are changed.
