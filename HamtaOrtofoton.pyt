# -*- coding: utf-8 -*-
"""
HamtaOrtofoton.pyt

Hämtar Lantmäteriets geodata för ett område och lägger dem i kartan för
användning utan uppkoppling: ortofoton, markhöjdmodell, topografisk webbkarta
och vektordata. Användaren ser vad som täcker området och väljer vad som ska
hämtas och med vilken cellstorlek.

Datakälla
---------
STAC-katalog (öppen, ingen inloggning):
    https://api.lantmateriet.se/stac-bild/v1
    Sök:     POST /search  {"bbox": [...], "limit": 1000}
             Nästa sida via länken rel="next". Sökningen svarar 429 efter ett
             tiotal snabba anrop i följd (inget Retry-After), så stora sidor.

Katalogen har en samling per flygområde och år (orto-o2-2025) och täcker hela
landet. Varje item är en ruta med en COG (asset "data"):
    https://dl1.lantmateriet.se/bild/data/orto/<produkt>/<område>/<id>.tif
    properties: flygar, upplosning (m), spektraltyp, datetime
    asset data: proj:code, proj:bbox, proj:shape, file:size

    spektraltyp  Band                     Upplösning och rutor
    rgbi         R, G, B, NIR             0,16 m (2,5 km) eller 0,4 m, från 2019
    rgb          R, G, B                  0,25 m eller 0,5 m, 2005-2018
    cir          NIR, R, G (falsk färg)   0,25 m eller 0,5 m, 2006-2018
    pan          ett gråskaleband         0,5 m eller 1 m, 1940-2004

Äldre filer kan ligga i RT 90 (EPSG:3021), övriga i SWEREF 99 TM. En fil per
bildtyp före 2019, en fil med fyra band därefter, så IR ur en rgbi-fil är
banden 4, 1, 2. Bandordningen i rgbi är kontrollerad på en riktig körning (2025,
Värmdö): röda bilar syns röda, vegetation lyser i band 4. Övriga spektraltyper
är inte kontrollerade på riktiga filer.

Filerna kräver OAuth2 (client credentials):
    POST https://apimanager.lantmateriet.se/oauth2/token
    Basic-auth med consumer key/secret, grant_type=client_credentials.
    Token gäller 3600 s. Nyckeln måste ha API:et STAC-bild och en beställning
    av Ortofoto Nedladdning, annars svarar dl1 med 401 eller 403.

Hela rutor laddas aldrig ned. Området delas i block om högst BLOCK_PX pixlar,
och ortofoto_worker.py läser varje block med GDAL över HTTP i egna processer.
Vid grov cellstorlek läses bara filernas översikter. Blocken sätts ihop till en
GeoTIFF med översikter lokalt och skrivs till utdataplatsen i ett svep.

Verktygstips (parameterförklaringar) skrivs till
HamtaOrtofoton.HamtaOrtofoton.pyt.xml från TOOLTIPS nedan när verktygslådan
laddas, så att texten bara finns på ett ställe.

Övriga kataloger (samma inloggning, men varje produkt beställs för sig):
    https://api.lantmateriet.se/stac-hojd/v1    dtm-cog: markhöjdmodell, COG i
        EPSG:5845 (SWEREF 99 TM + RH 2000), 1 m, flyttal. Läses som ortofotona.
    https://api.lantmateriet.se/stac-karta/v1   topowebb: en GeoPackage på 175 GB
        per variant och koordinatsystem. Läses med GDAL, se ortofoto_worker.py.
    https://api.lantmateriet.se/stac-vektor/v1  zip med GeoPackage per kommun
        (byggnader, marktäcke, fastighetsindelning, adresser) eller för hela
        landet (ortnamn, kommun-lan-rike). Laddas ned hela, klipps till området
        och skrivs som featureklasser. Item "aktuell" i kommun-lan-rike är
        nuläget, övriga items är äldre år. Lagernamnen läses ur filerna.

NMK 50 och 250 i stac-karta tas inte med: användningsvillkoren gäller offentlig
slutanvändning inom civil beredskap.

Krav: ArcGIS Pro 3.x. arcpy, numpy, GDAL och certifi ingår i arcgispro-py3.
Ingen licensnivå utöver Basic behövs.
"""

import base64
import datetime
import importlib
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from xml.sax.saxutils import escape

import arcpy

# GDAL-delen ligger i ortofoto_worker.py bredvid verktygslådan och körs i egna
# processer. reload: Pro håller modulen i minnet mellan körningar och uppdateringar.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import ortofoto_worker as _ow  # noqa: E402
importlib.reload(_ow)

# ── Konstanter ────────────────────────────────────────────────────────────────

STAC_SEARCH_URL = "https://api.lantmateriet.se/stac-bild/v1/search"
TOKEN_URL = "https://apimanager.lantmateriet.se/oauth2/token"
STAC_HOJD_URL = "https://api.lantmateriet.se/stac-hojd/v1/search"
STAC_KARTA_URL = "https://api.lantmateriet.se/stac-karta/v1/search"
STAC_VEKTOR_URL = "https://api.lantmateriet.se/stac-vektor/v1/search"
PRODUCT_LIST = "https://www.lantmateriet.se/sv/geodata/vara-produkter/produktlista/"
PRODUCT_URL = PRODUCT_LIST + "ortofoto-nedladdning/"
ATTRIBUTION = "Lantmäteriet"

KIND_ORTHO = "ortho"
KIND_DTM = "dtm"
KIND_MAP = "map"
KIND_VECTOR = "vector"

DTM_COLLECTION = "dtm-cog"
DTM_RES = 1.0
DTM_URL = PRODUCT_LIST + "markhojdmodell-nedladdning"
# Item i stac-karta: (rubrik, namn i utdata). Det finns även items i Web Mercator
# (-3857). Rastret projiceras ändå om vid behov, så SWEREF-filen räcker.
MAP_ITEMS = {
    "topowebb-3006": ("Topografisk webbkarta", "topowebb"),
    "topowebb_nedtonad-3006": ("Topografisk webbkarta nedtonad", "topowebb_nedtonad"),
}
MAP_COLLECTION = "topowebb"
MAP_DEFAULT_CELL = 1.0
MAP_URL = PRODUCT_LIST + "topografisk-webbkarta-nedladdning-raster"
# Samling i stac-vektor: rubrik. Filerna täcker en kommun eller hela landet.
VECTOR_COLLECTIONS = {
    "byggnader": "Byggnader",
    "marktacke": "Marktäcke",
    "fastighetsindelning": "Fastighetsindelning",
    "belagenhetsadresser": "Belägenhetsadresser",
    "ortnamn": "Ortnamn",
    "kommun-lan-rike": "Kommun, län och rike",
}
VECTOR_LATEST_ITEM = {"kommun-lan-rike": "aktuell"}
VECTOR_URL = "https://geotorget.lantmateriet.se/geodataprodukter"

USER_AGENT = _ow.USER_AGENT
HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
# Sökningen i dialogen ska ge upp snabbt, dialogen väntar på den.
DIALOG_TIMEOUT = 20
# Fyra kataloger per sökning, och de svarar 429 efter ett tiotal snabba anrop.
DIALOG_RETRIES = 2

TOKEN_MAX_AGE_S = 50 * 60

SWEREF99TM_WKID = 3006
WGS84_WKID = 4326

# Sida på ett block i pixlar. Ett block ska rymmas i arbetsprocessens minne och
# vara så stort att det inte öppnar filerna i onödan (ungefär 1 s per block).
BLOCK_PX = 4096
# Läsningen väntar mest på nätverket. Taket skyddar Lantmäteriets server.
MAX_WORKERS = 4

# Filerna är inte större än så här jämfört med okomprimerade pixlar (uppmätt på
# katalogens file:size: 0,69-0,76 för rgbi, 0,75-0,80 för rgb och cir).
SIZE_FACTOR = 0.8
# Blocken och den färdiga filen ligger samtidigt på disken.
DISK_FACTOR = 2.2
# Markhöjdmodellens flyttal, från katalogens file:size (246 MB för 10 000 x 10 000).
DTM_BYTES_PER_CELL = 2.5
# En uppackad GeoPackage jämfört med zip-filen, uppskattat.
UNZIP_FACTOR = 5
DOWNLOAD_CHUNK = 1024 * 1024
LARGE_GB = 10

# Ytor mindre än så här (m²) räknas inte som täckning.
SLIVER_M2 = 1.0
FULL_COVERAGE = 0.995

AOI_POLYGONS = "Polygoner (lager eller ritade i kartan)"
AOI_EXTENT = "Utbredning (kartvy, lager eller koordinater)"
AOI_EMPTY = "Rita minst en polygon i kartan eller välj ett polygonlager."

IMG_RGB = "Färg"
IMG_IR = "IR"
IMG_RGBI = "Färg + IR"
IMG_GRAY = "Svartvit"
# Bildtyper per spektraltyp i katalogen, med banden i filen som ger bildtypen.
SPECTRAL = {
    "rgbi": ((IMG_RGB, [1, 2, 3]), (IMG_IR, [4, 1, 2]), (IMG_RGBI, [1, 2, 3, 4])),
    "rgb": ((IMG_RGB, [1, 2, 3]),),
    "cir": ((IMG_IR, [1, 2, 3]),),
    "pan": ((IMG_GRAY, [1]),),
    "gra": ((IMG_GRAY, [1]),),
}
IMG_NAME = {IMG_RGB: "farg", IMG_IR: "ir", IMG_RGBI: "farg_ir", IMG_GRAY: "svartvit"}
IMG_ORDER = [IMG_RGB, IMG_IR, IMG_RGBI, IMG_GRAY]
SEP = " | "

