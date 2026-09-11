# Apply Domain Library

|  |  |
|---|---|
| **Algorithm ID** | `kga:apply_domain_library` |
| **Group** | KGA Schema Tools |
| **Source** | `kga_tools/algorithms/domain_apply.py` |
| **Icon** | `icons/alg_domain_apply.png` |
| **Type** | Batch algorithm (runs on the GUI thread — it touches layers that may be open) |

## Overview

Imports a domain library — the JSON file the
**[Domain & Schema Manager](domain_manager.md)** exports — into a GeoPackage, and
attaches its domains to the fields that should carry them.

A domain is the drop-down list behind a field: *status* can only be Planned, Under
Construction or Complete. Building that list once and pushing it into every
GeoPackage on a project is what this tool is for. Unlike the dialog, it is a plain
parameter-driven algorithm, so it works in a model, in batch mode and from
`processing.run()` — the dialog is for exploring, this is for repeating.

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| **Domain library (JSON)** | File (`.json`) | — | The library to import, as exported by the Domain & Schema Manager. |
| **Target GeoPackage** | File (`.gpkg`) | — | The container the domains are written into. |
| **Mode** | Enum | `Merge — add and update, keep the rest` | `Merge` adds and updates domains and leaves anything the library does not mention alone. `Replace` also deletes those, so the target ends up matching the library exactly. |
| **Attach each domain to every field with a matching name** | Boolean | `True` | Gives every field of the same name the same domain. The same column recurs across layers — status, owner, material — so this does the bulk of the work. |
| **Dry run (report only, write nothing)** | Boolean | **`True`** | On by default. Read the report before turning it off. |
| **Change report** | File destination (HTML), optional | not created | The report as a file. |

## How to use

1. Build or export a domain library with the
   **[Domain & Schema Manager](domain_manager.md)**.
2. Open **KGA Schema Tools > Apply Domain Library**.
3. Point **Domain library** at the JSON and **Target GeoPackage** at the
   container to update.
4. Choose the **Mode**. Start with `Merge`; `Replace` deletes domains the library
   does not mention.
5. **Leave the dry run on and press Run.** The report lists every domain that
   would be added, updated or deleted, and every field it would be attached to.
6. Untick **Dry run** and run again to write the changes.

## Outputs

- The domains written into the target GeoPackage, and attached to the matching
  fields.
- A change report — in the Processing log, and as HTML when you ask for the file
  — naming every domain added, updated or deleted and every field attachment.

## Notes and limits

- **The dry run is on by default.** The first run writes nothing.
- **`Replace` deletes.** It makes the target match the library exactly, so a
  domain that exists only in the target is removed. Use `Merge` unless you
  specifically want that.
- **Only GeoPackage targets.** The parameter accepts `.gpkg` only.
- Close the target in ArcGIS or any other application first; a container held
  open elsewhere cannot be modified.
- Use **[Validate Against Domains](validate_against_domains.md)** afterwards:
  attaching a domain constrains *new* edits, but existing data is never
  re-checked, so old values that break the new rule stay in place until you go
  looking for them.
