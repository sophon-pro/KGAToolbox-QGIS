# Domain & Schema Manager

|  |  |
|---|---|
| **Algorithm ID** | `kga:domain_manager` |
| **Group** | KGA Schema Tools |
| **Source** | `kga_tools/algorithms/domain_manager_alg.py` (dialog in `kga_tools/gui/domain_manager.py`) |
| **Icon** | `icons/domain.png` |
| **Type** | Interactive dialog — modeless, needs the QGIS window |

## Overview

Create, edit and reuse **field domains** — the database-level lists of allowed
values that drive the value-map widget in the attribute form.

QGIS can create a domain from the Browser panel, but not edit one afterwards,
not attach one to many fields at once, and not move a set of domains between
containers. This tool does all three, in three tabs: **Domains** (define them),
**Assignment** (attach them), **Library** (move them).

The tool is promoted onto the KGA toolbar as its own button. Opening it while a
GeoPackage-backed layer is active loads that GeoPackage straight away.

## Parameters

The algorithm takes no Processing parameters — running it opens the window. The
controls below are the window's.

### Container (above the tabs)

| Control | Type | Default | Description |
|---|---|---|---|
| **GeoPackage / Geodatabase** | Path box, drop target | the active layer's `.gpkg`, if any | The container being worked on. Type a path, use **Browse…**, or drag a `.gpkg` here from the Browser panel. |
| **Browse…** / **Reload** | Buttons | — | *Reload* re-reads the container from disk. |
| **Copy container…** | Button (in the warning strip) | — | Saves a copy of the **whole** GeoPackage — every layer and every domain together. Appears when it matters; see the notes. |

### Tab 1 — Domains

| Control | Type | Default | Description |
|---|---|---|---|
| Domain list | List | — | Every domain in the container. |
| **New** / **Duplicate** / **Delete** | Buttons | — | Define a new domain, copy the selected one under a new name, or remove it. |
| **Name** | Text | — | The domain's name, as stored in the container. |
| **Description** | Text | empty | Free text. |
| **Domain type** | Combo box | `Coded values` | `Coded values`, `Range`, `Glob pattern`. |
| **Field type** | Combo box | — | The attribute type the domain constrains. |
| **Used by** | Label | — | A live count of the fields currently using this domain. |
| *Coded values* table | Table (`Code`, `Label`) | — | With **Add**, **Remove**, **Move up**, **Move down** and **Paste list…** — the last one takes a list off the clipboard, which is how most domains actually get built. |
| *Range* box | Spin boxes and checkboxes | both bounds on, both inclusive | **Has a minimum** / **Has a maximum** with their values, and **Minimum is included** / **Maximum is included**. |
| *Glob pattern* box | Text | empty | A wildcard pattern such as `K-*`. |
| **Revert** / **Save domain** | Buttons | — | Discard the edits in the form, or write the domain to the container. |

### Tab 2 — Assignment

| Control | Type | Default | Description |
|---|---|---|---|
| **Filter** | Text box | empty | Narrows the grid by layer or field name. |
| Assignment grid | Table (`Layer`, `Field`, `Domain`) | current state | Every layer and field in the container, with the domain each carries. Change the *Domain* cell to attach or detach one. |
| **Apply to all matching field names** | Button | — | Gives every field with the same name as the selected row the same domain. The same column recurs across layers — status, owner, material — so this is usually what you want. |
| **Revert** / **Apply assignments** | Buttons | — | Throw away the pending changes, or write them. |

### Tab 3 — Library

| Control | Type | Default | Description |
|---|---|---|---|
| **Include field assignments** | Checkbox | **ticked** | Export which fields use each domain, not just the domains. |
| **Export library…** | Button | — | Writes this container's domains to a JSON file you can version and reuse. |
| **Mode** | Combo box | `Merge — add and update, keep the rest` | Or `Replace — make this container match exactly`. |
| **Attach each domain to every field with a matching name** | Checkbox | **ticked** | As on the Assignment tab, applied during the import. |
| **Preview a library…** | Button | — | Reports what an import would change, writing nothing. |
| **Import a library…** | Button | — | Performs the import. |
| Result log | Read-only text | empty | Where the outcome of a preview or an import appears. |

## How to use

1. Click a layer stored in the GeoPackage you want to work on, then open **KGA
   Schema Tools > Domain & Schema Manager** (or press its toolbar button). That
   GeoPackage is loaded for you. Close the Processing window once the pane
   appears.
2. If it opened on nothing, drag a `.gpkg` onto the path box from the Browser
   panel or use **Browseâ€¦**. A large container takes a moment to read.
3. On **Domains**, press **New** and fill in the definition: name, domain type,
   field type, then the coded values, range or glob. **Paste listâ€¦** takes a
   list off the clipboard, which is the quickest way to build a coded domain.
   Press **Save domain**.
4. On **Assignment**, find a field and set its *Domain* cell. Then press **Apply
   to all matching field names** to give every same-named field across the
   container the same domain. Press **Apply assignments** to write them.
5. On **Library**, press **Export libraryâ€¦** to save the whole set as JSON you
   can version and reuse. To bring one in, choose the **Mode**, press **Preview
   a libraryâ€¦** first to see what would change, then **Import a libraryâ€¦**.
6. Run **[Validate Against Domains](validate_against_domains.md)** afterwards â€”
   the data already in the container was never checked against the new rules.

## Outputs

Nothing is returned to Processing — the tool writes into the container:

- Domains created, renamed, retyped or deleted in the GeoPackage.
- Domain assignments written onto layer fields, so the attribute form shows the
  drop-down.
- A JSON **domain library** file, when you export one. That file is what
  **[Apply Domain Library](apply_domain_library.md)** consumes.

## Notes and limits

- **Editing needs a GeoPackage.** A File Geodatabase can be read and exported to
  a library, but not written to.
- **Domains belong to the whole GeoPackage, not to one layer.** Exporting a
  single layer with *Save Features As…* can leave its domains behind. Use
  **Copy container…** to share or back up the data with the domains intact —
  this is what the yellow strip in the window is warning about.
- **Opening a container reads every layer's fields**, so a large container takes
  a noticeable moment to load. The busy overlay reports progress.
- **The window stays open.** Close the Processing dialog behind it and keep
  working. Running the tool again while it is open raises it; running it after
  you closed it starts clean, without the previous container loaded.
- Changing a domain does not re-check data that is already there. Run
  **[Validate Against Domains](validate_against_domains.md)** afterwards to find
  values the new rule rejects.
- For repeating this work — in a model, in batch, or from `processing.run()` —
  use **[Apply Domain Library](apply_domain_library.md)** instead. The dialog is
  for exploring; the algorithm is for repeating.
- The tool cannot run headless — it needs the QGIS main window.
