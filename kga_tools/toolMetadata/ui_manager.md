# Panels and Toolbars Manager

|  |  |
|---|---|
| **Algorithm ID** | none: a dock panel (not a Processing algorithm) |
| **Group** | KGA Toolbox toolbar, next to Data Explorer |
| **Source** | `kga_tools/gui/ui_manager/` |
| **Icon** | `gui/ui_manager/icons/icon.svg` |
| **Type** | Dock panel, tabbed with the Catalog; shortcut **Ctrl+Alt+P** (rebindable in Settings > Keyboard Shortcuts) |

## Overview

A visual replacement for the right-click Panels / Toolbars text menu. Every panel and toolbar in the QGIS
window, including those added by other plugins, is a card you toggle with one click.

## Key points

* **Cards:** toolbars show a strip of their real tool icons; panels show a snapshot taken the first time they
  are open and kept on disk, so a closed panel still has a preview. Hover a snapshot to enlarge it.
* **Search by tool name:** typing `buffer` finds the toolbar that contains a Buffer tool and says so.
* **Filters:** Panels / Toolbars, All / Open / Hidden, Compact rows. Show all / Hide all act only on the
  items currently listed.
* **Layouts:** save the current workspace (positions and visibility) under a name, apply it in one click,
  and export or import layouts as JSON to give a whole class the same workspace.
* **Always current:** the list rebuilds itself when a plugin adds or removes a panel or toolbar, and follows
  the light or dark QGIS theme.

## Notes and limits

* Applying a layout never hides the Panels and Toolbars Manager itself.
* Snapshots of a widget are only possible while it is on screen.