# Mappar som synkas till molnet - olämpliga för stora raster
_SYNC_HINTS = ("onedrive", "sharepoint", "dropbox", "google drive")

# Nyckeln kan sparas i Windows Autentiseringshanteraren. Samma post används av alla
# verktyg med Lantmäteriets nyckel, så namnen ska vara lika i verktygslådorna.
CREDENTIAL_SERVICE = "Lantmateriet API (ArcGIS-verktyg)"
CREDENTIAL_USER = "consumer"
LOGIN_NO_KEY = "Ange en consumer key från Lantmäteriets API-portal."
LOGIN_NO_SECRET = "Ange consumer secret."

TOOL_SUMMARY = (
    "Hämtar Lantmäteriets geodata för ett område och lägger dem i kartan, så att de går att "
    "använda utan uppkoppling: ortofoton (färg, IR, svartvit), markhöjdmodell, topografisk "
    "webbkarta och vektordata som byggnader och fastighetsindelning. Visar vad som täcker "
    "området. Raster läses bara för området, vektordata hämtas per kommun och klipps."
)

# Verktygstips per parameter, visas i verktygsdialogen. Se _write_tool_metadata.
TOOLTIPS = {
    "aoi_mode": (
        "Hur området avgränsas. 'Polygoner' använder polygoner ur ett lager eller polygoner "
        "som du ritar i kartan, och klipper bilderna till själva polygonerna. 'Utbredning' ger "
        "en rektangel: aktuell kartvy, utbredningen av ett lager, en ritad rektangel eller "
        "inskrivna koordinater."
    ),
    "aoi": (
        "Polygoner som avgränsar området. Välj ett polygonlager i listan, eller rita en eller "
        "flera polygoner i kartan med ritverktyget bredvid fältet (dubbelklicka för att avsluta "
        "en polygon). Från ett lager används alla objekt, eller bara de valda om lagret har ett "
        "urval. Alla polygoner slås ihop till ett område. Lagret kan ha vilket koordinatsystem "
        "som helst. Utanför polygonerna blir bilden NoData."
    ),
    "aoi_extent": (
        "Rektangel som avgränsar området. I listan kan du välja kartvyns aktuella utbredning, "
        "eller ett lager för att använda dess utbredning. Du kan också rita en rektangel i "
        "kartan eller skriva in koordinater. Inskrivna koordinater tolkas i den aktiva kartans "
        "koordinatsystem; vilket som användes står i meddelandena."
    ),
    "search": (
        "Markera för att söka i Lantmäteriets kataloger och fylla listan 'Data att hämta' "
        "med det som täcker området. Sökningen är öppen, kräver ingen nyckel och tar någon "
        "sekund. Den görs om när området ändras, så länge rutan är markerad. Av som standard, "
        "eftersom dialogen annars skulle anropa Lantmäteriet utan att du bett om det."
    ),
    "selection": (
        "Det som ska hämtas. Listan fylls när du markerat 'Sök tillgängliga data'. Överst står "
        "höjd, karta och vektordata, sedan ortofoton med nyast först. Varje produkt beställs "
        "för sig på Geotorget, och nyckeln måste ha behörighet till det du väljer.\n"
        "Höjd är Lantmäteriets markhöjdmodell, 1 m, flyttal i meter över havet (RH 2000).\n"
        "Karta är den topografiska webbkartan. Cellstorleken väljer kartans zoomnivå: kartans "
        "innehåll ändras med skalan, så 1 m är en detaljerad karta och 16 m en översikt. "
        "Tomt = 1 m.\n"
        "Vektor är en eller flera GeoPackage som hämtas per kommun, eller för hela landet, och "
        "klipps till området. Varje lager i filen blir en egen featureklass. Cellstorleken "
        "gäller inte vektordata.\n"
        "En ortofotorad är år, bildtyp, filernas upplösning, hur stor del av området som täcks "
        "och ungefärlig storlek i filernas upplösning. Varje vald rad blir ett eget raster.\n"
        "Färg är röd, grön, blå. IR är färginfraröd i falsk färg (nära infrarött, rött, grönt), "
        "där levande vegetation syns röd. Färg + IR är ett raster med fyra band (röd, grön, "
        "blå, nära infrarött) och finns från 2019. Svartvit är äldre flygbilder.\n"
        "En årgång täcker sällan hela landet, och täcker den bara en del av området blir resten "
        "NoData.\n"
        "I ett skript anges de tre första fälten, till exempel '2025 | Färg | 0,16 m'."
    ),
    "consumer_key": (
        "Consumer key från Lantmäteriets API-portal. Nyckeln behöver det API som hör till "
        "katalogen (STAC-bild, STAC-hojd, STAC-karta, STAC-vektor) och en beställning av varje "
        "produkt på Geotorget. Produkterna är avgiftsfria, men vissa beställningar prövas och "
        "du behöver godkänna särskilda användningsvillkor. Behövs bara för att hämta, inte för "
        "att söka."
    ),
    "consumer_secret": (
        "Consumer secret som hör till nyckeln. Visas dold i dialogen och skrivs aldrig till "
        "meddelandena. Kan lämnas tomt om nyckeln är sparad."
    ),
    "save_login": (
        "Spara consumer key och secret i Windows Autentiseringshanteraren när körningen startar, "
        "krypterat för ditt Windows-konto och bara på den här datorn. Nyckeln sparas bara om "
        "Lantmäteriet godkänner den. Sedan fylls consumer key i av sig själv och secret kan "
        "lämnas tomt, i alla verktyg som använder Lantmäteriets nyckel. Fyll i och markera igen "
        "för att byta nyckel. Ta bort den under Autentiseringshanteraren, Windows-autentiseringsuppgifter."
    ),
    "out_workspace": (
        "Geodatabas eller mapp där resultatet sparas. I en mapp blir raster GeoTIFF och "
        "vektordata shapefiler, som kortar fältnamn och text. En mapp på samma "
        "dator går snabbast att visa utan uppkoppling. Undvik mappar som synkas till molnet, som "
        "OneDrive: ortofoton blir snabbt stora."
    ),
    "prefix": (
        "Början på utdatanamnen: <prefix>_<år>_<bildtyp>, till exempel lm_2025_farg, "
        "lm_markhojd, lm_topowebb, eller <prefix>_<lager> för vektordata. Finns "
        "samma år och bildtyp i flera upplösningar läggs upplösningen till i namnet. Bara "
        "bokstäver, siffror och understreck, och första tecknet måste vara en bokstav. Befintliga "
        "raster skrivs över bara om geobehandlingens inställning för att skriva över utdata är "
        "på."
    ),
    "cell_size": (
        "Cellstorlek i meter i de hämtade rastren. Tomt = filernas egen upplösning: för ortofoton "
        "0,16 eller 0,4 m från 2019, 0,25 eller 0,5 m före, för markhöjdmodellen 1 m, för kartan "
        "1 m. Gäller inte vektordata. Fördubblad "
        "cellstorlek ger en fjärdedel så stor fil. Grövre än originalet hämtar bara filernas "
        "översikter och går snabbare. Finare än originalet ger ingen ny information, bara en "
        "större fil. Ett lokalt värde på Cell Size i miljöinställningarna används om fältet är "
        "tomt. Mellan 0,1 och 100 m."
    ),
    "out_rasters": "De hämtade rastren och featureklasserna.",
}


# =============================================================================
# Förlopp
# =============================================================================

