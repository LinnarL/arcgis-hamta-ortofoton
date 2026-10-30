# Hämta geodata

ArcGIS Pro Python toolbox that downloads Lantmäteriet's geodata for an area and adds it to the
map, for use without a connection. It shows what covers the area, and you tick what to fetch:

- **Orthophotos**: every year and image type (colour, IR, black and white) back to 1958.
- **Markhöjdmodell**: terrain model, 1 m.
- **Topografisk webbkarta**: the topographic web map, also in its toned-down version, at the zoom
  level you choose with the cell size.
- **Vector data**: Byggnader, Marktäcke, Fastighetsindelning, Belägenhetsadresser, Ortnamn, and
  Kommun, län och rike, clipped to the area.

Rasters are read only for the area, straight from Lantmäteriet's files. Whole tiles are never
downloaded, and a coarser cell size reads only the files' overviews. For a 16 ha area over two
tiles, 0.16 m read about 49 MB, 2 m about 4 MB. The web map is one 175 GB GeoPackage and is read
the same way. Vector files are downloaded per municipality, or for the whole country, then clipped.

## Requirements

- ArcGIS Pro 3.x, Basic licence is enough. Developed and tested on 3.6 with Python 3.13.
- No extra packages. GDAL's Python bindings and `certifi` ship with the default `arcgispro-py3`
  environment.
- A consumer key and secret from Lantmäteriet's API portal. Each catalogue has its own API, and
  each product is ordered separately on [Geotorget](https://geotorget.lantmateriet.se/geodataprodukter):

  | Rows | API | Product |
  |---|---|---|
  | Ortofoton | `STAC-bild` | Ortofoto Nedladdning |
  | Markhöjdmodell | `STAC-hojd` | Markhöjdmodell Nedladdning |
  | Topografisk webbkarta | `STAC-karta` | Topografisk webbkarta Nedladdning, raster |
  | Vector data | `STAC-vektor` | One order per product |

  The products are free of charge, but some orders are reviewed and special terms of use must be
  accepted. If the key lacks a chosen product, the tool stops before fetching anything and says
  which one. Searching needs no key, only downloading does.

## Install

1. Clone or download this repo. Keep `HamtaOrtofoton.pyt` and `ortofoto_worker.py` in the same
   folder; the toolbox runs the worker file in separate processes.
2. In ArcGIS Pro: Catalog, Toolboxes, Add Toolbox, select `HamtaOrtofoton.pyt`.
3. Open Lantmäteriet: hämta geodata, Hämta geodata.

## Use

1. Choose the area, as polygons or an extent.
2. Tick **Sök tillgängliga data**. The list **Data att hämta** fills with what covers the area,
   for example `2025 | IR | 0,16 m | täcker 100 % | ca 15 MB`. Height, map and vector rows come
   first, then orthophotos, newest first.
3. Tick the rows you want, set a cell size if you do not want the default, and run.

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| Avgränsa området med | Polygoner | Polygons from a layer or drawn in the map, or an extent |
| Intresseområde (polygoner) | empty | Any coordinate system. All features or the layer's selection are merged, and the results are clipped to the shapes |
| Utbredning | - | Rectangle: display extent, a layer's extent, a drawn rectangle or typed coordinates. Typed coordinates are read in the active map's coordinate system |
| Sök tillgängliga data | off | Calls the open catalogues from the dialog, again whenever the area changes. Off by default so that the dialog stays offline until asked |
| Data att hämta | - | One raster per orthophoto, height or map row, one feature class per layer in a vector product. From a script, give the first three fields: `2025 \| Färg \| 0,16 m`, `Höjd \| Markhöjdmodell \| 1 m`. An unknown row fails and lists what is available |
| Utdata-arbetsyta | project geodatabase | Geodatabase or folder. A folder gives GeoTIFF and shapefiles |
| Namnprefix | `lm` | Output names are `<prefix>_<year>_<type>`, `<prefix>_markhojd`, `<prefix>_topowebb` and `<prefix>_<layer>` |
| Cellstorlek (m) | files' own, 1 m for the map | 0.1 to 100 m. Coarser than the files reads only their overviews; finer adds no information. Not used for vector data |
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
- In orthophotos and the map, pixel value 0 is NoData in the output. Real 0 values are raised to 1,
  so a dark pixel never becomes a hole. Markhöjdmodell is 32-bit float in metres in RH 2000, with
  NoData -9999.
- At the original cell size in SWEREF 99 TM, pixels are copied unchanged. Coarser cells are
  averaged, and finer cells or another coordinate system use cubic resampling (bilinear for
  height). The map is always resampled bilinearly.
- The web map's content changes with scale, so a coarser cell size gives a more generalised map,
  not just a smaller one.
- A vector row can fetch more files than the area needs, since the catalogue's municipality shapes
  are coarse. The clip removes what is outside. Marktäcke and Fastighetsindelning can be 30-70 MB
  per municipality.
- NMK 50 and 250 in the same catalogue are left out: their terms cover public-sector use in civil
  preparedness.
- Source: Lantmäteriet. It is written to each dataset's metadata. Check your order's terms of use
  for how the data may be used and credited.
