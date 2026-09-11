# Validate Against Domains

|  |  |
|---|---|
| **Algorithm ID** | `kga:validate_against_domains` |
| **Group** | KGA Schema Tools |
| **Source** | `kga_tools/algorithms/domain_apply.py` |
| **Icon** | `icons/alg_validate.png` |
| **Type** | Batch algorithm |

## Overview

Checks every feature against the field domains its layer declares, and lists the
values that break them.

A domain constrains new edits — but data that predates the domain, or that
arrived through an import, is never re-checked. Attaching a domain does not clean
up what is already there. This tool finds that data.

Run it after **[Apply Domain Library](apply_domain_library.md)**, after any bulk
import, and before handing a dataset on.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Input layer(s)** | Multiple vector layers | — | The layers to check. Layers that cannot carry domains are skipped with a note. |
| **Report empty values as violations** | Boolean | `False` | Off, a NULL or empty value passes. On, every empty value in a domain-constrained field is listed too. |
| **Violations** | Feature sink | temporary layer | The violations table: one row per bad value. |
| **Violation locations** | Feature sink (point), optional | not created | A point layer of the offending features, so you can zoom to them on the map. |
| **Change report** | File destination (HTML), optional | not created | The report as a file. |

### Outputs declared

| Output | Type | Description |
|---|---|---|
| **Number of violations** | Number | Total bad values found across all layers. |

## How to use

1. Load the layers to check. They must be stored in a GeoPackage or File
   Geodatabase — nothing else can carry domains.
2. Open **KGA Schema Tools > Validate Against Domains**.
3. Pick the layers under **Input layer(s)**.
4. Tick **Violation locations** if you want to zoom to the offending features;
   this is usually the fastest way to fix them.
5. Press **Run**.
6. Open the **Violations** table. Each row names the layer, feature, field,
   value, domain and reason.
7. Fix the values, then run it again until the count is zero.

## Outputs

- A **Violations** table, one row per bad value: layer, feature, field, value,
  domain and reason.
- Optionally a **Violation locations** point layer for map navigation.
- A report — in the log, and as HTML when asked for — summarising per layer.
- The **Number of violations** count in the Processing results panel.

## Notes and limits

- **Only layers stored in a GeoPackage or File Geodatabase can carry domains.**
  Shapefiles, GeoJSON, memory layers and the rest are skipped with a note in the
  log, not an error — check the note before concluding a layer is clean.
- **A clean run means the values match the domains, nothing more.** Fields with
  no domain attached are not checked at all; use the
  **[Domain & Schema Manager](domain_manager.md)** to see which fields actually
  carry one.
- **Nothing is modified.** This tool only reports.
- Empty values pass by default. Tick *Report empty values as violations* when a
  blank is as wrong as a bad code — which it usually is for a status field.
