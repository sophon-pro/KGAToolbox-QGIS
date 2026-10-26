# Copy Paste

|  |  |
|---|---|
| **Category** | Modify Features > Construct |
| **Type** | Map tool in the Modify Features pane (not a Processing algorithm) |
| **Source** | `kga_tools/editor/modify/copypaste.py` (CopyPasteTool) |
| **Icon** | `kga_tools/icons/copyPaste.png` |
| **Also as a window** | [copy_paste_feature](../copy_paste_feature.md) (`kga:copy_paste_feature`) |

## Overview

The Copy-Paste Feature tool inside the pane. It shares its clipboard and paste engine with the Copy-Paste Feature window, so what is copied here can be pasted there and the other way round.

## How to use

1. **Copy tab:** select features (select mode stays on), choose the **Layer** if features of several layers are selected, then press **Copy**. The copied features are outlined on the map.
2. **Paste tab:** set the Paste Special fields (target layer, how attributes carry across, geometry options), then press **Paste**, or **Paste again** for the same target and mapping.

## Options

| Option | Meaning |
|---|---|
| **Clear clipboard** | Empties the clipboard. |
| **Paste Special fields** | Target layer, how attributes carry across, and geometry options. |

## Notes and limits

* A blue bar tells what is on the clipboard; a green bar reports what was pasted.
* Needs **Edit Session** On; only editable layers shown on the map are changed.
