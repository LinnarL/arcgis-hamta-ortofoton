# -*- coding: utf-8 -*-
"""
ortofoto_worker.py

Läser block ur Lantmäteriets ortofoton och sätter ihop dem till en GeoTIFF.
Körs i egna processer, startade av HamtaOrtofoton.pyt, och importerar aldrig
arcpy: GDAL i trådar i samma process som arcpy har gett access violation
(Esris gdal_e.dll delas), se CLAUDE.md. gdal importeras därför först i main(),
så att verktygslådan kan läsa konstanterna här utan att ladda GDAL.

Protokoll: en JSON-uppgift per rad på stdin, ett svar per rad på stdout med
prefixet RESULT_PREFIX. Allt annat går till stderr (loggfil).

Filerna är COG. /vsicurl/ hämtar bara de byte som behövs och gdal.Warp väljer
själv närmaste översikt, så en grov cellstorlek läser bara översiktsnivån.
GDAL:s curl i Pro saknar CA-certifikat, så CA-filen pekas mot certifi.

Ett pixelvärde 0 betyder NoData i resultatet. Warp skriver en alfakanal som
visar vilka pixlar som har data; riktiga 0 höjs till 1 och pixlar utan data
sätts till 0 i alla band, så att en mörk pixel aldrig blir ett hål och ett hål
aldrig syns som svart.
"""

import json
import os
import re
import sys
import traceback

RESULT_PREFIX = "@@RESULT "
NODATA = 0

USER_AGENT = "arcgis-hamta-ortofoton/1.0"

_TIFF_BLOCK = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=DEFLATE", "PREDICTOR=2",
               "ZLEVEL=1", "ALPHA=NO"]
_TIFF_FINAL = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=DEFLATE", "PREDICTOR=2",
               "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS", "ALPHA=NO"]
# Översikter byggs tills den minsta sidan är under detta.
MIN_OVERVIEW_PX = 256


def _configure(gdal):
    import certifi
    options = {
        "GDAL_CURL_CA_BUNDLE": certifi.where(),
        "CURL_CA_BUNDLE": certifi.where(),
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "CPL_VSIL_CURL_CHUNK_SIZE": "524288",
        "GDAL_INGESTED_BYTES_AT_OPEN": "65536",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_USERAGENT": USER_AGENT,
        # Utan AUTH skickas GDAL_HTTP_BEARER inte med.
        "GDAL_HTTP_AUTH": "BEARER",
        "GDAL_HTTP_TIMEOUT": "120",
        "GDAL_HTTP_CONNECTTIMEOUT": "30",
        "GDAL_HTTP_MAX_RETRY": "5",
        "GDAL_HTTP_RETRY_DELAY": "2",
        "GDAL_CACHEMAX": "256",
        "GDAL_PAM_ENABLED": "NO",
    }
    for key, value in options.items():
        gdal.SetConfigOption(key, value)
    gdal.UseExceptions()


def warp_block(gdal, task):
    """
    Ett block: sources ligger äldst först, så att den nyaste bilden hamnar överst.
    Returnerar {"empty": True} om blocket inte fick någon pixel med data.
    """
    gdal.SetConfigOption("GDAL_HTTP_BEARER", task["token"])
    vrts = []
    tmp = task["out"] + ".warp.tif"
    n_bands = len(task["sources"][0]["bands"])
    try:
        for i, src in enumerate(task["sources"]):
            vrt = "/vsimem/{}_{}.vrt".format(os.getpid(), i)
            gdal.Translate(vrt, "/vsicurl/" + src["url"], format="VRT", bandList=src["bands"])
            vrts.append(vrt)

        options = dict(
            format="GTiff", outputBounds=task["bounds"], width=task["width"], height=task["height"],
            dstSRS=task["srs"], resampleAlg=task["resampling"], dstAlpha=True,
            outputType=gdal.GDT_Byte, warpMemoryLimit=256, multithread=False,
            creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "ZLEVEL=1"],
        )
        if task.get("cutline"):
            with open(task["cutline"], encoding="utf-8") as fh:
                options["cutlineWKT"] = fh.read()
            options["cutlineSRS"] = task["srs"]
        gdal.Warp(tmp, vrts, options=gdal.WarpOptions(**options))

        src_ds = gdal.Open(tmp)
        valid = src_ds.GetRasterBand(n_bands + 1).ReadAsArray() >= 128
        if not valid.any():
            src_ds = None
            return {"empty": True}
        out = gdal.GetDriverByName("GTiff").Create(
            task["out"], task["width"], task["height"], n_bands, gdal.GDT_Byte,
            options=_TIFF_BLOCK)
        out.SetGeoTransform(src_ds.GetGeoTransform())
        out.SetProjection(src_ds.GetProjection())
        for b in range(1, n_bands + 1):
            band = src_ds.GetRasterBand(b).ReadAsArray()
            band[valid & (band == NODATA)] = 1
            band[~valid] = NODATA
            target = out.GetRasterBand(b)
            target.SetNoDataValue(NODATA)
            target.WriteArray(band)
        out.FlushCache()
        out = None
        src_ds = None
        return {"empty": False}
    finally:
        for vrt in vrts:
            gdal.Unlink(vrt)
        if os.path.exists(tmp):
            gdal.GetDriverByName("GTiff").Delete(tmp)


def assemble(gdal, task):
    """Blocken till en GeoTIFF med översikter, över hela rutnätet."""
    vrt_path = task["out"] + ".vrt"
    x0, y0, x1, y1 = task["bounds"]
    vrt = gdal.BuildVRT(vrt_path, task["blocks"], outputBounds=(x0, y0, x1, y1),
                        xRes=task["cell"], yRes=task["cell"], srcNodata=NODATA, VRTNodata=NODATA)
    vrt = None
    gdal.Translate(task["out"], vrt_path, format="GTiff", creationOptions=_TIFF_FINAL)
    gdal.Unlink(vrt_path)

    for key, value in (("COMPRESS_OVERVIEW", "DEFLATE"), ("PREDICTOR_OVERVIEW", "2"),
                       ("BIGTIFF_OVERVIEW", "IF_SAFER"), ("GDAL_NUM_THREADS", "ALL_CPUS")):
        gdal.SetConfigOption(key, value)
    ds = gdal.Open(task["out"], gdal.GA_Update)
    levels = []
    level = 2
    while max(ds.RasterXSize, ds.RasterYSize) / level >= MIN_OVERVIEW_PX:
        levels.append(level)
        level *= 2
    if levels:
        ds.BuildOverviews("AVERAGE", levels)
    ds.FlushCache()
    ds = None
    return {}


def main():
    from osgeo import gdal
    _configure(gdal)
    for line in sys.stdin:
        task = json.loads(line)
        try:
            result = warp_block(gdal, task) if task["kind"] == "block" else assemble(gdal, task)
            result["ok"] = True
        except Exception as exc:
            traceback.print_exc()
            sys.stderr.flush()
            code = re.search(r"HTTP response code: (\d+)", str(exc))
            result = {"ok": False, "error": str(exc), "http": int(code.group(1)) if code else None}
        result["id"] = task.get("id")
        sys.stdout.write(RESULT_PREFIX + json.dumps(result) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
