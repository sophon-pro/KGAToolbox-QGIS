# Import Attributes from Excel

|  |  |
|---|---|
| **Algorithm ID** | `kga:xlsx_to_attributes` |
| **Group** | KGA Data Conversion |
| **Source** | `kga_tools/algorithms/attributes_xlsx.py` (engine in `kga_tools/core/xlsx_io.py`) |
| **Icon** | `icons/fromExcel.png` |
| **Type** | Batch algorithm (runs on the GUI thread — it writes into a project layer) |

## Overview

Applies a workbook of edited attributes back onto the layer they came from,
matching rows on the key column.

**Reports first, writes second.** The report gives a per-field count and a table
of changed values, old next to new, so the change can be reviewed before anything
is committed — and it runs as a dry run by default.

The workbook and the layer are **already filled in** from your last export, so
the normal round trip — export, edit in Excel, save, come back — needs no
browsing. Both survive a QGIS restart, and clear themselves if the file is moved
or deleted.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Workbook** | File (`.xlsx`) | **the last exported workbook** | The edited sheet. |
| **Target layer** | Vector layer | **the layer that export came from** | The layer written to. |
| **Key field (taken from the workbook if left empty)** | Field of *Target layer*, optional | empty | Normally leave it: the workbook records which column is the key. |
| **Fields to apply (all the workbook holds if left empty)** | Field of *Target layer*, multiple, optional | empty (all) | Narrow it to apply only some columns. |
| **Rows whose key is not in the layer** | Enum | `Report them` | `Ignore them`, `Report them`, `Fail if there are any`. |
| **Read bare numbers in date fields as Excel day serials** | Boolean | `True` | Decodes Excel's `45231` back into a date. |
| **Dry run (report only, write nothing)** | Boolean | **`True`** | On by default. Turn it off deliberately. |
| **Change report** | File destination (HTML), optional | not created | The report as a file. In a dry run, the report *is* the deliverable. |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Values changed** | Number | |

## How to use

1. Save and close the workbook in Excel.
2. Make sure the **target layer has no unsaved edits** — commit or roll back
   first.
3. Open **KGA Data Conversion > Import Attributes from Excel**. The workbook and
   layer are already filled in.
4. **Leave the dry run on and press Run.**
5. Read the report: per-field counts, and every changed value old next to new.
   This is where you catch a colleague who sorted the sheet, retyped a code, or
   pasted a column one row out.
6. Untick **Dry run** and run again to commit.

## Outputs

- The changed values written onto the target layer — only when the dry run is
  off.
- A change report with a per-field count and a table of old-versus-new values,
  in the Processing log and as HTML when asked for.
- The **Values changed** count.

## Notes and limits

- **The dry run is on by default, and the report is the point.** Do not untick it
  before reading one.
- **The target must have no unsaved edits.** The run is refused otherwise.
- **Excel's habits are handled, not absorbed.** A text code turned into a number
  comes back as text; a date turned into the serial `45231` is decoded when
  *Excel date serials* is on; a value that will not fit the field is **reported**
  rather than landing as a NULL.
- **A workbook recording a different layer gives a warning, not a refusal** —
  applying a sheet to a copy is legitimate. But read the report before turning
  off the dry run.
- Rows whose key is not in the layer are reported by default; set the enum to
  *Fail if there are any* when the sheet is supposed to match exactly.
- The outbound half is
  **[Export Attributes to Excel](attributes_to_xlsx.md)**; the two are pinned
  together in the Data Conversion menu.
