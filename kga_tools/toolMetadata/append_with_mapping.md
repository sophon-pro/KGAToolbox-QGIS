# Append with Field Mapping

|  |  |
|---|---|
| **Algorithm ID** | `kga:append_with_mapping` |
| **Group** | KGA Schema Tools |
| **Source** | `kga_tools/algorithms/append_with_mapping.py` |
| **Icon** | `icons/alg_append.png` |
| **Type** | Batch algorithm (runs on the GUI thread — it writes into a project layer) |

## Overview

Appends the features of one layer into another that already exists, mapping the
fields explicitly on the way.

QGIS has no good tool for this. `native:mergevectorlayers` unions schemas naively
and produces a wide junk table when the sources disagree; `native:refactorfields`
maps fields properly but writes a *new* layer instead of appending into one that
already exists. Appending several differently-shaped sources into one master
layer is a routine job, and this is the tool for it.

Each row of the mapping table names a field of the **target** and an expression
evaluated against the **source** feature, so renaming, combining and converting
all happen in one pass. Target fields with no mapping row keep their own
defaults.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Source layer** | Vector layer | — | Where the features come from. |
| **Target layer (appended to in place)** | Vector layer | — | The layer written into. It is modified — there is no output layer. |
| **Field mapping** | Field mapping table, optional | derived from the source | The same widget that powers *Refactor Fields*. Each row is a target field plus an expression over the source feature. |
| **…or a saved mapping file (overrides the table above)** | File (`.json`), optional | empty | A mapping written by **[Generate Field Mapping](generate_field_mapping.md)**. When set, it wins over the table. |
| **Key field for duplicate detection** | Field of *target*, optional | empty | Leave empty to skip duplicate detection entirely. |
| **When the key already exists** | Enum | `Skip the incoming feature` | `Skip the incoming feature`, `Append it anyway`, `Update the existing feature`. |
| **Geometry** | Enum | `Reproject to the target CRS` | `Keep as it is`, `Reproject to the target CRS`, `Reproject and force multipart`, `Drop the geometry`. |
| **Use only the selected features of the source** | Boolean | `False` | Appends just the current selection. |
| **Dry run (report only, write nothing)** | Boolean | **`True`** | On by default. See the notes. |
| **Change report** | File destination (HTML), optional | not created | The full report as a file. Tick it in the Processing dialog to keep one. |

## How to use

1. Load both layers in QGIS.
2. Open **KGA Schema Tools > Append with Field Mapping**.
3. Set **Source layer** and **Target layer**.
4. Fill in the **Field mapping** table, or point **…or a saved mapping file** at
   a JSON written by *Generate Field Mapping*.
5. If the target already holds some of these records, choose a **Key field** and
   decide what happens on a clash.
6. **Leave the dry run on and press Run.** Read the report: features read,
   duplicates found, every value that had to change type, and every target field
   left at its default.
7. Fix whatever the report flags, untick **Dry run**, and run it again for real.

## Outputs

- Features appended into the **target layer in place**. No new layer is created.
- A change report — always pushed to the Processing log, and written to HTML when
  you ask for the *Change report* file. It carries: features read, features
  written, duplicates found and what was done with them, every lossy conversion,
  and every target field left at its default.

## Notes and limits

- **The dry run is on by default, on purpose.** The first run writes nothing and
  tells you what would happen. This is the whole safety model of the tool — do
  not untick it before reading a report.
- **The mapping is checked against the target's real fields before anything is
  written.** A mapping naming a field the target does not have aborts the run.
  That mistake is the most common way an append tool silently loses data.
- **Lossy conversions are reported, not swallowed.** QGIS will happily write a
  value that does not fit and leave a NULL behind. Text that is not a number, a
  number too large for the column, text longer than the column, a date that will
  not parse — each is counted and named in the report.
- **Edit sessions are respected.** If the target is already in edit mode the
  features land in your edit buffer, uncommitted, so your own undo stack still
  owns them. Otherwise the write is committed, and rolled back whole if anything
  fails.
- Pairs with **[Generate Field Mapping](generate_field_mapping.md)**, which
  produces the JSON this tool reads.
