# Go To XY

|  |  |
|---|---|
| **Algorithm ID** | none: a bar over the map, switched on from the KGA Toolbox toolbar and menu (not a Processing algorithm) |
| **Group** | KGA Toolbox toolbar |
| **Source** | `kga_tools/gui/go_to_xy_bar.py`, `kga_tools/core/go_to_xy.py` |
| **Icon** | `icons/navigation/goToXY.png` |
| **Type** | Map bar |

## Overview

Type a coordinate, press **Zoom To**, and the map moves there. A temporary point is drawn at the spot with the pin
style you chose on the bar. It works like Go To XY in ArcGIS Pro.

The button sits next to **Zoom to Location** on the KGA Toolbox toolbar. Press it to show the bar at the top left of
the map, press it again (or the **×** on the bar) to close it. The round chevron at the left folds the bar down to
one button.

## Controls

| Control | Meaning |
|---|---|
| **X**, **Y** | The coordinate. Press Enter in either box instead of the button if you like |
| **Unit** | How the coordinate is written (see below). Your choice is remembered |
| **Combined** | Tick it to type or paste both values in one box, for example `11.560115921796148, 104.91513426178041`. Degrees are read **latitude first**, as web maps copy them; a letter (`104.91E, 11.56N`) or a first value that cannot be a latitude changes the order. Separate the two with a comma, semicolon, tab or space, with or without brackets. Meters, feet and UTM read `X, Y`. Not used by MGRS, US National Grid or Search. Remembered |
| **Zone** | Only for UTM: the zone number and hemisphere, for example `48N` or `33S` |
| **Zoom To** | Moves the map to the position and marks it. The scale becomes 1:5000 unless the map is already closer than that |
| **Pin** | The look of the temporary point: a pin in red, blue, green, orange, yellow, purple or black, or a circle, star, cross or diamond. Change it before or after Zoom To |
| **Clear** | Remove the temporary point |

A box that cannot be read is outlined in red and the message bar says why. Nothing moves until the coordinate is
valid.

## Units

| Unit | What to type |
|---|---|
| **Search Place** | Type a place name, the way you would in a web map. After three letters a list of matching places drops down under the box, with the typed part in bold and the area in grey (for example `Angkor Wat` · `Krong Siem Reap`). Click one, or use the Up and Down keys and Enter, and the map goes there and the pin marks it. Places near the middle of the map are listed first. A city or district is framed by its outline; a building or village is shown at 1:5000. Press **Search** (or Enter with nothing highlighted) for a fuller search that lists more matches. You can also **paste a Google Maps link** (a `maps.app.goo.gl` share link or a full `google.com/maps` address): it is opened, the map goes to the place or dropped pin it names, and the pin marks it |
| **Meters**, **Feet**, **US Feet** | X and Y in the **map's own coordinate system** (the project CRS), whatever its unit. Not available while the map is in degrees |
| **Decimal Degrees** | X = longitude, Y = latitude, for example `104.8887` and `11.5972`. A minus sign or a letter (`104.8887E`) sets the side |
| **Degrees Minutes Seconds** | `104 53 19.3 E` and `11 35 50 N`; `104°53'19.3"E` also works |
| **Degrees Decimal Minutes** | `104 53.32 E` and `11 35.83 N` |
| **MGRS** | One reference in the X box, for example `48PXS 12345 67890`. Two to ten digits are accepted; fewer digits mean a bigger square and the middle of it is used |
| **US National Grid** | Read the same way as MGRS |
| **UTM** | X = easting, Y = northing in metres, and the zone, for example `500000`, `1283000`, `48N` |

The degree, UTM and MGRS units name a place on the Earth, so they work whatever coordinate system the map uses.

## The temporary point

* It is drawn on the map only. It is not a layer, it is not saved with the project, and it is not selectable.
* It stays in place if you change the map's coordinate system.
* The next Zoom To replaces it. **Clear** or closing the bar removes it.
* To keep a position, add a layer yourself, for example with **Create Points from Table**.

## Notes and limits

* In UTM, **N** and **S** after the zone number are the hemisphere. A latitude band letter (C to X) works too: C to M
  are south, P to X north.
* MGRS covers 80°S to 84°N. The polar grid is not supported, and a reference is always read in the zone it names (the
  special zones around Norway and Svalbard are not applied when working out a reference from a position).
* An easting must be 100000 to 900000 and a northing 0 to 10000000 metres.

* A pin stands on its tip, so the tip marks the exact spot. The default is the red pin.
* **Search Place** needs an internet connection. While you type in the search box, the text so far (and the middle of
  the map, to rank nearby places first) is sent to the Photon service (photon.komoot.io) to fill the list. **Search**
  sends the text to OpenStreetMap's Nominatim service (nominatim.openstreetmap.org). Nothing is sent in the other
  units. Results are OpenStreetMap data, so coverage varies by area and shops and businesses are often missing.
* A pasted Google Maps link is opened from your computer to read where it leads; only that link is requested. Nothing
  else about your map or project is sent. If Google changes the form of its addresses, a link may stop working; the
  message bar says so.
