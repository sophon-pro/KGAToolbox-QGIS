# Zoom to Location

|  |  |
|---|---|
| **Algorithm ID** | none: a window on the KGA Toolbox toolbar and menu (not a Processing algorithm) |
| **Group** | KGA Toolbox toolbar |
| **Source** | `kga_tools/gui/zoom_location_dialog.py`, `kga_tools/core/zoom_location.py` |
| **Icon** | `icons/navigation/zoomLocation.png` |
| **Type** | Window |

## Overview

Move the map to a place without hunting for it. Pick a continent, then a country, province, district, commune
and village, each list filled from the one above it, or type a latitude and longitude. You can stop at any
rung: pick only a country to see the whole country, or go down to one commune.

The button sits next to **Clip Basemap by Boundary** on the KGA Toolbox toolbar.

## Controls

| Control | Meaning |
|---|---|
| **Continent**, **Country** | Built in, so they open instantly and work offline. Starts on Asia |
| **Province**, **District**, **Commune** | The first, second and third admin level of the country. Each is a check list: tick several and the list below holds the places of all of them (a name is followed by its parent when more than one parent is ticked). Ticks in the lists below are kept when they still apply. A list is greyed out when the country publishes no boundaries at that level |
| **Village** | A check list of the villages OpenStreetMap places inside the ticked communes (or districts, when the country has no communes). Only the villages OpenStreetMap has named are listed |
| **Basemap** | A preset such as Google Satellite, a basemap already in the project, or **No basemap**. It is added to the map when it is not there yet |
| **Clip basemap for the area? (by extent)** | Show the basemap only inside the box around the place (see Clip Basemap by Boundary; **Remove Clip** there undoes it) |
| **Add the location data to map?** | Add the place as a layer: its outline, or a point for a village or a coordinate. A continent has no outline to add |
| **Or type coordinate** | Latitude, longitude in decimal degrees, for example `11.5972, 104.8887`. `11.5972N 104.8887E` also works. When this box is filled in it is used instead of the lists |
| **Zoom To** | Moves the map to fit every place ticked in the deepest list that has a tick, and flashes the outlines. With the add checkbox on, each place becomes one feature of the new layer |

## Where the data comes from

* **Provinces, districts, communes:** [geoBoundaries](https://www.geoboundaries.org) (gbOpen, simplified), levels
  ADM1 to ADM3. It does not say which district is in which province, so the window works that out from the
  shapes. Boundaries are the published ones, which can differ from the official ones; check the layer's
  metadata for the licence.
* **Villages:** OpenStreetMap contributors (ODbL), through the Overpass API. Coverage varies a lot by country
  and by area; an empty list means OpenStreetMap has no named village there, not that there are none.
* **Continents and countries:** the country list and extents that ship with the plugin and QGIS.

## Notes and limits

* Needs an internet connection for provinces, districts, communes and villages. Boundary files are cached in
  the QGIS profile, so a country you have used opens quickly and works offline. Villages are cached for a week.
* Villages are looked up in the background, with a turning indicator beside the status line, so the window never
  freezes. The **Village** list stays greyed out when OpenStreetMap has no named village for the area.
* The first time a country's districts or communes are opened, the file for the whole country is downloaded.
* A village or typed coordinate is framed with a box about 2 km across.
* If the map's coordinate system cannot show the place (a UTM zone far from it), the window says so; change the
  project CRS, for example to EPSG:4326 or EPSG:3857.
