# Generate Field Mapping

|  |  |
|---|---|
| **Algorithm ID** | `kga:generate_field_mapping` |
| **Group** | KGA Schema Tools |
| **Source** | `kga_tools/algorithms/append_with_mapping.py` |
| **Icon** | `icons/alg_mapping.png` |
| **Type** | Batch algorithm |

## Overview

Works out which source field belongs in which target field, and writes the result
as a JSON mapping file.

The file is in exactly the format **[Append with Field Mapping](append_with_mapping.md)**
takes, so a mapping between two schemas can be saved once, kept in version
control, reviewed in a diff and reused. That reuse is the part that actually
saves time when the same two schemas come round every month.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Source layer** | Vector layer | — | The layer whose fields are being mapped from. |
| **Target layer** | Vector layer | — | The layer whose fields are being mapped to. |
| **Match field names by** | Enum | `Fuzzy (ignore case, spaces and underscores)` | `Exact name`, `Case-insensitive name`, `Fuzzy (ignore case, spaces and underscores)`. Fuzzy lowercases names and strips spaces and underscores, so `Parcel_ID` finds `parcelid`. |
| **Field mapping** | File destination (`*.json`) | — | Where the mapping is written. |
| **Change report** | File destination (HTML), optional | not created | The match report as a file. |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Number of matched fields** | Number | How many source→target pairs were found. |

## How to use

1. Load both layers in QGIS.
2. Open **KGA Schema Tools > Generate Field Mapping**.
3. Set **Source layer** and **Target layer**, and pick a matching strategy —
   start with the Fuzzy default.
4. Choose where the **Field mapping** JSON goes. Somewhere you keep, not a temp
   folder: the point is to reuse it.
5. Press **Run** and read the report. The unmatched fields on either side are the
   ones to fix by hand.
6. Edit the JSON if needed, then feed it to
   **[Append with Field Mapping](append_with_mapping.md)** through its
   *…or a saved mapping file* parameter.

## Outputs

- A JSON mapping file, ready for *Append with Field Mapping*.
- A report — in the Processing log, and as HTML when you ask for the file —
  listing the matched pairs, the unmatched source fields and the unmatched target
  fields.
- The **Number of matched fields** count in the Processing results panel.

## Notes and limits

- **Each target field is claimed at most once**, so two source fields can never
  collapse onto one column.
- **Nothing is written to either layer.** This tool only inspects schemas.
- Unmatched fields are not an error — they are the output you care about. A field
  that appears in the unmatched lists needs a human decision, which is why the
  JSON is meant to be edited before use.
- The mapping matches **names**, not types. A pair whose types do not fit shows up
  later, as a lossy conversion in the append tool's report.