def _fmt_duration(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "{} s".format(seconds)
    if seconds < 3600:
        return "{} min".format(int(round(seconds / 60.0)))
    return "{} h {} min".format(seconds // 3600, (seconds % 3600) // 60)


def _fmt_num(v):
    """Tal med högst två decimaler och decimalkomma: 0,16."""
    return "{:g}".format(round(float(v), 2)).replace(".", ",")


def _fmt_gb(n_bytes):
    if n_bytes < 1e9:
        return "{} MB".format(max(1, int(round(n_bytes / 1e6))))
    return "{} GB".format(_fmt_num(round(n_bytes / 1e9, 1)))


class _Steps:
    """Numrerade steg i förloppsindikatorn och i meddelandena."""

    def __init__(self, total, messages):
        self.total = total
        self.messages = messages
        self.k = 0

    def next(self, text):
        self.k += 1
        self.t_step = time.time()
        label = "Steg {} av {}: {}".format(self.k, self.total, text)
        arcpy.SetProgressor("default", label)
        self.messages.addMessage(label)

    def label(self, text):
        arcpy.SetProgressorLabel("Steg {} av {}: {}".format(self.k, self.total, text))

    def done(self, text=None):
        msg = "    klart på {}".format(_fmt_duration(time.time() - self.t_step))
        self.messages.addMessage(msg + (". " + text if text else "."))


class _Silent:
    """Ersätter messages när dialogen läser området."""

    def addMessage(self, text):
        pass


# =============================================================================
# HTTP
# =============================================================================

def _http(req, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES):
    """urlopen med omförsök vid tillfälliga fel. Returnerar (status, body)."""
    delay = 2
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            # HTTPError först: den är också URLError och OSError.
            if exc.code in (408, 429) or exc.code >= 500:
                if attempt < retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
            raise
        except (urllib.error.URLError, OSError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def _get_token(key, secret):
    auth = base64.b64encode("{}:{}".format(key, secret).encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii"),
        headers={"Authorization": "Basic " + auth, "User-Agent": USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        _status, body = _http(req)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            raise ValueError(
                "Lantmäteriet godkände inte consumer key/secret (HTTP {}). Kontrollera "
                "nyckeln i Lantmäteriets API-portal.".format(exc.code)
            )
        raise
    return json.loads(body.decode("utf-8"))["access_token"]


def _saved_login():
    """(key, secret) sparade i Windows Autentiseringshanteraren, annars tomma."""
    try:
        import keyring
        raw = keyring.get_password(CREDENTIAL_SERVICE, CREDENTIAL_USER)
        saved = json.loads(raw) if raw else {}
    except Exception:
        saved = {}
    return saved.get("key", ""), saved.get("secret", "")


def _login(p):
    """
    Key och secret: det som står i fälten, annars det sparade. Varje värde för
    sig, så att en ny secret fungerar även om Pro har tömt fältet för key.
    """
    saved_key, saved_secret = _saved_login()
    key = (p["consumer_key"].valueAsText or "").strip() or saved_key
    secret = (p["consumer_secret"].valueAsText or "").strip() or saved_secret
    if not key:
        raise ValueError(LOGIN_NO_KEY)
    if not secret:
        raise ValueError(LOGIN_NO_SECRET)
    return key, secret


def _check_login_fields(p):
    """Felmeddelanden i dialogen för det som varken är ifyllt eller sparat."""
    saved_key, saved_secret = _saved_login()
    if not (p["consumer_key"].valueAsText or "").strip() and not saved_key:
        p["consumer_key"].setErrorMessage(LOGIN_NO_KEY)
    if not (p["consumer_secret"].valueAsText or "").strip() and not saved_secret:
        p["consumer_secret"].setErrorMessage(LOGIN_NO_SECRET)


def _remember_login(key, secret, messages):
    """Spara nyckeln, men bara om Lantmäteriet godkänner den (annars ValueError)."""
    _get_token(key, secret)
    try:
        import keyring
        keyring.set_password(CREDENTIAL_SERVICE, CREDENTIAL_USER,
                             json.dumps({"key": key, "secret": secret}))
    except Exception as exc:
        messages.addWarningMessage("Nyckeln kunde inte sparas: {}".format(exc))
    else:
        messages.addMessage("Nyckeln är sparad i Windows Autentiseringshanteraren.")


class _Token:
    """Token gäller en timme. Hämtas om innan den går ut, under en lång körning."""

    def __init__(self, key, secret):
        self.key = key
        self.secret = secret
        self.t = 0
        self.value = None

    def get(self):
        if time.time() - self.t > TOKEN_MAX_AGE_S:
            self.value = _get_token(self.key, self.secret)
            self.t = time.time()
        return self.value


def _access_error(code, group=None):
    title = group["title"] if group else "ortofotona"
    return ("Nyckeln saknar behörighet till {} (HTTP {}). Den behöver katalogens API i "
            "Lantmäteriets API-portal och en beställning av produkten:\n{}".format(
                title, code, group["product_url"] if group else PRODUCT_URL))


def _check_access(group, token):
    """Hämta en byte av första filen, för ett begripligt fel i stället för GDAL:s."""
    req = urllib.request.Request(
        group["items"][0]["url"],
        headers={"Authorization": "Bearer " + token, "Range": "bytes=0-0",
                 "User-Agent": USER_AGENT},
    )
    try:
        _http(req)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError(_access_error(exc.code, group))
        raise


def _stac_search(bbox_wgs84, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES, search_url=STAC_SEARCH_URL,
                 collections=None):
    """Alla items som skär bbox, över alla sidor."""
    body = {"bbox": list(bbox_wgs84), "limit": 1000}
    if collections:
        body["collections"] = list(collections)
    url, method = search_url, "POST"
    items = []
    seen_pages = set()
    while url:
        data = json.dumps(body).encode("utf-8") if method == "POST" else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        _status, raw = _http(req, timeout, retries)
        page = json.loads(raw.decode("utf-8"))
        feats = page.get("features", [])
        # Skydd mot en pager som returnerar samma sida om och om igen.
        page_key = tuple(f.get("id") for f in feats)
        if page_key in seen_pages:
            break
        seen_pages.add(page_key)
        items.extend(feats)

        nxt = next((l for l in page.get("links", []) if l.get("rel") == "next"), None)
        if not nxt or not feats:
            break
        url = nxt["href"]
        method = nxt.get("method", "GET").upper()
        body = nxt.get("body", body)
    return items


# =============================================================================
# Område
# =============================================================================

def _polygon_schema():
    """
    Tom polygonfeatureklass i SWEREF 99 TM, standardvärde för Feature Set-
    parametern så att dialogens ritverktyg ritar polygoner. Unikt namn i memory:
    memory-arbetsytan delas av hela Pro-sessionen.
    """
    name = "aoi_schema_{}".format(uuid.uuid4().hex[:12])
    arcpy.management.CreateFeatureclass("memory", name, "POLYGON",
                                        spatial_reference=arcpy.SpatialReference(SWEREF99TM_WKID))
    return "memory/" + name


def _is_layer(value):
    """Sant för ett Layer-objekt. arcpy.mp har ingen Layer-klass att jämföra med."""
    return type(value).__name__ == "Layer"


def _has_features(value):
    """True/False om värdet har objekt, None om det inte går att läsa."""
    try:
        with arcpy.da.SearchCursor(value, ["OID@"]) as cur:
            return next(iter(cur), None) is not None
    except Exception:
        return None


def _aoi_geometry(value, messages):
    """Alla polygoner i Feature Set-parametern, sammanslagna, i SWEREF 99 TM."""
    if _has_features(value) is False:
        raise ValueError(AOI_EMPTY)
    # Kontrollera koordinatsystemet innan något omprojiceras: utan det skulle
    # polygonerna tyst läsas som om de redan vore i SWEREF 99 TM.
    sr_in = arcpy.Describe(value).spatialReference
    if sr_in is None or not (sr_in.factoryCode or sr_in.exportToString()):
        raise ValueError("Intresseområdet saknar koordinatsystem.")
    sr = arcpy.SpatialReference(SWEREF99TM_WKID)
    geom = None
    n = 0
    with arcpy.da.SearchCursor(value, ["SHAPE@"], spatial_reference=sr) as cur:
        for (shape,) in cur:
            if shape is None or shape.area <= 0:
                continue
            n += 1
            geom = shape if geom is None else geom.union(shape)
    if geom is None:
        raise ValueError("Intresseområdet innehåller inga polygoner med yta.")
    messages.addMessage("Intresseområde: {} polygon(er) i {}, sammanlagt {} ha.".format(
        n, sr_in.name, _fmt_num(geom.area / 1e4)))
    return geom


def _active_map_sr():
    try:
        m = arcpy.mp.ArcGISProject("CURRENT").activeMap
        if m is not None and m.spatialReference is not None:
            return m.spatialReference, m.name
    except Exception:
        pass
    return None, None


def _aoi_from_extent(value, text, messages):
    """
    En GPExtent-parameter som polygon i SWEREF 99 TM.

    .value är ett geoprocessing-extentobjekt, inte arcpy.Extent. Koordinatsystemet
    följer bara med när utbredningen kommer från ett lager eller en datakälla, och
    då bara som WKT2 i valueAsText efter de fyra talen. Inskrivna koordinater har
    inget; de tolkas i den aktiva kartans koordinatsystem.
    """
    xmin, ymin, xmax, ymax = (float(value.XMin), float(value.YMin),
                              float(value.XMax), float(value.YMax))
    if not (xmax > xmin and ymax > ymin):
        raise ValueError("Utbredningen har ingen yta.")

    sr = None
    parts = (text or "").split(" ", 4)
    if len(parts) == 5 and parts[4].strip():
        sr = arcpy.SpatialReference()
        try:
            sr.loadFromString(parts[4].strip())
        except Exception:
            sr = None
    if sr is not None and (sr.factoryCode or sr.exportToString()):
        source = "utbredningens eget koordinatsystem"
    else:
        sr, map_name = _active_map_sr()
        if sr is not None and (sr.factoryCode or sr.exportToString()):
            source = "den aktiva kartans koordinatsystem ({})".format(map_name)
        else:
            sr = arcpy.SpatialReference(SWEREF99TM_WKID)
            source = "ingen aktiv karta, så SWEREF 99 TM antas"
    messages.addMessage("Utbredningen tolkas i {}: {}.".format(sr.name, source))

    # Förtäta kanterna, så att en utbredning i grader eller ett annat system
    # inte blir en för liten fyrhörning efter omprojicering.
    n = 16
    pts = ([(xmin + (xmax - xmin) * i / n, ymin) for i in range(n)]
           + [(xmax, ymin + (ymax - ymin) * i / n) for i in range(n)]
           + [(xmax - (xmax - xmin) * i / n, ymax) for i in range(n)]
           + [(xmin, ymax - (ymax - ymin) * i / n) for i in range(n)])
    poly = arcpy.Polygon(arcpy.Array([arcpy.Point(x, y) for x, y in pts]), sr)
    tm = arcpy.SpatialReference(SWEREF99TM_WKID)
    if sr.factoryCode != SWEREF99TM_WKID:
        poly = poly.projectAs(tm)
    if poly is None or poly.area <= 0:
        raise ValueError("Utbredningen kunde inte omvandlas till SWEREF 99 TM.")
    return poly


def _rect_polygon(x0, y0, x1, y1, sr):
    return arcpy.Polygon(arcpy.Array([arcpy.Point(x0, y0), arcpy.Point(x1, y0),
                                      arcpy.Point(x1, y1), arcpy.Point(x0, y1)]), sr)


# =============================================================================
# Katalogen: vad finns i området?
# =============================================================================

def _footprint(feature, tm):
    """Rutans polygon i SWEREF 99 TM. proj:bbox är exakt när rutan redan ligger där."""
    props = feature["properties"]
    if props.get("proj:code") == "EPSG:{}".format(SWEREF99TM_WKID) and props.get("proj:bbox"):
        return _rect_polygon(*props["proj:bbox"], tm)
    return arcpy.AsShape(feature["geometry"]).projectAs(tm)


def _key(year, image, res):
    """Radens identitet: det ett skript anger. Täckning och storlek ingår inte."""
    return SEP.join((str(year), image, "{} m".format(_fmt_num(res))))


def _pick_items(items, aoi):
    """
    Rutor per plats, nyaste först: en ruta behövs bara om den täcker något som de
    nyare inte täcker. Samma ruta kan finnas i två samlingar, och en samling kan
    delas om, så urvalet görs på yta och inte på rutans id eller gränser.
    Returnerar (rutor med äldst först, så att nyast skrivs sist, täckt yta).
    """
    covered = None
    kept = []
    for item in sorted(items, key=lambda i: i["datetime"], reverse=True):
        part = item["fp"].intersect(aoi, 4)
        if part.area < SLIVER_M2:
            continue
        if covered is not None:
            if part.difference(covered).area < SLIVER_M2:
                continue
            part = covered.union(part)
        covered = part
        kept.append(item)
    kept.reverse()
    return kept, (covered.area if covered is not None else 0.0)


def _groups(features, aoi):
    """Katalogens rutor per rad i listan: år, bildtyp, upplösning."""
    tm = arcpy.SpatialReference(SWEREF99TM_WKID)
    raw = {}
    for f in features:
        props = f["properties"]
        kinds = SPECTRAL.get(props.get("spektraltyp"))
        data = f.get("assets", {}).get("data")
        if not kinds or not data or not props.get("upplosning"):
            continue
        year = props.get("flygar") or int(props["datetime"][:4])
        fp = _footprint(f, tm)
        for image, bands in kinds:
            key = (year, image, float(props["upplosning"]))
            raw.setdefault(key, []).append({
                "id": f["id"], "url": data["href"], "bands": bands, "fp": fp,
                "datetime": props.get("datetime") or "", "size": data.get("file:size") or 0,
            })

    groups = []
    for (year, image, res), items in raw.items():
        kept, area = _pick_items(items, aoi)
        if not kept:
            continue
        n_bands = len(kept[0]["bands"])
        groups.append({
            "kind": KIND_ORTHO, "year": year, "image": image, "res": res, "items": kept,
            "area": area, "coverage": min(1.0, area / aoi.area),
            "bytes": area / (res * res) * n_bands * SIZE_FACTOR,
            "key": _key(year, image, res),
            "title": "ortofoto {} {}".format(year, image.lower()),
            "product_url": PRODUCT_URL,
        })
    groups.sort(key=lambda g: (-g["year"], IMG_ORDER.index(g["image"]), g["res"]))
    return groups


def _dtm_group(features, aoi):
    """Markhöjdmodellen, om den täcker något av området."""
    tm = arcpy.SpatialReference(SWEREF99TM_WKID)
    items = []
    for f in features:
        data = f.get("assets", {}).get("data")
        if data:
            items.append({"id": f["id"], "url": data["href"], "bands": [1],
                          "fp": _footprint(f, tm), "size": data.get("file:size") or 0,
                          "datetime": f["properties"].get("datetime") or ""})
    kept, area = _pick_items(items, aoi)
    if not kept:
        return None
    return {"kind": KIND_DTM, "key": SEP.join(("Höjd", "Markhöjdmodell", "1 m")), "res": DTM_RES,
            "items": kept, "area": area, "coverage": min(1.0, area / aoi.area),
            "bytes": area / (DTM_RES * DTM_RES) * DTM_BYTES_PER_CELL, "name": "markhojd",
            "title": "markhöjdmodell", "product_url": DTM_URL}


def _map_groups(features, aoi):
    """Webbkartan: ett item per variant. Filen täcker landet, så täckningen är hela området."""
    groups = []
    for f in features:
        known = MAP_ITEMS.get(f["id"])
        data = f.get("assets", {}).get("data")
        if not known or not data:
            continue
        title, name = known
        res = MAP_DEFAULT_CELL
        groups.append({
            "kind": KIND_MAP, "key": SEP.join(("Karta", title, "SWEREF 99 TM")), "res": res,
            "items": [{"id": f["id"], "url": data["href"], "bands": [1, 2, 3], "datetime": "",
                       "fp": None, "size": 0}],
            "area": aoi.area, "coverage": 1.0, "bytes": aoi.area / (res * res) * 3 * SIZE_FACTOR,
            "name": name, "title": title.lower(), "product_url": MAP_URL})
    return sorted(groups, key=lambda g: g["key"])


def _vector_groups(features, aoi):
    """
    Vektordata per samling. Kommunernas polygoner i katalogen är grova och kan
    överlappa, så alla filer som skär området hämtas. Klippningen sorterar bort resten.
    """
    tm = arcpy.SpatialReference(SWEREF99TM_WKID)
    found = {}
    for f in features:
        col = f.get("collection")
        data = f.get("assets", {}).get("data")
        if col not in VECTOR_COLLECTIONS or not data:
            continue
        latest = VECTOR_LATEST_ITEM.get(col)
        if latest and f["id"] != latest:
            continue
        if arcpy.AsShape(f["geometry"]).projectAs(tm).disjoint(aoi):
            continue
        found.setdefault(col, []).append({
            "id": f["id"], "url": data["href"], "size": data.get("file:size") or 0,
            "title": f["properties"].get("title") or f["id"]})
    groups = []
    for col, title in VECTOR_COLLECTIONS.items():
        items = found.get(col)
        if items:
            groups.append({
                "kind": KIND_VECTOR, "key": SEP.join(("Vektor", title, "klipps till området")),
                "res": None, "items": items, "area": aoi.area, "coverage": 1.0,
                "bytes": sum(i["size"] for i in items), "name": col, "title": title.lower(),
                "product_url": VECTOR_URL})
    return groups


def _label(group):
    if group["kind"] == KIND_VECTOR:
        return SEP.join((group["key"], "{} fil(er)".format(len(group["items"])),
                         "ca {}".format(_fmt_gb(group["bytes"]))))
    return SEP.join((group["key"], "täcker {} %".format(int(round(group["coverage"] * 100))),
                     "ca {}".format(_fmt_gb(group["bytes"]))))


def _search_groups(aoi, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES):
    """Allt som täcker området: höjd, karta och vektor först, sedan ortofoton."""
    wgs = aoi.projectAs(arcpy.SpatialReference(WGS84_WKID)).extent
    bbox = (wgs.XMin, wgs.YMin, wgs.XMax, wgs.YMax)
    groups = []
    dtm = _dtm_group(_stac_search(bbox, timeout, retries, STAC_HOJD_URL, [DTM_COLLECTION]), aoi)
    if dtm:
        groups.append(dtm)
    groups += _map_groups(_stac_search(bbox, timeout, retries, STAC_KARTA_URL, [MAP_COLLECTION]), aoi)
    groups += _vector_groups(
        _stac_search(bbox, timeout, retries, STAC_VEKTOR_URL, list(VECTOR_COLLECTIONS)), aoi)
    return groups + _groups(_stac_search(bbox, timeout, retries), aoi)


def _size_estimate(group, cell):
    """Ungefärlig storlek på resultatet i byte."""
    if group["kind"] == KIND_VECTOR:
        return group["bytes"]
    return group["bytes"] * (group["res"] / (cell or group["res"])) ** 2


def _key_of(value):
    """Radens identitet ur en listrad eller ett skriptvärde."""
    return SEP.join(p.strip() for p in str(value).split("|")[:3])


# Senaste sökningen i dialogen: nyckel -> (rader, felmeddelande). Modulnivå, inte
# på verktygsobjektet: updateParameters körs vid varje ändring i dialogen.
_DIALOG_SEARCH = {}


def _dialog_key(p):
    """Billig nyckel för området, så att sökningen bara görs om när det ändrats."""
    if p["aoi_mode"].valueAsText == AOI_EXTENT:
        text = p["aoi_extent"].valueAsText
        return ("extent", text) if text else None
    value = p["aoi"].value
    if value is None or not p["aoi"].valueAsText:
        return None
    try:
        desc = arcpy.Describe(value)
        fids = getattr(desc, "FIDSet", "") if _is_layer(value) else ""
        query = getattr(value, "definitionQuery", "") if _is_layer(value) else ""
        ext = desc.extent
        return ("aoi", p["aoi"].valueAsText, fids, query,
                "{} {} {} {}".format(ext.XMin, ext.YMin, ext.XMax, ext.YMax))
    except Exception:
        return None


def _dialog_search(p):
    """Sök en gång per område. Returnerar (rader, felmeddelande)."""
    key = _dialog_key(p)
    if key is None:
        return None, None
    if key not in _DIALOG_SEARCH:
        _DIALOG_SEARCH.clear()
        try:
            if p["aoi_mode"].valueAsText == AOI_EXTENT:
                aoi = _aoi_from_extent(p["aoi_extent"].value, p["aoi_extent"].valueAsText,
                                       _Silent())
            else:
                aoi = _aoi_geometry(p["aoi"].value, _Silent())
            groups = _search_groups(aoi, DIALOG_TIMEOUT, DIALOG_RETRIES)
            if groups:
                _DIALOG_SEARCH[key] = (groups, None)
            else:
                _DIALOG_SEARCH[key] = ([], "Katalogerna har inga data i området.")
        except ValueError as exc:
            _DIALOG_SEARCH[key] = ([], str(exc))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            _DIALOG_SEARCH[key] = ([], "Kunde inte söka i Lantmäteriets katalog: {}".format(exc))
    return _DIALOG_SEARCH[key]


# =============================================================================
# Rutnät och block
# =============================================================================

def _out_sr():
    """arcpy.env.outputCoordinateSystem om den är satt, annars filernas SWEREF 99 TM."""
    env_sr = arcpy.env.outputCoordinateSystem
    if env_sr is not None and (env_sr.factoryCode or env_sr.exportToString()):
        return env_sr
    return arcpy.SpatialReference(SWEREF99TM_WKID)


def _srs_text(sr):
    return "EPSG:{}".format(sr.factoryCode) if sr.factoryCode else sr.exportToString()


def _env_cell():
    """Ett tal i miljöinställningen Cell Size, annars None (MAXOF, MINOF, ett raster)."""
    try:
        cell = float(arcpy.env.cellSize)
    except (TypeError, ValueError):
        return None
    return cell if cell > 0 else None


def _snap_origin(sr, messages):
    """Rutnätets nollpunkt: Snap Raster om den finns och har samma koordinatsystem."""
    snap = arcpy.env.snapRaster
    if not snap:
        return 0.0, 0.0
    try:
        desc = arcpy.Describe(snap)
        if desc.spatialReference.exportToString() == sr.exportToString():
            return float(desc.extent.XMin), float(desc.extent.YMin)
    except Exception:
        pass
    messages.addWarningMessage("Snap Raster används inte: den saknas eller har ett annat "
                               "koordinatsystem än utdata.")
    return 0.0, 0.0


def _grid(aoi_out, cell, origin):
    """Rutnät som täcker området, med hörnen på hela celler från nollpunkten."""
    ext = aoi_out.extent
    ox, oy = origin
    x0 = ox + math.floor((ext.XMin - ox) / cell + 1e-6) * cell
    y0 = oy + math.floor((ext.YMin - oy) / cell + 1e-6) * cell
    nx = int(math.ceil((ext.XMax - x0) / cell - 1e-6))
    ny = int(math.ceil((ext.YMax - y0) / cell - 1e-6))
    return {"x0": x0, "y0": y0, "nx": nx, "ny": ny, "cell": cell}


def _blocks(grid, aoi_out, clip):
    """Rutnätet delat i block om högst BLOCK_PX. Block utanför polygonerna hoppas över."""
    cell = grid["cell"]
    out = []
    for iy in range(0, grid["ny"], BLOCK_PX):
        for ix in range(0, grid["nx"], BLOCK_PX):
            w = min(BLOCK_PX, grid["nx"] - ix)
            h = min(BLOCK_PX, grid["ny"] - iy)
            bx0 = grid["x0"] + ix * cell
            by0 = grid["y0"] + iy * cell
            bounds = (bx0, by0, bx0 + w * cell, by0 + h * cell)
            rect = _rect_polygon(*bounds, aoi_out.spatialReference)
            if clip and rect.disjoint(aoi_out):
                continue
            out.append({"bounds": bounds, "width": w, "height": h, "rect": rect})
    return out


def _resampling(cell, res, same_crs):
    """Närmaste granne bevarar pixlarna när inget behöver räknas om."""
    if cell > res * 1.001:
        return "average"
    if cell < res * 0.999 or not same_crs:
        return "cubic"
    return "near"


# =============================================================================
# Arbetsprocesser
# =============================================================================

def _python_exe():
    """python.exe i den aktiva miljön. I Pro är sys.executable ArcGISPro.exe."""
    for cand in (os.path.join(sys.exec_prefix, "python.exe"), sys.executable):
        if cand and os.path.basename(cand).lower() == "python.exe" and os.path.isfile(cand):
            return cand
    raise ValueError("Hittar inte python.exe i den aktiva miljön ({}).".format(sys.exec_prefix))


class _WorkerPool:
    """
    Ett fast antal ortofoto_worker-processer som tar ett block i taget. En
    läsartråd per process väntar på svar och lägger dem i en kö; trådarna gör
    bara I/O mot rören, aldrig GDAL eller arcpy.
    """

    def __init__(self, n, workdir):
        self._results = queue.Queue()
        self._procs = []
        self._logs = []
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        exe = _python_exe()
        script = os.path.join(_HERE, "ortofoto_worker.py")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        for i in range(n):
            log_path = os.path.join(workdir, "worker_{}.log".format(i))
            log = open(log_path, "w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(
                [exe, "-u", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=log, encoding="utf-8", errors="replace", creationflags=flags,
                env=env, cwd=workdir)
            self._procs.append(proc)
            self._logs.append((log, log_path))
            threading.Thread(target=self._reader, args=(i, proc), daemon=True).start()

    def __len__(self):
        return len(self._procs)

    def _reader(self, i, proc):
        for line in proc.stdout:
            if line.startswith(_ow.RESULT_PREFIX):
                self._results.put((i, json.loads(line[len(_ow.RESULT_PREFIX):])))
        self._results.put((i, None))  # processen har avslutats

    def submit(self, i, task):
        self._procs[i].stdin.write(json.dumps(task) + "\n")
        self._procs[i].stdin.flush()

    def get(self, timeout):
        """(processindex, svar) eller None vid timeout. Svar None = processen dog."""
        try:
            return self._results.get(timeout=timeout)
        except queue.Empty:
            return None

    def log_tail(self, i, n=15):
        log, path = self._logs[i]
        log.flush()
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-n:])
        except OSError:
            return ""

    def close(self, kill=False):
        for proc in self._procs:
            try:
                if kill:
                    proc.kill()
                else:
                    proc.stdin.close()
            except OSError:
                pass
        for proc in self._procs:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        for log, _path in self._logs:
            log.close()


def _reply(pool, i, res, group=None):
    """Felet i ett svar från en arbetsprocess som läsbart ValueError."""
    if res is None:
        raise ValueError("En arbetsprocess avslutades oväntat:\n{}".format(pool.log_tail(i)))
    if not res.get("ok"):
        if res.get("http") in (401, 403):
            raise ValueError(_access_error(res["http"], group))
        raise ValueError("Hämtningen misslyckades: {}".format(res.get("error")))


def _fetch_blocks(pool, tasks, token, steps, messages, group=None):
    """Kör uppgifterna över processerna. Returnerar filerna för block med data."""
    total = len(tasks)
    arcpy.SetProgressor("step", "Block 0 av {}".format(total), 0, total, 1)
    t0 = time.time()
    todo = list(reversed(tasks))
    running = 0
    done = 0
    files = []
    next_report = 0.1
    idle = list(range(len(pool)))
    while todo or running:
        while todo and idle:
            task = todo.pop()
            task["token"] = token.get()
            pool.submit(idle.pop(), task)
            running += 1
        if getattr(arcpy.env, "isCancelled", False):
            raise ValueError("Avbrutet.")
        got = pool.get(1.0)
        if got is None:
            continue
        i, res = got
        _reply(pool, i, res, group)
        running -= 1
        done += 1
        idle.append(i)
        if not res.get("empty"):
            files.append(tasks[res["id"]]["out"])
        left = (time.time() - t0) / done * (total - done)
        text = "Block {} av {}, ca {} kvar".format(done, total, _fmt_duration(left))
        arcpy.SetProgressorLabel("Steg {} av {}: {}".format(steps.k, steps.total, text))
        arcpy.SetProgressorPosition(done)
        if done / total >= next_report or done == total:
            messages.addMessage("    " + text)
            next_report = done / total + 0.1
    arcpy.SetProgressor("default", "Steg {} av {}".format(steps.k, steps.total))
    return files


# =============================================================================
# Utdata
# =============================================================================

def _is_network_path(path):
    """Sant om sökvägen ligger på en nätverksresurs (UNC eller mappad nätverksenhet)."""
    norm = (path or "").replace("/", "\\")
    if norm.startswith("\\\\"):
        return True
    drive = os.path.splitdrive(norm)[0]
    if len(drive) == 2 and drive[1] == ":":
        try:
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4   # DRIVE_REMOTE
        except Exception:
            return False
    return False


def _local_scratch_folder(messages):
    """arcpy.env.scratchFolder om den är lokal, annars systemets temp-mapp."""
    folder = arcpy.env.scratchFolder
    if folder and not _is_network_path(folder) and \
            not any(h in folder.lower() for h in _SYNC_HINTS):
        return folder
    fallback = tempfile.gettempdir()
    messages.addMessage(
        "    Scratch Workspace ligger på en nätverksresurs eller i en synkad mapp ({}). "
        "Arbetsfilerna läggs i {} i stället.".format(folder, fallback))
    return fallback


def _default_workspace():
    try:
        gdb = arcpy.mp.ArcGISProject("CURRENT").defaultGeodatabase
        if gdb:
            return gdb
    except Exception:
        pass
    return None


def _out_names(groups):
    """Namndelen per rad. Upplösningen tas med bara om två rader annars skulle krocka."""
    base = {}
    names = {}
    for g in groups:
        if g["kind"] == KIND_ORTHO:
            base.setdefault((g["year"], g["image"]), []).append(g)
        else:
            names[g["key"]] = g["name"]
    for (year, image), same in base.items():
        for g in same:
            name = "{}_{}".format(year, IMG_NAME[image])
            if len(same) > 1:
                name += "_{}cm".format(int(round(g["res"] * 100)))
            names[g["key"]] = name
    return names


def _out_path(workspace, prefix, name):
    full = "{}_{}".format(prefix, name)
    is_gdb = str(workspace).lower().endswith(".gdb")
    return os.path.join(workspace, full if is_gdb else full + ".tif")


def _norm_path(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _release_from_maps(paths, messages):
    """
    Ta bort lager som visar raster som ska skrivas över. Ett lager håller ett
    schemalås på rastret, och att skriva över det misslyckas då med ERROR 000464.
    """
    targets = {_norm_path(p) for p in paths}
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except Exception:
        return
    removed = 0
    for m in aprx.listMaps():
        for lyr in m.listLayers():
            try:
                if (lyr.supports("DATASOURCE") and not lyr.isGroupLayer
                        and _norm_path(lyr.dataSource) in targets):
                    m.removeLayer(lyr)
                    removed += 1
            except Exception:
                continue
    if removed:
        messages.addMessage("    {} äldre lager togs bort ur kartorna, de skrivs över.".format(removed))


def _write_output(local, target):
    """Den färdiga filen till utdataplatsen, i ett svep."""
    if str(os.path.dirname(target)).lower().endswith(".gdb"):
        # Miljöerna ska inte klippa eller projicera om det som redan är byggt.
        with arcpy.EnvManager(extent="MAXOF", snapRaster="", outputCoordinateSystem=None,
                              cellSize="MAXOF"):
            arcpy.management.CopyRaster(local, target, nodata_value=str(_ow.NODATA))
    else:
        if arcpy.Exists(target):
            arcpy.management.Delete(target)
        shutil.copy2(local, target)


def _write_metadata(path, group, cell):
    md = arcpy.metadata.Metadata(path)
    today = datetime.date.today().isoformat()
    title = group["title"]
    md.title = title[0].upper() + title[1:]
    md.summary = "Lantmäteriets {}, hämtat {}.".format(title, today)
    source = "{} fil(er): {}.".format(len(group["items"]),
                                      ", ".join(i["id"] for i in group["items"]))
    if group["kind"] == KIND_ORTHO:
        dates = sorted({i["datetime"][:10] for i in group["items"] if i["datetime"]})
        period = dates[0] if len(dates) == 1 else "{} - {}".format(dates[0], dates[-1]) if dates else ""
        md.description = (
            "<p>Bildtyp: {}. Filernas upplösning {} m, cellstorlek {} m. Fotodatum: {}.</p>"
            "<p>Källa: {}</p>".format(group["image"], _fmt_num(group["res"]), _fmt_num(cell),
                                      period, source))
    else:
        md.description = "<p>Källa: {}</p>".format(source)
    md.tags = "Lantmäteriet, {}".format(title)
    md.credits = "Källa: {}".format(ATTRIBUTION)
    md.accessConstraints = "Användningsvillkor: {}".format(group["product_url"])
    md.save()


def _download(url, target, token, label, messages):
    """
    En fil till disken. Avbrott fortsätter med Range från .part-filen, och storleken
    kontrolleras mot svaret. Omförsök med stigande väntan vid tillfälliga fel.
    """
    part = target + ".part"
    total = None
    delay = 2
    for attempt in range(HTTP_RETRIES + 2):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = {"Authorization": "Bearer " + token, "User-Agent": USER_AGENT}
        if have:
            headers["Range"] = "bytes={}-".format(have)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        timeout=HTTP_TIMEOUT) as resp:
                # Utan 206 skickar servern hela filen: börja om.
                mode = "ab" if have and resp.status == 206 else "wb"
                if mode == "wb":
                    have = 0
                length = resp.headers.get("Content-Length")
                total = have + int(length) if length else None
                with open(part, mode) as fh:
                    while True:
                        chunk = resp.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
                        if total:
                            arcpy.SetProgressorLabel("{}: {} av {}".format(
                                label, _fmt_gb(have), _fmt_gb(total)))
                        if getattr(arcpy.env, "isCancelled", False):
                            raise ValueError("Avbrutet.")
            if total is None or os.path.getsize(part) == total:
                os.replace(part, target)
                return
            raise OSError("ofullständig fil")
        except urllib.error.HTTPError as exc:
            if not (exc.code in (408, 429) or exc.code >= 500):
                raise
            if attempt == HTTP_RETRIES + 1:
                raise
        except (urllib.error.URLError, OSError):
            if attempt == HTTP_RETRIES + 1:
                raise
        messages.addMessage("    Nytt försök om {} s.".format(delay))
        time.sleep(delay)
        delay *= 2


def _gpkg_layers(path):
    """(namn utan main., sökväg) för featureklasser i en GeoPackage."""
    layers = []
    for dirpath, _dirs, names in arcpy.da.Walk(path, datatype="FeatureClass"):
        for name in names:
            layers.append((name.split(".")[-1], os.path.join(dirpath, name)))
    return layers


def _fetch_vector(group, aoi, workspace, prefix, token, workdir, messages):
    """
    Hämta filerna, klipp varje lager till området och skriv ett lager per
    featureklass. Mellanled i memory (lokalt, lämnar inget efter sig), resultatet
    skrivs med en ExportFeatures, så att även en geodatabas på filservern går fort.
    """
    is_gdb = str(workspace).lower().endswith(".gdb")
    pieces = {}
    temp = []
    extracted = []
    try:
        for n, item in enumerate(group["items"], 1):
            label = "{}, fil {} av {}".format(group["title"], n, len(group["items"]))
            arcpy.SetProgressorLabel(label)
            messages.addMessage("    {}: {} ({})".format(label, item["title"],
                                                         _fmt_gb(item["size"])))
            folder = os.path.join(workdir, "{}_{}".format(group["name"], n))
            os.makedirs(folder, exist_ok=True)
            archive = os.path.join(folder, "data.zip")
            try:
                _download(item["url"], archive, token, label, messages)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise ValueError(_access_error(exc.code, group))
                raise
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(folder)
            os.remove(archive)
            gpkgs = [os.path.join(d, f) for d, _s, fs in os.walk(folder) for f in fs
                     if f.lower().endswith(".gpkg")]
            if not gpkgs:
                raise ValueError("{} innehöll ingen GeoPackage.".format(item["id"]))
            extracted += gpkgs
            for gpkg in gpkgs:
                for layer, path in _gpkg_layers(gpkg):
                    clipped = "memory/klipp_{}".format(uuid.uuid4().hex[:12])
                    arcpy.analysis.PairwiseClip(path, aoi, clipped)
                    temp.append(clipped)
                    if int(arcpy.management.GetCount(clipped)[0]):
                        pieces.setdefault(layer, []).append(clipped)
        if not pieces:
            messages.addWarningMessage("{} har inga objekt i området.".format(group["title"]))
            return []

        outputs = []
        for layer, fcs in sorted(pieces.items()):
            name = arcpy.ValidateTableName("{}_{}".format(prefix, layer), workspace)
            target = os.path.join(workspace, name if is_gdb else name + ".shp")
            if arcpy.Exists(target) and not arcpy.env.overwriteOutput:
                raise ValueError("Finns redan: {}.".format(target))
            source = fcs[0]
            if len(fcs) > 1:
                source = "memory/samlat_{}".format(uuid.uuid4().hex[:12])
                arcpy.management.Merge(fcs, source)
                temp.append(source)
            if arcpy.Exists(target):
                _release_from_maps([target], messages)
            with arcpy.EnvManager(extent="MAXOF"):
                if is_gdb:
                    arcpy.conversion.ExportFeatures(source, target)
                else:
                    # GeoPackage ger 64-bitars ObjectID, som ExportFeatures inte kan
                    # skriva till en shapefile (ERROR 002809).
                    arcpy.management.CopyFeatures(source, target)
            messages.addMessage("    {}: {} objekt.".format(
                os.path.basename(target), arcpy.management.GetCount(target)[0]))
            outputs.append(target)
        return outputs
    finally:
        for fc in temp:
            try:
                arcpy.management.Delete(fc)
            except Exception:
                pass
        for gpkg in extracted:
            try:
                arcpy.management.ClearWorkspaceCache(gpkg)
            except Exception:
                pass


def _add_to_map(paths, messages):
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except Exception:
        return
    m = aprx.activeMap
    if m is None:
        messages.addWarningMessage("Ingen aktiv karta - rastren läggs inte till.")
        return
    for p in paths:
        m.addDataFromPath(p)


def _write_tool_metadata(tool_cls, toolbox_alias):
    """
    Skriv verktygets metadatafil med parameterförklaringar från TOOLTIPS.

    Pro läser verktygstipsen i dialogen från <verktygslåda>.<verktyg>.pyt.xml
    (elementet dialogReference per parameter). Det finns inget attribut på
    arcpy.Parameter för detta. Filen skrivs bara om innehållet har ändrats.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    toolbox = os.path.splitext(os.path.basename(__file__))[0]
    path = os.path.join(here, "{}.{}.pyt.xml".format(toolbox, tool_cls.__name__))

    def html(text):
        body = escape(text).replace("\n", "</SPAN></P><P><SPAN>")
        return escape('<DIV STYLE="text-align:Left;"><P><SPAN>{}</SPAN></P></DIV>'.format(body))

    tool = tool_cls()
    params = []
    for p in tool.getParameterInfo():
        tip = TOOLTIPS.get(p.name)
        if not tip:
            continue
        params.append(
            '<param name="{n}" displayname="{d}" type="{t}" direction="{r}">'
            "<dialogReference>{h}</dialogReference>"
            "<pythonReference>{h}</pythonReference></param>".format(
                n=p.name, d=escape(p.displayName, {'"': "&quot;"}),
                t=p.parameterType, r=p.direction, h=html(tip))
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metadata xml:lang="sv"><Esri><ArcGISFormat>1.0</ArcGISFormat></Esri>'
        '<tool name="{name}" displayname="{label}" toolboxalias="{alias}" xmlns="">'
        "<parameters>{params}</parameters><summary>{summary}</summary></tool>"
        "<dataIdInfo><idCitation><resTitle>{label}</resTitle></idCitation>"
        "<idAbs>{summary}</idAbs></dataIdInfo></metadata>\n"
    ).format(name=tool_cls.__name__, label=escape(tool.label), alias=toolbox_alias,
             params="".join(params), summary=html(TOOL_SUMMARY))

    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == xml:
                return
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)
    except OSError:
        # Skrivskyddad plats: verktyget fungerar ändå, bara utan verktygstips.
        pass


# =============================================================================
# Toolbox
# =============================================================================

class Toolbox:
    def __init__(self):
        self.label = "Lantmäteriet: hämta geodata"
        self.alias = "hamta_ortofoton"
        self.tools = [HamtaOrtofoton]
        _write_tool_metadata(HamtaOrtofoton, self.alias)


class HamtaOrtofoton:
    def __init__(self):
        self.label = "Hämta geodata"
        self.description = TOOL_SUMMARY + (
            " Kräver en consumer key och secret från Lantmäteriets API-portal med behörighet "
            "till katalogerna och de produkter som hämtas."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p_mode = arcpy.Parameter(
            displayName="Avgränsa området med", name="aoi_mode", datatype="GPString",
            parameterType="Required", direction="Input",
        )
        p_mode.filter.type = "ValueList"
        p_mode.filter.list = [AOI_POLYGONS, AOI_EXTENT]
        p_mode.value = AOI_POLYGONS

        # Båda är Optional i ramverket; updateMessages kräver den som valts.
        p_aoi = arcpy.Parameter(
            displayName="Intresseområde (polygoner)", name="aoi",
            datatype="GPFeatureRecordSetLayer", parameterType="Optional", direction="Input",
        )
        p_aoi.filter.list = ["Polygon"]
        p_aoi.value = _polygon_schema()

        p_extent = arcpy.Parameter(
            displayName="Utbredning", name="aoi_extent", datatype="GPExtent",
            parameterType="Optional", direction="Input",
        )
        p_extent.enabled = False

        p_search = arcpy.Parameter(
            displayName="Sök tillgängliga data", name="search", datatype="GPBoolean",
            parameterType="Optional", direction="Input",
        )
        p_search.value = False

        p_sel = arcpy.Parameter(
            displayName="Data att hämta", name="selection", datatype="GPString",
            parameterType="Required", direction="Input", multiValue=True,
        )

        p_ws = arcpy.Parameter(
            displayName="Utdata-arbetsyta", name="out_workspace", datatype="DEWorkspace",
            parameterType="Required", direction="Input",
        )
        default_ws = _default_workspace()
        if default_ws:
            p_ws.value = default_ws

        p_prefix = arcpy.Parameter(
            displayName="Namnprefix", name="prefix", datatype="GPString",
            parameterType="Required", direction="Input",
        )
        p_prefix.value = "lm"

        p_cell = arcpy.Parameter(
            displayName="Cellstorlek (m)", name="cell_size", datatype="GPDouble",
            parameterType="Optional", direction="Input",
        )

        p_key = arcpy.Parameter(
            displayName="Consumer key", name="consumer_key", datatype="GPString",
            parameterType="Optional", direction="Input", category="Inloggning",
        )
        p_key.value = _saved_login()[0] or None
        p_secret = arcpy.Parameter(
            displayName="Consumer secret", name="consumer_secret", datatype="GPStringHidden",
            parameterType="Optional", direction="Input", category="Inloggning",
        )

        p_save = arcpy.Parameter(
            displayName="Spara nyckeln på den här datorn", name="save_login", datatype="GPBoolean",
            parameterType="Optional", direction="Input", category="Inloggning",
        )
        p_save.value = False

        p_out = arcpy.Parameter(
            displayName="Hämtade data", name="out_rasters", datatype="DEDatasetType",
            parameterType="Derived", direction="Output", multiValue=True,
        )

        return [p_mode, p_aoi, p_extent, p_search, p_sel, p_ws, p_prefix, p_cell, p_key, p_secret,
                p_save, p_out]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        p = {q.name: q for q in parameters}
        by_extent = p["aoi_mode"].valueAsText == AOI_EXTENT
        p["aoi"].enabled = not by_extent
        p["aoi_extent"].enabled = by_extent
        if not p["search"].value:
            return
        groups, _error = _dialog_search(p)
        if not groups:
            return
        labels = {g["key"]: _label(g) for g in groups}
        # Behåll valda rader: samma år och bildtyp kan ha ny täckning med ett nytt
        # område, och ett skript skickar bara radens tre första fält. Ett värde
        # som inte finns i området ligger kvar, så att körningen kan säga vilket.
        chosen = [str(v) for v in (p["selection"].values or [])]
        kept = [labels.get(_key_of(v), v) for v in chosen]
        p["selection"].filter.list = list(labels.values()) + [v for v in kept
                                                              if v not in labels.values()]
        if kept != chosen:
            p["selection"].values = kept

    def updateMessages(self, parameters):
        p = {q.name: q for q in parameters}
        _check_login_fields(p)

        if p["aoi_mode"].valueAsText == AOI_EXTENT:
            if not p["aoi_extent"].valueAsText:
                p["aoi_extent"].setErrorMessage("Ange en utbredning.")
        elif not p["aoi"].valueAsText:
            p["aoi"].setErrorMessage(AOI_EMPTY)
        elif not _is_layer(p["aoi"].value) and _has_features(p["aoi"].value) is False:
            # Tom polygonmall eller tom featureklass. Ett valt lager kontrolleras
            # först vid körningen: det kan vara en tjänst.
            p["aoi"].setErrorMessage(AOI_EMPTY)

        ws = p["out_workspace"].valueAsText
        prefix = (p["prefix"].valueAsText or "").strip()
        if prefix and not (prefix[0].isalpha() and all(c.isalnum() or c == "_" for c in prefix)):
            p["prefix"].setErrorMessage(
                "Prefixet får bara innehålla bokstäver, siffror och understreck, och "
                "måste börja med en bokstav."
            )

        if ws and any(h in ws.lower() for h in _SYNC_HINTS):
            p["out_workspace"].setWarningMessage(
                "Platsen ser ut att synkas till molnet. Ortofoton blir snabbt stora och skulle "
                "då laddas upp."
            )

        cell = p["cell_size"].value
        if cell is not None and not (0.1 <= cell <= 100):
            p["cell_size"].setErrorMessage("Cellstorleken ska vara mellan 0,1 och 100 m.")

        groups, error = _dialog_search(p) if p["search"].value else (None, None)
        if error:
            p["search"].setErrorMessage(error)
        if not groups:
            return

        by_key = {g["key"]: g for g in groups}
        chosen = [by_key[k] for k in (_key_of(v) for v in (p["selection"].values or []))
                  if k in by_key]
        partial = [g["key"] for g in chosen if g["coverage"] < FULL_COVERAGE]
        notes = []
        if partial:
            notes.append("Täcker bara en del av området: {}.".format(", ".join(partial)))
        coarse = [g["key"] for g in chosen
                  if cell and g["kind"] in (KIND_ORTHO, KIND_DTM) and cell < g["res"] * 0.999]
        if coarse:
            notes.append("Cellstorleken är finare än filerna och ger ingen ny information: "
                         "{}.".format(", ".join(coarse)))
        total = sum(_size_estimate(g, cell) for g in chosen)
        if total > LARGE_GB * 1e9:
            notes.append("Det blir ungefär {}. En större cellstorlek ger mindre filer.".format(
                _fmt_gb(total)))
        if notes:
            p["selection"].setWarningMessage(" ".join(notes))

        if prefix and ws and chosen:
            names = _out_names(chosen)
            existing = [_out_path(ws, prefix, names[g["key"]]) for g in chosen
                        if g["kind"] != KIND_VECTOR]
            existing = [os.path.basename(e) for e in existing if arcpy.Exists(e)]
            if existing and arcpy.env.overwriteOutput:
                p["prefix"].setWarningMessage("Skrivs över: " + ", ".join(existing))
            elif existing:
                p["prefix"].setErrorMessage("Finns redan: " + ", ".join(existing))

    def execute(self, parameters, messages):
        p = {q.name: q for q in parameters}
        try:
            by_extent = p["aoi_mode"].valueAsText == AOI_EXTENT
            if by_extent:
                if not p["aoi_extent"].valueAsText:
                    raise ValueError("Ange en utbredning.")
                aoi = _aoi_from_extent(p["aoi_extent"].value, p["aoi_extent"].valueAsText,
                                       messages)
            else:
                if not p["aoi"].valueAsText:
                    raise ValueError(AOI_EMPTY)
                aoi = _aoi_geometry(p["aoi"].value, messages)
            key, secret = _login(p)
            if p["save_login"].value:
                _remember_login(key, secret, messages)
            outputs = _run(
                aoi,
                not by_extent,
                [str(v) for v in (p["selection"].values or [])],
                p["out_workspace"].valueAsText,
                p["prefix"].valueAsText.strip(),
                p["cell_size"].value or None,
                key,
                secret,
                messages,
            )
        except ValueError as exc:
            error = str(exc)
        else:
            error = None
        # Utanför except-blocket, så att Pro inte skriver ut hela kedjan av
        # undantag under det läsbara felmeddelandet.
        if error:
            messages.addErrorMessage(error)
            raise arcpy.ExecuteError

        arcpy.SetParameterAsText([q.name for q in parameters].index("out_rasters"),
                                 ";".join(outputs))

    def postExecute(self, parameters):
        return


# =============================================================================
# Körningens innehåll (separat funktion - går att testa utanför Pro)
# =============================================================================

def _run(aoi, clip, selection, workspace, prefix, cell, key, secret, messages):
    """
    aoi: polygon i SWEREF 99 TM (från _aoi_geometry eller _aoi_from_extent).
    clip: klipp till polygonerna (annars rektangeln runt dem).
    selection: listrader eller deras tre första fält.
    cell: cellstorlek i meter, None = filernas upplösning.
    """
    chosen = list(dict.fromkeys(_key_of(v) for v in selection))
    if not chosen:
        raise ValueError("Välj minst en rad att hämta.")

    steps = _Steps(2 + len(chosen) + 1, messages)
    workdir = None
    pool = None
    outputs = []
    try:
        steps.next("Söker i Lantmäteriets kataloger")
        groups = {g["key"]: g for g in _search_groups(aoi)}
        missing = [k for k in chosen if k not in groups]
        if missing:
            raise ValueError("Finns inte i området: {}.\nTillgängligt:\n{}".format(
                ", ".join(missing), "\n".join(_label(g) for g in groups.values())
                or "inga data"))
        chosen = [groups[k] for k in chosen]
        for g in chosen:
            messages.addMessage("    " + _label(g))
            if g["coverage"] < FULL_COVERAGE:
                messages.addWarningMessage(
                    "{} täcker {} % av området. Resten blir NoData.".format(
                        g["key"], int(round(g["coverage"] * 100))))
        steps.done()

        names = _out_names(chosen)
        targets = {g["key"]: _out_path(workspace, prefix, names[g["key"]]) for g in chosen
                   if g["kind"] != KIND_VECTOR}
        existing = [t for t in targets.values() if arcpy.Exists(t)]
        if existing and not arcpy.env.overwriteOutput:
            raise ValueError("Finns redan: {}. Slå på att skriva över utdata eller byt "
                             "namnprefix.".format(", ".join(existing)))

        sr = _out_sr()
        same_crs = sr.factoryCode == SWEREF99TM_WKID
        aoi_out = aoi if same_crs else aoi.projectAs(sr)
        if aoi_out is None or aoi_out.area <= 0:
            raise ValueError("Området kunde inte omvandlas till {}.".format(sr.name))
        cell = cell or _env_cell()
        origin = _snap_origin(sr, messages)
        messages.addMessage("Utdata i {}.".format(sr.name))

        steps.next("Kontrollerar nyckeln")
        token = _Token(key, secret)
        # Varje produkt beställs för sig: stoppa innan något hämtas.
        for g in {g["product_url"] + g["key"].split(SEP)[1]: g for g in chosen}.values():
            _check_access(g, token.get())
        steps.done()

        workdir = tempfile.mkdtemp(prefix="ortofoto_", dir=_local_scratch_folder(messages))
        need = sum(_size_estimate(g, cell) * (UNZIP_FACTOR if g["kind"] == KIND_VECTOR else 1)
                   for g in chosen) * DISK_FACTOR
        free = shutil.disk_usage(workdir).free
        if need > free:
            raise ValueError("För lite ledigt diskutrymme i {}: behöver ungefär {}, ledigt {}. "
                             "Välj en större cellstorlek eller ett mindre område.".format(
                                 workdir, _fmt_gb(need), _fmt_gb(free)))
        cutline = None
        if clip:
            cutline = os.path.join(workdir, "cutline.wkt")
            with open(cutline, "w", encoding="utf-8") as fh:
                fh.write(aoi_out.WKT)

        pool = _WorkerPool(MAX_WORKERS, workdir)
        for g in chosen:
            steps.next("Hämtar {}".format(g["key"]))
            if g["kind"] == KIND_VECTOR:
                clip_geom = aoi if clip else _rect_polygon(
                    aoi.extent.XMin, aoi.extent.YMin, aoi.extent.XMax, aoi.extent.YMax,
                    aoi.spatialReference)
                written = _fetch_vector(g, clip_geom, workspace, prefix, token.get(), workdir,
                                        messages)
                for path in written:
                    try:
                        _write_metadata(path, g, None)
                    except Exception as exc:
                        messages.addWarningMessage("Metadata kunde inte skrivas: {}".format(exc))
                outputs += written
                steps.done("{} lager".format(len(written)))
                continue
            g_cell = cell or g["res"]
            is_float = g["kind"] == KIND_DTM
            grid = _grid(aoi_out, g_cell, origin)
            messages.addMessage("    {} x {} pixlar, cellstorlek {} m.".format(
                grid["nx"], grid["ny"], _fmt_num(g_cell)))
            footprints = [(i, None if i["fp"] is None else i["fp"] if same_crs
                           else i["fp"].projectAs(sr)) for i in g["items"]]
            method = "bilinear" if g["kind"] == KIND_MAP else _resampling(g_cell, g["res"], same_crs)
            if is_float and method == "cubic":
                method = "bilinear"

            tasks = []
            for blk in _blocks(grid, aoi_out, clip):
                sources = [{"url": i["url"], "bands": i["bands"]} for i, fp in footprints
                           if fp is None or not fp.disjoint(blk["rect"])]
                if not sources:
                    continue
                tasks.append({
                    "kind": "block", "id": len(tasks), "sources": sources,
                    "bounds": blk["bounds"], "width": blk["width"], "height": blk["height"],
                    "srs": _srs_text(sr), "resampling": method, "cutline": cutline,
                    "float": is_float,
                    "out": os.path.join(workdir, "{}_{}.tif".format(names[g["key"]], len(tasks))),
                })
            files = _fetch_blocks(pool, tasks, token, steps, messages, g)
            if not files:
                raise ValueError("{} gav ingen bild i området.".format(g["key"]))

            arcpy.SetProgressorLabel("Steg {} av {}: sätter ihop och bygger översikter".format(
                steps.k, steps.total))
            messages.addMessage("    Sätter ihop {} block och bygger översikter.".format(len(files)))
            local = os.path.join(workdir, names[g["key"]] + ".tif")
            pool.submit(0, {"kind": "assemble", "blocks": files, "out": local, "cell": g_cell,
                            "float": is_float,
                            "bounds": [grid["x0"], grid["y0"], grid["x0"] + grid["nx"] * g_cell,
                                       grid["y0"] + grid["ny"] * g_cell]})
            while True:
                got = pool.get(1.0)
                if got is not None:
                    break
                if getattr(arcpy.env, "isCancelled", False):
                    raise ValueError("Avbrutet.")
            _reply(pool, got[0], got[1], g)
            for f in files:
                os.remove(f)

            arcpy.SetProgressorLabel("Steg {} av {}: skriver {}".format(
                steps.k, steps.total, os.path.basename(targets[g["key"]])))
            size = os.path.getsize(local)
            if arcpy.Exists(targets[g["key"]]):
                _release_from_maps([targets[g["key"]]], messages)
            _write_output(local, targets[g["key"]])
            try:
                _write_metadata(targets[g["key"]], g, g_cell)
            except Exception as exc:
                messages.addWarningMessage("Metadata kunde inte skrivas: {}".format(exc))
            os.remove(local)
            outputs.append(targets[g["key"]])
            steps.done("{}, {}".format(os.path.basename(targets[g["key"]]), _fmt_gb(size)))

        steps.next("Lägger till i kartan")
        _add_to_map(outputs, messages)
        steps.done()
        messages.addMessage("Källa: {}.".format(ATTRIBUTION))
    except urllib.error.HTTPError as exc:
        raise ValueError("Lantmäteriet svarade med HTTP {}: {}".format(exc.code, exc.reason))
    except (urllib.error.URLError, OSError) as exc:
        raise ValueError("Nätverks- eller filfel: {}".format(exc))
    finally:
        if pool is not None:
            pool.close(kill=True)
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        arcpy.ResetProgressor()
    return outputs
