# Hämta ortofoton

ArcGIS Pro Python toolbox that downloads Lantmäteriet's orthophotos for an area and adds them to
the map, for use without a connection. It shows which years and image types cover the area, and
you choose what to fetch and at what cell size. Each choice becomes its own raster.

Only the area is read, straight from Lantmäteriet's cloud-optimised files. Whole tiles (up to
about 700 MB each) are never downloaded, and a coarser cell size reads only the files' overviews.
For a 16 ha area over two tiles, 0.16 m read about 49 MB, 2 m about 4 MB.

## Requirements

- ArcGIS Pro 3.x, Basic licence is enough. Developed and tested on 3.6 with Python 3.13.
- No extra packages. GDAL's Python bindings and `certifi` ship with the default `arcgispro-py3`
  environment.
- A consumer key and secret from Lantmäteriet's API portal with **both**:
  - the API `STAC-bild`, and
  - an order of [Ortofoto Nedladdning](https://www.lantmateriet.se/sv/geodata/vara-produkter/produktlista/ortofoto-nedladdning/)
    on Geotorget. It is free of charge, but the order is reviewed and special terms of use must be
    accepted.

  Searching the catalogue needs no key, only downloading does.

## Install

1. Clone or download this repo. Keep `HamtaOrtofoton.pyt` and `ortofoto_worker.py` in the same
   folder; the toolbox runs the worker file in separate processes.
2. In ArcGIS Pro: Catalog, Toolboxes, Add Toolbox, select `HamtaOrtofoton.pyt`.
3. Open Lantmäteriet: hämta ortofoton, Hämta ortofoton.

## Use

1. Choose the area, as polygons or an extent.
2. Tick **Sök tillgängliga ortofoton**. The list **Ortofoton att hämta** fills with what covers the
   area, for example `2025 | IR | 0,16 m | täcker 100 % | ca 15 MB`.
3. Tick the rows you want, set a cell size if you do not want the original, and run.

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| Avgränsa området med | Polygoner | Polygons from a layer or drawn in the map, or an extent |
| Intresseområde (polygoner) | empty | Any coordinate system. All features or the layer's selection are merged, and the rasters are clipped to the shapes |
| Utbredning | - | Rectangle: display extent, a layer's extent, a drawn rectangle or typed coordinates. Typed coordinates are read in the active map's coordinate system |
| Sök tillgängliga ortofoton | off | Calls the open catalogue from the dialog, again whenever the area changes. Off by default so that the dialog stays offline until asked |
| Ortofoton att hämta | - | One raster per row. From a script, give the first three fields: `2025 \| Färg \| 0,16 m`. An unknown row fails and lists what is available |
| Utdata-arbetsyta | project geodatabase | Geodatabase or folder. A folder gives GeoTIFF |
| Namnprefix | `ortofoto` | Output names are `<prefix>_<year>_<type>`, with the resolution added if the same year and type exist in two resolutions |
| Cellstorlek (m) | files' own | 0.1 to 100 m. Coarser than the files reads only their overviews; finer adds no information |
| Consumer key / secret | - | Under Inloggning. The secret is a hidden field |
| Spara nyckeln på den här datorn | off | Under Inloggning. Saves the key and secret in Windows Credential Manager, once Lantmäteriet has accepted them. After that both fields can stay empty, and the same entry is used by all toolboxes here that take this key. To remove it: Credential Manager, Windows Credentials |

The tool honours Output Coordinate System (default SWEREF 99 TM), Cell Size when it is a number,
Snap Raster when it has the output's coordinate system, Scratch Workspace and overwrite output.

## Image types

| Type | Bands | Available |
|---|---|---|
| Färg | red, green, blue | 2005 onwards |
| IR | near infrared, red, green (false colour) | 2006 onwards |
| Färg + IR | red, green, blue, near infrared | 2019 onwards |
| Svartvit | one grey band | Up to about 2004 |

From 2019 Lantmäteriet delivers one four-band file per tile, 0.16 or 0.4 m. Before that colour and
IR are separate files at 0.25 or 0.5 m. Files are in SWEREF 99 TM or, for older images, RT 90, and
RT 90 images are reprojected.

## Good to know

- A year rarely covers the whole country: each flight area is photographed every few years. If a
  year covers only part of your area the dialog warns, the log says how much, and the rest is
  NoData.
- If two tiles of the same row overlap, the newest photo is on top.
- Pixel value 0 is NoData in the output. Real 0 values in the image are raised to 1, so a dark
  pixel never becomes a hole.
- At the original cell size in SWEREF 99 TM, pixels are copied unchanged. Coarser cells are
  averaged, and finer cells or another coordinate system use cubic resampling.
- Source: Lantmäteriet. It is written to each raster's metadata. Check your order's terms of use
  for how the images may be used and credited.
