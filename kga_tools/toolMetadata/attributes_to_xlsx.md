# Export Attributes to Excel

|  |  |
|---|---|
| **Algorithm ID** | `kga:attributes_to_xlsx` |
| **Group** | KGA Data Conversion |
| **Source** | `kga_tools/algorithms/attributes_xlsx.py` (engine in `kga_tools/core/xlsx_io.py`) |
| **Icon** | `icons/toExcel.png` |
| **Type** | Batch algorithm |

## Overview

Writes a layer's attributes to an Excel workbook a colleague can edit without
QGIS — in a shape **[Import Attributes from Excel](xlsx_to_attributes.md)** can
bring back safely.

This is the outbound half of the attribute round trip. The workbook is not a
dump: it carries a hidden sheet recording which layer it came from, drop-downs
for every field with a value map, and a locked key column, so what comes back is
already valid and already matched.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input layer** | Vector layer | — | The layer to export. |
| **Key field (identifies each row)** | Field of *Input layer*, optional | empty | The column that identifies a record on the way back. Choose something stable and unique. |
| **Fields to export (all if left empty)** | Field of *Input layer*, multiple, optional | empty (all) | |
| **Selected features only** | Boolean | `False` | |
| **Turn value maps into Excel dropdowns** | Boolean | `True` | Any field with a value map — from a field domain or set by hand — becomes a drop-down in the workbook, so the data comes back already valid. |
| **Open the workbook when it is written** | Boolean | `True` | |
| **Workbook** | File destination (`*.xlsx`) | — | |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Rows written** | Number | |

## How to use

1. Open **KGA Data Conversion > Export Attributes to Excel**.
2. Pick the **Input layer** and a **Key field** — this is the single most
   important choice, because it is what matches the edited rows back.
3. Narrow **Fields to export** to what the colleague actually has to fill in.
   A shorter sheet comes back with fewer accidents.
4. Press **Run**. The workbook opens straight away.
5. Send it, get it back, and apply it with
   **[Import Attributes from Excel](xlsx_to_attributes.md)** — which already has
   this file and this layer filled in.

## Outputs

- An `.xlsx` workbook with:
  - a **data** sheet — the key column first, then the fields you chose;
  - a hidden **`_kga_meta`** sheet recording which layer it came from, so the
    import can tell whether a sheet belongs to the layer it is applied to;
  - drop-downs on value-mapped columns;
  - the key column **shaded and locked**.
- The **Rows written** count.
- The file and layer remembered for the import tool.

## Notes and limits

- **The formatting touches need the `openpyxl` library.** Without it the workbook
  is written plain — no drop-downs, no locked key — and the round trip still
  works. The log says which happened.
- **An edited key is a row that matches nothing on the way back.** That is why
  the key column is locked; do not unlock it.
- Nothing is written back to the layer. This half is read-only.
- Excel's habits — turning the text code `0042` into the number 42, turning a
  date into the serial 45231 — are handled on the way **in**, by the import tool,
  not prevented here.
