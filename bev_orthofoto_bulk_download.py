"""
BEV Orthofoto Bulk-Download - QGIS Processing Tool
====================================================
Lädt für ein gewähltes Gebiet Orthofotos aus dem bundesweiten BEV-
Datenkatalog (data.bev.gv.at) - deckt ganz Österreich ab.

Standardeinstellung: Orthofoto RGBI (Echtfarben+Infrarot, 20cm) - liegt
nicht auf einem festen Gitter, sondern in unregelmäßigen Befliegungs-
"Operaten". Deckt laut BEV in Summe ganz Österreich ab, allerdings mit
unterschiedlichen Befliegungsjahren je nach Operat (rollierender
3-Jahres-Zyklus). Wird komplett per Katalogsuche gefunden: Volltextsuche
nach dem Produktnamen, dann lokale Überschneidungsprüfung der
zurückgegebenen Ausdehnungen gegen die AOI. RGB und Infrarot kommen als
ZWEI getrennte Dateien - der Infrarot-Kanal ist per Häkchen abschaltbar,
auf Wunsch werden beide zu einem 4-Kanal-Stack kombiniert.

Erweiterte Einstellungen:

Zusätzlich Orthofoto RGB - als Alternative Quelle für den Orthofoto-
Download. Liegt auf einem festen 50×50-km-Gitter in EPSG:3035 - die
Kachel-ID wird direkt aus den AOI-Koordinaten berechnet. Kein Infrarot-
Kanal verfügbar. Die Veröffentlichungstermine sind unregelmäßig über das
ganze Jahr verteilt - deshalb wird der aktuelle Download-Link je Kachel
per CSW-Katalogsuche ermittelt.

Ziel-CRS - Es lässt sich auch ein Ziel-CRS für alle Ergebnisse festlegen -
da dafür virtuell umprojiziert wird, kann das die Verarbeitungszeit
spürbar erhöhen.

Beide Produkte lesen nur den benötigten Ausschnitt per HTTP-Range direkt aus
der Cloud-optimierten GeoTIFF, statt die komplette (bei 20cm Auflösung auf
50×50 km sehr große) Datei herunterzuladen. Jede betroffene Kachel/jedes
Operat wird einzeln heruntergeladen (mit eigener Fenster-Lesen-/
Volldownload-Absicherung); mehrere Kacheln werden anschließend per VRT
zusammengeführt - die einzelnen Original-Kachelstücke bleiben dabei
unangetastet als eigene Dateien erhalten, das VRT ist nur eine Ansicht
darüber. Das gilt auch für den 4-Kanal-Stack und eine eventuelle
Umprojektion - beides liefert ebenfalls ein VRT, nirgends entsteht eine
materialisierte Kopie der Pixel.

Ergebnisdateien werden automatisch nach dem Jahr des Befliegungszeitpunkts
benannt (laut Katalog-Metadaten), und erhalten interne Pyramiden
(Übersichtsebenen) für schnelleres Anzeigen in QGIS - beides optional
abschaltbar.
"""

import math
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFolderDestination,
)
from osgeo import gdal, osr

# GDAL >= 3.7 warnt, wenn weder UseExceptions() noch DontUseExceptions()
# explizit aufgerufen wurde - ab GDAL 4.0 werden Exceptions Standard sein.
# Explizit aktivieren (wie in QGIS' eigenen gebuendelten GDAL-Algorithmen) -
# macht das Skript robust gegen Fehler in gdal.Open/Translate/Warp/BuildVRT
# unabhaengig davon, ob die aufgerufene GDAL-Version None oder eine
# Exception zurueckliefert.
gdal.UseExceptions()
osr.UseExceptions()

CSW_URL = "https://data.bev.gv.at/geonetwork/srv/eng/csw"
BEV_CRS = "EPSG:3035"
TILE_SIZE = 50000  # Meter, Kachelraster des DOP-Gitterprodukts in EPSG:3035

# Verlustfreie, maximal kompatible Kompression - DEFLATE ist der Standard,
# den praktisch jede GDAL-Installation unterstuetzt (im Gegensatz zu z.B.
# ZSTD oder LERC, die nicht ueberall verfuegbar sind). PREDICTOR=2
# (horizontale Differenzbildung) verbessert das Kompressionsverhaeltnis bei
# Bilddaten spuerbar, ohne verlustbehaftet zu sein. ZLEVEL=6 ist GDALs
# eigener Standardwert - guter, bewusst gewaehlter Mittelweg zwischen
# Geschwindigkeit und Dateigroesse (ZLEVEL=9 war in Tests spuerbar
# langsamer, fuer nur wenig kleinere Dateien).
TIF_CREATION_OPTIONS = ["COMPRESS=DEFLATE", "PREDICTOR=2", "ZLEVEL=6", "TILED=YES", "BIGTIFF=IF_SAFER"]

ISO_NS = {
    "gmd": "http://www.isotc211.org/2005/gmd",
    "gco": "http://www.isotc211.org/2005/gco",
}

# GML wird in ISO19139-Metadaten fuer den zeitlichen Erfassungszeitraum
# (Befliegungsdatum) verwendet - manche Kataloge nutzen GML 3.2, manche die
# aeltere GML-Namensraum-URI. Beide werden probiert.
GML_NAMESPACE_VARIANTS = ["http://www.opengis.net/gml/3.2", "http://www.opengis.net/gml"]


def extract_temporal(md):
    """Versucht, den Erfassungszeitraum (Befliegungsdatum) aus einem
    ISO19139-Metadatensatz zu lesen - als GML TimePeriod (von/bis) oder
    TimeInstant (ein einzelner Zeitpunkt). Gibt None zurueck, falls nichts
    gefunden wird - kein kritischer Fehler, viele Datensaetze haben das
    vielleicht schlicht nicht gepflegt."""
    for gml_ns in GML_NAMESPACE_VARIANTS:
        ns = {"gml": gml_ns}
        try:
            begin_el = md.find(".//gml:TimePeriod/gml:beginPosition", ns)
            end_el = md.find(".//gml:TimePeriod/gml:endPosition", ns)
            # WICHTIG: begin_el/end_el koennen als Element VORHANDEN sein,
            # aber ohne Textinhalt (z.B. bei einer Befliegung an einem
            # einzigen Tag, "Ende nicht angegeben") - dann nur das
            # tatsaechlich vorhandene Datum zeigen, statt "... bis None".
            b = begin_el.text if begin_el is not None else None
            e = end_el.text if end_el is not None else None
            if b and e:
                return f"{b} bis {e}"
            if b or e:
                return b or e
            instant_el = md.find(".//gml:TimeInstant/gml:timePosition", ns)
            if instant_el is not None and instant_el.text:
                return instant_el.text
        except Exception:
            continue
    return None


KNOWN_BEV_CRS_NAMES = {
    "ETRS89-extended / LAEA Europe": "3035",
    "ETRS89 / LAEA Europe": "3035",
}


def extract_year(temporal_str):
    """Extrahiert nur die Jahreszahl aus einem Befliegungszeitpunkt-String
    (z.B. '2022-01-28T00:00:00' -> '2022'). Gibt None zurueck, falls kein
    4-stelliges Jahr gefunden wird."""
    if not temporal_str:
        return None
    m = re.search(r"(19|20)\d{2}", temporal_str)
    return m.group(0) if m else None


def year_suffix(years):
    """Baut aus einer Menge von Jahreszahlen (manche Kacheln koennen
    unterschiedliche Befliegungsjahre haben) ein Dateinamen-Suffix - ein
    einzelnes Jahr, oder einen Bereich, falls mehrere Jahre vorkommen."""
    years = sorted({y for y in years if y})
    if not years:
        return ""
    if len(years) == 1:
        return f"_{years[0]}"
    return f"_{years[0]}-{years[-1]}"

# Warnschwelle, ab der VOR dem eigentlichen Download gewarnt wird.
WARN_AREA_KM2 = 300

# Grober, aber grosszuegiger Gueltigkeitsbereich fuer Oesterreich in
# EPSG:3035 - erkennt eine fehlgeschlagene/nicht durchgefuehrte
# CRS-Umrechnung, die sonst still falsche (unveraenderte) Koordinaten
# durchreichen wuerde.
AUSTRIA_3035_SANITY_BOUNDS = (4_000_000, 2_400_000, 5_200_000, 3_000_000)


def unique_path(path):
    """Haengt bei Bedarf _1, _2, ... an einen Dateinamen an, damit eine
    bereits vorhandene Datei nie ungefragt ueberschrieben wird - jeder Lauf
    bekommt garantiert eine eigene, frische Ausgabedatei."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{base}_{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def robust_replace(src, dst, feedback=None, cache_key="", retries=5, delay=1.5):
    """os.replace() mit Wiederholung bei transienten Windows-Dateisperren
    (WinError 32 'The process cannot access the file because it is being
    used by another process') - typischerweise ein kurz sperrender
    Virenscanner direkt nach dem Schreiben einer frischen Datei, kein
    dauerhaftes Problem.

    WICHTIG: Vor jedem (erneuten) Versuch pruefen, ob die Umbenennung
    tatsaechlich schon geklappt hat, obwohl ein vorheriger Versuch einen
    Fehler gemeldet hat - eine bekannte Windows-Eigenart, bei der
    os.replace() gelegentlich einen Fehler zurueckgibt, obwohl die
    Operation im Hintergrund trotzdem durchgegangen ist. Ohne diese Pruefung
    wuerde ein erneuter Versuch dann faelschlich mit 'Quelldatei nicht
    gefunden' scheitern, weil sie ja bereits erfolgreich verschoben wurde."""
    for attempt in range(retries):
        if not os.path.exists(src) and os.path.exists(dst):
            return True
        try:
            os.replace(src, dst)
            if attempt > 0 and feedback is not None:
                feedback.pushInfo(f"{cache_key}: Sperre gelöst, Umbenennen erfolgreich.")
            return True
        except (PermissionError, FileNotFoundError) as e:
            if os.path.exists(dst) and not os.path.exists(src):
                if feedback is not None:
                    feedback.pushInfo(f"{cache_key}: Sperre gelöst, Umbenennen war bereits erfolgreich.")
                return True  # vorheriger Versuch war doch erfolgreich
            if isinstance(e, FileNotFoundError):
                # Kein Sperr-Problem, das sich durch Warten loesen wuerde -
                # Quelldatei ist tatsaechlich weg und Ziel existiert nicht.
                if feedback is not None:
                    feedback.pushWarning(f"{cache_key}: Quelldatei unerwartet nicht auffindbar ({e}) - gebe auf.")
                return False
            if attempt == retries - 1:
                if feedback is not None:
                    feedback.pushWarning(f"{cache_key}: Datei bleibt gesperrt ({e}) - gebe auf.")
                return False
            if feedback is not None:
                feedback.pushInfo(f"{cache_key}: Datei kurz gesperrt (Virenscanner?), versuche erneut ...")
            time.sleep(delay)
    return False


def looks_like_valid_3035_austria(bbox):
    ax0, ay0, ax1, ay1 = AUSTRIA_3035_SANITY_BOUNDS
    bx0, by0, bx1, by1 = bbox
    return not (bx1 < ax0 or bx0 > ax1 or by1 < ay0 or by0 > ay1)


def fetch_url_bytes(url, timeout=60):
    req = Request(url, headers={"User-Agent": "bev-orthofoto-tool/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def csw_search(search_text, feedback=None, max_records=200):
    """CSW-Volltextsuche (CQL_TEXT) gegen den BEV-Geonetwork-Katalog. Liefert
    pro Treffer Titel, geografische Ausdehnung (falls vorhanden, in
    EPSG:4326) und alle Online-Ressourcen-Links direkt mit (elementSetName=
    full spart einen zweiten Abruf pro Treffer).

    WICHTIG: diese Funktion ist der unsicherste Teil des ganzen Tools (siehe
    Modul-Docstring) - deshalb wird hier bewusst ausfuehrlich geloggt: die
    exakte Anfrage-URL (zum manuellen Nachvollziehen im Browser/curl), die
    Trefferzahl, und bei Null Treffern ein Ausschnitt der Roh-Antwort."""
    cql = f"AnyText like '%{search_text}%'"
    query = {
        "service": "CSW",
        "version": "2.0.2",
        "request": "GetRecords",
        "typeNames": "csw:Record",
        "resultType": "results",
        "maxRecords": str(max_records),
        "elementSetName": "full",
        "outputSchema": "http://www.isotc211.org/2005/gmd",
        "constraintLanguage": "CQL_TEXT",
        "constraint_language_version": "1.1.0",
        "constraint": cql,
    }
    url = f"{CSW_URL}?{urlencode(query)}"
    if feedback is not None:
        feedback.pushInfo(f"CSW-Suche: {url}")

    try:
        xml_bytes = fetch_url_bytes(url)
    except Exception as e:
        if feedback is not None:
            feedback.reportError(f"CSW-Anfrage fehlgeschlagen: {e}")
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        if feedback is not None:
            feedback.reportError(
                f"CSW-Antwort konnte nicht als XML gelesen werden ({e}). "
                f"Erste 500 Zeichen der Antwort: {xml_bytes[:500]!r}")
        return []

    records = []
    for md in root.iter("{http://www.isotc211.org/2005/gmd}MD_Metadata"):
        bbox = None
        west_el = md.find(".//gmd:EX_GeographicBoundingBox/gmd:westBoundLongitude/gco:Decimal", ISO_NS)
        east_el = md.find(".//gmd:EX_GeographicBoundingBox/gmd:eastBoundLongitude/gco:Decimal", ISO_NS)
        south_el = md.find(".//gmd:EX_GeographicBoundingBox/gmd:southBoundLatitude/gco:Decimal", ISO_NS)
        north_el = md.find(".//gmd:EX_GeographicBoundingBox/gmd:northBoundLatitude/gco:Decimal", ISO_NS)
        if all(e is not None for e in (west_el, east_el, south_el, north_el)):
            bbox = (float(west_el.text), float(south_el.text), float(east_el.text), float(north_el.text))

        links = []
        for res in md.iter("{http://www.isotc211.org/2005/gmd}CI_OnlineResource"):
            linkage_el = res.find("gmd:linkage/gmd:URL", ISO_NS)
            name_el = res.find("gmd:name/gco:CharacterString", ISO_NS)
            if linkage_el is not None and linkage_el.text:
                links.append((name_el.text if name_el is not None else "", linkage_el.text))

        title_el = md.find(".//gmd:title/gco:CharacterString", ISO_NS)
        records.append({
            "title": title_el.text if title_el is not None else "",
            "bbox": bbox,
            "links": links,
            "temporal": extract_temporal(md),
        })

    if feedback is not None:
        feedback.pushInfo(f"CSW-Suche '{search_text}': {len(records)} Record(s) gefunden.")
        if not records:
            preview = xml_bytes[:800].decode("utf-8", errors="replace")
            feedback.pushWarning(f"Keine Treffer - Antwort-Ausschnitt zur Diagnose:\n{preview}")

    return records


def bbox_overlap(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def tiles_for_bbox(bbox3035, tile_size=TILE_SIZE):
    """Liefert alle (n, e) Kachel-Ursprungskoordinaten des DOP-Gitters, die
    eine bbox in EPSG:3035 überschneiden - reine Arithmetik, da die
    Kachelnamen die Gitterkoordinaten direkt kodieren."""
    xmin, ymin, xmax, ymax = bbox3035
    e0 = math.floor(xmin / tile_size) * tile_size
    e1 = math.floor(xmax / tile_size) * tile_size
    n0 = math.floor(ymin / tile_size) * tile_size
    n1 = math.floor(ymax / tile_size) * tile_size
    tiles = []
    e = e0
    while e <= e1:
        n = n0
        while n <= n1:
            tiles.append((n, e))
            n += tile_size
        e += tile_size
    return tiles


def find_dop_tile_url(n, e, feedback=None):
    """Findet die aktuelle Download-URL fuer eine DOP-Gitterkachel per
    CSW-Volltextsuche nach ihrem eindeutigen Namen (kein Stichtag-Raten
    moeglich, siehe Modul-Docstring). Gibt (url, temporal) zurueck, wobei
    temporal der Befliegungszeitpunkt laut Katalog ist (oder None).

    WICHTIG: Die reine Gitter-Koordinate ("CRS3035RES50000mN...E...") allein
    ist NICHT eindeutig - dieselbe Koordinate steckt auch im Namen der
    Hoehendaten-Kacheln (ALS_DTM_.../ALS_DSM_...), die auf demselben Gitter
    liegen. Ein zu laxer Link-Filter hat deshalb im ersten Testlauf
    faelschlich eine Hoehendaten-Kachel statt eines Orthofotos geliefert.
    Deshalb hier den Suchtext selbst um das "DOP_"-Praefix ergaenzen, das
    nur echte Orthofoto-Dateien tragen."""
    search_text = f"DOP_CRS3035RES50000mN{n}E{e}"
    records = csw_search(search_text, feedback=feedback, max_records=5)

    all_tif_links = []
    for rec in records:
        for name, link in rec["links"]:
            low = link.lower()
            if low.endswith(".tif") and "thumbnail" not in low:
                all_tif_links.append((link, rec.get("temporal")))

    # Bevorzugt einen Link mit "dop" im Pfad - zur zusaetzlichen Absicherung
    # gegen die oben beschriebene Verwechslung, falls die praezisere Suche
    # doch mal einen fremden Treffer mitbringt.
    for link, temporal in all_tif_links:
        if "dop" in link.lower():
            if feedback is not None:
                feedback.pushInfo(
                    f"N{n}E{e}: Befliegungszeitpunkt laut Katalog: {temporal or 'nicht angegeben'}.")
            return link, temporal
    if all_tif_links:
        link, temporal = all_tif_links[0]
        if feedback is not None:
            feedback.pushWarning(
                f"N{n}E{e}: {len(all_tif_links)} .tif-Treffer gefunden, aber keiner enthält 'dop' im "
                f"Pfad - verwende trotzdem den ersten (bitte Ergebnis prüfen!): {link}")
            feedback.pushInfo(f"N{n}E{e}: Befliegungszeitpunkt laut Katalog: {temporal or 'nicht angegeben'}.")
        return link, temporal

    if feedback is not None and records:
        feedback.pushWarning(
            f"N{n}E{e}: {len(records)} Record(s) gefunden, aber kein Link endete auf '.tif'. "
            "Gefundene Records zur Diagnose:")
        for rec in records:
            feedback.pushInfo(f"  Titel: {rec['title']!r}")
            if not rec["links"]:
                feedback.pushInfo("    (keine Online-Ressourcen-Links in diesem Record)")
            for name, link in rec["links"]:
                feedback.pushInfo(f"    Link ({name!r}): {link}")
    return None, None


def find_rgbi_operate(aoi_bbox_4326, feedback=None):
    """Findet per Volltextsuche alle 'DOP RGBI Operat'-Datensaetze, deren
    Ausdehnung die AOI ueberschneidet. Liefert eine Liste von Dicts mit
    title, bbox, rgb_url, nir_url."""
    records = csw_search("Digitales Orthophoto Farbe und Infrarot", feedback=feedback, max_records=200)

    matches = []
    skipped_no_bbox = 0
    skipped_no_links = 0
    for rec in records:
        if "operat" not in rec["title"].lower():
            continue  # Serien-Sammelrecord ueberspringen, nur Einzel-Operate
        if not rec["bbox"]:
            skipped_no_bbox += 1
            continue
        if not bbox_overlap(rec["bbox"], aoi_bbox_4326):
            continue
        rgb_url = None
        nir_url = None
        for name, link in rec["links"]:
            low = link.lower()
            if not low.endswith(".tif"):
                continue
            if "nir" in low or "infrarot" in name.lower():
                nir_url = link
            elif "rgb" in low or "farbe" in name.lower():
                rgb_url = link
        if rgb_url:
            matches.append({"title": rec["title"], "bbox": rec["bbox"], "rgb_url": rgb_url, "nir_url": nir_url,
                             "temporal": rec.get("temporal")})
            if feedback is not None:
                feedback.pushInfo(
                    f"Operat '{rec['title']}': Befliegungszeitpunkt laut Katalog: "
                    f"{rec.get('temporal') or 'nicht angegeben'}.")
        else:  # bbox-Ueberschneidung ist an dieser Stelle bereits durch die obigen continues garantiert
            skipped_no_links += 1
            if feedback is not None:
                feedback.pushWarning(
                    f"Operat '{rec['title']}' überschneidet die AOI, aber kein Link passte auf "
                    "das RGB-Filter ('.tif' + 'rgb'/'farbe'). Gefundene Links zur Diagnose:")
                for name, link in rec["links"]:
                    feedback.pushInfo(f"    Link ({name!r}): {link}")

    if feedback is not None:
        feedback.pushInfo(
            f"RGBI: {len(matches)} Operat(e) überschneiden die AOI"
            + (f" ({skipped_no_bbox} Record(s) ohne Ausdehnung, {skipped_no_links} ohne passenden Link übersprungen)"
               if skipped_no_bbox or skipped_no_links else "")
            + (": " + ", ".join(m["title"] for m in matches) if matches else "."))
    return matches


def add_overviews(path, feedback=None):
    """Baut Pyramiden-Ebenen fuer schnelleres Anzeigen in QGIS - bei einer
    TIF-Datei direkt eingebettet, bei einem VRT als externe .vrt.ovr-
    Nebendatei (GDAL entscheidet das automatisch je nach Format). Kein
    kritischer Fehler, falls das fehlschlaegt - die Hauptdatei bleibt
    trotzdem vollstaendig nutzbar.

    WICHTIG: Dieser Schritt braucht bei grossen Dateien selbst spuerbar
    Zeit. Nutzt denselben GDAL-Callback wie die Fenster-Lese-Operationen,
    damit auch dieser Schritt abbrechbar ist und Fortschritt zeigt."""
    if feedback is not None:
        feedback.pushInfo(f"{os.path.basename(path)}: baue Pyramiden (Übersichtsebenen) ...")
    try:
        ds = gdal.Open(path, gdal.GA_Update)
        if ds is not None:
            ds.BuildOverviews("AVERAGE", [2, 4, 8, 16, 32],
                               callback=_gdal_cancel_callback(feedback, f"{os.path.basename(path)} (Pyramiden)"))
        ds = None
        return True
    except Exception as e:
        if feedback is not None:
            feedback.pushInfo(f"{os.path.basename(path)}: Pyramiden-Erstellung fehlgeschlagen ({e}) - nicht kritisch.")
        return False


class gdal_error_capture:
    """Context-Manager, der GDALs interne CPL-Fehler-/Warnmeldungen waehrend
    eines Aufrufs sammelt. Noetig, weil gdal.BuildVRT() bekanntermassen
    KEINE zuverlaessige Python-Exception wirft, selbst mit UseExceptions()
    aktiviert (bestaetigtes GDAL-Verhalten, siehe
    github.com/OSGeo/gdal/issues/4755) - z.B. eine nicht oeffenbare
    Quelldatei wird nur als Warnung ('Can't open X. Skipping it') gemeldet
    und dann einfach uebersprungen, ohne dass das im Python-Code sichtbar
    waere. Dieser Handler macht solche Meldungen endlich sichtbar."""

    def __init__(self):
        self.messages = []

    def _handler(self, err_class, err_num, err_msg):
        self.messages.append(err_msg)

    def __enter__(self):
        gdal.PushErrorHandler(self._handler)
        return self

    def __exit__(self, *exc_info):
        gdal.PopErrorHandler()


def build_verified_vrt(sources, out_dir, label, tile_crs, feedback=None, separate=False, dst_crs=None, retries=3):
    """Baut ein VRT (Mosaik, oder mit separate=True ein Baender-Stapel, oder
    mit dst_crs eine per Warp virtuell umprojizierte Fassung - ein "warped
    VRT") und verifiziert danach tatsaechliche EXISTENZ (nicht nur
    Oeffenbarkeit) - GDALs Python-Bindings koennen erfolgreich zurueckkehren,
    ohne dass die Datei tatsaechlich geschrieben wurde. Bei Fehlschlag wird
    mit einem frischen Dateinamen erneut versucht. Das VRT ist in JEDEM Fall
    das Endergebnis, nie eine materialisierte Kopie der Pixel - auch nicht
    bei einer Umprojektion. dst_crs sollte der einfache EPSG-Code sein
    (z.B. "EPSG:25833"), nicht die volle WKT-Beschreibung - letztere ist bei
    modernen PROJ-Versionen sehr lang/komplex und unnoetig."""
    vrt_ok = False
    vrt_path = None
    for attempt in range(retries):
        vrt_path = unique_path(os.path.join(out_dir, f"{label}.vrt"))
        with gdal_error_capture() as cap:
            if dst_crs is not None:
                gdal.Warp(vrt_path, sources, dstSRS=dst_crs, format="VRT", resampleAlg="cubic")
            else:
                gdal.BuildVRT(vrt_path, sources, options=gdal.BuildVRTOptions(outputSRS=tile_crs, separate=separate))
        if feedback is not None:
            for msg in cap.messages:
                feedback.pushWarning(f"{label}: GDAL meldete beim VRT-Bau: {msg}")
        if not os.path.exists(vrt_path):
            if feedback is not None:
                feedback.pushInfo(
                    f"{label}: VRT-Datei fehlt nach dem Bauen unerwartet - versuche mit neuem "
                    "Dateinamen erneut ...")
            time.sleep(2)
            continue
        for _ in range(3):
            try:
                check_ds = gdal.Open(vrt_path)
                if check_ds is not None:
                    check_ds = None
                    vrt_ok = True
                    break
            except Exception:
                pass
            if feedback is not None:
                feedback.pushInfo(f"{label}: VRT noch nicht sofort lesbar, versuche erneut ...")
            time.sleep(2)
        if vrt_ok:
            break
    if not vrt_ok:
        raise RuntimeError("VRT konnte trotz mehrerer Versuche nicht zuverlässig erstellt werden")
    if feedback is not None:
        feedback.pushInfo(f"{label}: VRT erstellt -> {vrt_path}")
    return vrt_path


def _gdal_cancel_callback(feedback, label=""):
    """GDAL bricht eine laufende Operation (Translate/Warp) sofort ab, wenn
    der Progress-Callback 0 zurueckgibt - das macht 'echtes' Abbrechen
    mitten in einem Fenster-Read moeglich, nicht nur zwischen Kacheln.
    Nutzt zusaetzlich 'complete' (GDALs eigener 0.0-1.0-Fortschritt fuer
    GENAU DIESE Operation), um bei einer einzelnen langen Operation (z.B.
    einer sehr grossen Kachel) sichtbaren Fortschritt zu zeigen, statt nur
    "irgendwas passiert" ueber viele Minuten hinweg - alle 10% eine
    Log-Zeile, um das Protokoll nicht zu uebefluten."""
    state = {"last_pct": -10}

    def _cb(complete, message, user_data):
        if feedback is not None:
            pct = int(complete * 100)
            if pct >= state["last_pct"] + 10:
                feedback.pushInfo(f"{label}: {pct}% ...")
                state["last_pct"] = pct
            if feedback.isCanceled():
                return 0
        return 1
    return _cb


def download_full_file(url, out_path, feedback=None):
    """Atomarer, gestreamter Volldownload mit Abbruch-Unterstuetzung pro
    Chunk - Rueckfallebene, falls der Fenster-Read fehlschlaegt."""
    tmp_path = out_path + ".part"
    for attempt in range(2):
        try:
            req = Request(url, headers={"User-Agent": "bev-orthofoto-tool/1.0"})
            with urlopen(req, timeout=600) as resp, open(tmp_path, "wb") as f:
                while True:
                    if feedback is not None and feedback.isCanceled():
                        raise RuntimeError("abgebrochen")
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            if not robust_replace(tmp_path, out_path, feedback=feedback, cache_key=os.path.basename(out_path)):
                continue
            return True
        except Exception:
            if feedback is not None and feedback.isCanceled():
                break
            continue
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass
    return False


def compute_src_window(gt, xsize_total, ysize_total, geo_window):
    """Berechnet ein Pixel-Fenster (xoff, yoff, xsize, ysize) direkt aus der
    echten Geotransform einer Datei, mit fester Begrenzung auf die
    tatsaechliche Rastergroesse - kann dadurch NIEMALS negative Breite/Hoehe
    liefern. Wird verwendet, weil sich gdal.Translate's projWin (rein
    geografische Koordinaten) bei manchen JPEG-komprimierten Cloud-GeoTIFFs
    (beobachtet bei Orthofotos, nicht bei den DEFLATE-komprimierten
    Hoehendaten) als unzuverlaessig herausgestellt hat - mit teils
    falsch berechneten (negativen) Fenstern trotz korrekter Koordinaten."""
    xmin, ymin, xmax, ymax = geo_window
    px_w, px_h = gt[1], gt[5]
    if px_h < 0:
        # Standard "Nord-oben"-Ausrichtung: y nimmt mit der Zeilennummer ab.
        yoff_f = (ymax - gt[3]) / px_h
        yend_f = (ymin - gt[3]) / px_h
    else:
        yoff_f = (ymin - gt[3]) / px_h
        yend_f = (ymax - gt[3]) / px_h
    xoff_f = (xmin - gt[0]) / px_w
    xend_f = (xmax - gt[0]) / px_w

    xoff = max(0, min(int(math.floor(xoff_f)), xsize_total))
    yoff = max(0, min(int(math.floor(yoff_f)), ysize_total))
    xend = max(0, min(int(math.ceil(xend_f)), xsize_total))
    yend = max(0, min(int(math.ceil(yend_f)), ysize_total))
    return xoff, yoff, max(0, xend - xoff), max(0, yend - yoff)


def _attempt_windowed_translate(src_path, tmp_out, out_path, geo_window, cache_key, feedback):
    """Ein einzelner Versuch, das Fenster zu lesen und bereitzustellen.
    Rueckgabe: out_path bei Erfolg, 'empty' wenn das Fenster wirklich leer
    ist (kein Sinn in Wiederholung), sonst None (Fehler, geloggt)."""
    try:
        src_ds = gdal.Open(src_path)
        gt = src_ds.GetGeoTransform()
        xsize_total, ysize_total = src_ds.RasterXSize, src_ds.RasterYSize
        src_ds = None
        xoff, yoff, xsize, ysize = compute_src_window(gt, xsize_total, ysize_total, geo_window)
        if xsize <= 0 or ysize <= 0:
            if feedback is not None:
                feedback.pushWarning(
                    f"{cache_key}: berechnetes Pixel-Fenster ist leer (xoff={xoff}, yoff={yoff}, "
                    f"xsize={xsize}, ysize={ysize}) - übersprungen.")
            return "empty"

        # WICHTIG: Manche Quellen (beobachtet bei den BEV-DOP-Gitterkacheln)
        # sind intern "Sueden-oben" ausgerichtet (positive Y-Aufloesung,
        # gt[5] > 0) statt der ueblichen "Norden-oben"-Konvention.
        # gdal.Translate mit srcWin kopiert diese Ausrichtung unveraendert
        # durch - unproblematisch fuer sich allein, aber gdalbuildvrt kann
        # solche Dateien beim spaeteren Zusammenfuehren nicht verarbeiten
        # ("does not support positive NS resolution", stillschweigend
        # uebersprungen). Deshalb in diesem Fall gdal.Warp statt
        # gdal.Translate verwenden - Warp resampled korrekt auf die
        # Standard-Ausrichtung, Translate kopiert nur unveraendert durch.
        if gt[5] > 0:
            if feedback is not None:
                feedback.pushInfo(
                    f"{cache_key}: Quelle ist 'Süden-oben' ausgerichtet - verwende Warp zur Normalisierung.")
            translate_options = gdal.WarpOptions(
                outputBounds=list(geo_window), format="GTiff",
                creationOptions=TIF_CREATION_OPTIONS,
                callback=_gdal_cancel_callback(feedback, cache_key))
            ds = gdal.Warp(tmp_out, src_path, options=translate_options)
        else:
            # Verlustfreie Kompression - siehe TIF_CREATION_OPTIONS-Definition
            # oben fuer die Begruendung der gewaehlten Werte.
            translate_options = gdal.TranslateOptions(
                srcWin=[xoff, yoff, xsize, ysize], format="GTiff",
                creationOptions=TIF_CREATION_OPTIONS,
                callback=_gdal_cancel_callback(feedback, cache_key))
            ds = gdal.Translate(tmp_out, src_path, options=translate_options)
        ok = ds is not None
        ds = None
        if not (ok and os.path.exists(tmp_out)):
            return None
        if not robust_replace(tmp_out, out_path, feedback=feedback, cache_key=cache_key):
            raise RuntimeError("Datei blieb nach Wiederholungsversuchen gesperrt")
        # Verifizieren, dass die fertige Datei tatsaechlich sofort wieder
        # oeffenbar ist, BEVOR wir sie als Erfolg zurueckgeben - bei sehr
        # grossen Dateien kann eine kurze Dateisystem-/Virenscanner-
        # Verzoegerung auftreten. Ein kurzer erneuter Oeffnungsversuch
        # faengt das ab.
        verify_ds = None
        for attempt in range(3):
            try:
                verify_ds = gdal.Open(out_path)
                if verify_ds is not None:
                    break
            except Exception:
                pass
            if feedback is not None:
                feedback.pushInfo(f"{cache_key}: Datei noch nicht sofort lesbar, versuche erneut ...")
            time.sleep(2)
        verify_ds = None
        return out_path
    except Exception as e:
        if feedback is not None:
            if feedback.isCanceled():
                feedback.pushInfo(f"{cache_key}: abgebrochen.")
            else:
                feedback.pushInfo(f"{cache_key}: Versuch meldete ({e}).")
        return None


def windowed_read(url_or_path, out_path, geo_window, cache_dir, cache_key, feedback=None, is_local=False,
                   max_attempts=4):
    """Liest nur das benoetigte Fenster - per HTTP-Range direkt aus der
    Remote-COG (is_local=False) oder aus einer bereits lokal vorliegenden
    Datei (is_local=True, Rueckfallebene nach Volldownload). geo_window ist
    (xmin, ymin, xmax, ymax) in der CRS der Quelldatei. Das Pixel-Fenster
    wird SELBST aus der echten Geotransform berechnet (siehe
    compute_src_window) statt gdal.Translate's projWin zu vertrauen.

    WICHTIG: Der Fenster-Lese-Versuch wird bis zu max_attempts mal komplett
    wiederholt, BEVOR auf die teure Rueckfallebene (kompletter Download)
    ausgewichen wird - beobachtete Fehlschlaege waren bisher ausschliesslich
    transiente Windows-Dateisperren (Virenscanner), die ein erneuter Versuch
    typischerweise von selbst loest. Das vermeidet unnoetige Volldownloads,
    v.a. bei RGBI-Operaten, die deutlich groesser als die Gitterkacheln sein
    koennen (laut BEV ca. 2300-5000 km² pro Operat).

    Baut absichtlich KEINE Pyramiden auf dem einzelnen Kachelstueck - bei
    mehreren Kacheln werden diese ohnehin gleich zu einem Mosaik
    zusammengefuehrt, wodurch Pyramiden auf den Einzelstuecken nutzlose
    Arbeit waeren. Pyramiden werden stattdessen einmalig auf der
    tatsaechlichen Enddatei gebaut (siehe build_vrt_and_warp)."""
    src_path = url_or_path if is_local else "/vsicurl/" + url_or_path
    tmp_out = out_path + ".part"

    for attempt in range(max_attempts):
        if feedback is not None and feedback.isCanceled():
            return None
        result = _attempt_windowed_translate(src_path, tmp_out, out_path, geo_window, cache_key, feedback)
        if result == "empty":
            return None
        if result:
            return result
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except OSError:
            pass
        if feedback is not None and feedback.isCanceled():
            return None
        if attempt < max_attempts - 1:
            if feedback is not None:
                feedback.pushInfo(f"{cache_key}: erneuter Versuch ({attempt + 2}/{max_attempts}) ...")
            time.sleep(2)

    if is_local:
        if feedback is not None:
            feedback.pushWarning(
                f"{cache_key}: Zuschneiden nach Volldownload trotz {max_attempts} Versuchen fehlgeschlagen.")
        return None

    if feedback is not None:
        feedback.pushWarning(
            f"{cache_key}: Fenster-Lesen trotz {max_attempts} Versuchen fehlgeschlagen - "
            "lade komplette Datei als Rückfallebene.")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{cache_key}_full.tif")
    if not os.path.exists(cache_path):
        if not download_full_file(url_or_path, cache_path, feedback=feedback):
            if feedback is not None:
                feedback.pushWarning(f"{cache_key}: Volldownload fehlgeschlagen.")
            return None
    if feedback is not None and feedback.isCanceled():
        return None
    return windowed_read(cache_path, out_path, geo_window, cache_dir, cache_key, feedback=feedback,
                          is_local=True)


def reproject_bbox_osr(bbox, src_authid, dst_authid):
    src_srs = osr.SpatialReference()
    src_srs.SetFromUserInput(src_authid)
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst_srs = osr.SpatialReference()
    dst_srs.SetFromUserInput(dst_authid)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src_srs, dst_srs)
    corners = [(bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3])]
    xs, ys = [], []
    for x, y in corners:
        tx, ty, _ = tr.TransformPoint(x, y)
        xs.append(tx)
        ys.append(ty)
    return (min(xs), min(ys), max(xs), max(ys))


def detect_crs(path, feedback=None, label=""):
    """Dreistufige CRS-Erkennung aus einer echten Datei - AutoIdentifyEPSG,
    dann Namensabgleich, dann feste Annahme (BEV_CRS). JEDE Ebene einzeln
    per try/except abgesichert, damit eine Exception in Ebene 1 nicht
    automatisch auch Ebene 2/3 verhindert."""
    try:
        ds = gdal.Open(path)
        wkt = ds.GetProjection() if ds is not None else ""
        ds = None
    except Exception:
        wkt = ""

    if not wkt:
        if feedback is not None:
            feedback.pushWarning(f"{label}: keine Projektion in der Datei gefunden - verwende ersatzweise {BEV_CRS}.")
        return BEV_CRS

    epsg_code = None
    srs = None
    try:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        if srs.AutoIdentifyEPSG() == 0:
            epsg_code = srs.GetAuthorityCode(None)
    except Exception:
        srs = None

    if not epsg_code and srs is not None:
        try:
            crs_name = srs.GetName()
            epsg_code = KNOWN_BEV_CRS_NAMES.get(crs_name)
            if epsg_code and feedback is not None:
                feedback.pushInfo(f"{label}: CRS anhand des Namens '{crs_name}' als EPSG:{epsg_code} erkannt.")
        except Exception:
            pass

    if not epsg_code:
        epsg_code = BEV_CRS.split(":")[1]
        if feedback is not None:
            feedback.pushInfo(f"{label}: CRS konnte nicht eindeutig bestimmt werden - verwende {BEV_CRS} als Annahme.")
    elif feedback is not None:
        feedback.pushInfo(f"{label}: Original-Datei-CRS als EPSG:{epsg_code} identifiziert.")

    return f"EPSG:{epsg_code}"


def run_parallel(work_items, worker_fn, feedback, max_workers=3, progress_base=0, progress_span=100):
    """Verarbeitet eine Liste von Arbeitseinheiten parallel, abbrechbar
    innerhalb ca. 1 Sekunde (kein 'with'-Statement fuer den Executor, damit
    ein Abbruch nicht durch automatisches wait=True beim Verlassen des
    Blocks wieder blockiert). worker_fn erhaelt ein Element aus work_items
    und liefert entweder ein Ergebnis oder None."""
    results = []
    canceled = False
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = {ex.submit(worker_fn, item): item for item in work_items}
        pending = set(futs.keys())
        total = len(pending)
        done_count = 0
        while pending:
            if feedback.isCanceled():
                canceled = True
                feedback.pushInfo(f"Abbruch erkannt - breche {len(pending)} verbleibende Aufgabe(n) ab ...")
                feedback.pushInfo(
                    "Hinweis: bereits laufende Übertragungen bemerken den Abbruch erst beim nächsten "
                    "GDAL-Fortschritts-Check (kann einige Sekunden dauern) - es ist normal, wenn danach "
                    "noch 1-2 'User terminated'-Meldungen nachkommen, auch nachdem dieser Lauf schon als "
                    "beendet angezeigt wird. Das bedeutet nicht, dass etwas hängt.")
                for f in pending:
                    f.cancel()
                break
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    r = f.result()
                    if r:
                        results.append(r)
                except Exception:
                    pass
                done_count += 1
            if total:
                feedback.setProgress(int(progress_base + (done_count / total) * progress_span))
    finally:
        ex.shutdown(wait=not canceled, cancel_futures=canceled)
    return results, canceled


class BevOrthofotoBulkDownload(QgsProcessingAlgorithm):
    EXTENT = "EXTENT"
    INCLUDE_INFRARED = "INCLUDE_INFRARED"
    RGBI_STACK = "RGBI_STACK"
    BUILD_OVERVIEWS = "BUILD_OVERVIEWS"
    TARGET_CRS = "TARGET_CRS"
    OUTPUT_FOLDER = "OUTPUT_FOLDER"
    USE_GRID_RGB = "USE_GRID_RGB"

    def initAlgorithm(self, config: Optional[dict[str, Any]] = None):
        """
        Hier definieren wir die Eingabeparameter und Einstellungen des
        Werkzeugs.
        """
        self.addParameter(QgsProcessingParameterExtent(self.EXTENT, "Gebiet (AOI)"))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INCLUDE_INFRARED, "Nahinfrarot-Kanal zusätzlich laden (RGBI)",
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.RGBI_STACK, "RGBI als 4-Kanal-Stack kombinieren",
            defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.BUILD_OVERVIEWS, "Pyramiden (Übersichtsebenen) für schnelleres Anzeigen erstellen",
            defaultValue=True))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT_FOLDER, "Zielordner"))

        # WICHTIG: Zusaetzlicher, additiver Parameter unter "Erweiterte
        # Einstellungen" - laedt bei Aktivierung ZUSAETZLICH zum
        # (immer laufenden) RGBI-Operat-Produkt auch noch das feste
        # 50x50-km-Gitterprodukt (reines RGB, kein Infrarot). Ersetzt NICHT
        # die RGBI-Suche, sondern ergaenzt sie.
        grid_rgb_param = QgsProcessingParameterBoolean(
            self.USE_GRID_RGB,
            "Zusätzlich das feste RGB-Gitterprodukt laden (ohne Infrarot, eigener Layer)",
            defaultValue=False)
        grid_rgb_param.setFlags(grid_rgb_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(grid_rgb_param)

        target_crs_param = QgsProcessingParameterCrs(
            self.TARGET_CRS,
            "Ziel-CRS (leer lassen = jeweilige Original-CRS der Kacheln/Operate)",
            optional=True)
        target_crs_param.setFlags(target_crs_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(target_crs_param)

    def processAlgorithm(
        self,
        parameters: dict[str, Any],
        context: QgsProcessingContext,
        feedback: QgsProcessingFeedback,
    ) -> dict[str, Any]:
        """
        Hier findet die eigentliche Verarbeitung statt.
        """
        # WICHTIG: parameterAsExtent()'s eingebaute Umrechnung auf ein
        # Ziel-CRS hat sich in Tests als UNZUVERLAESSIG herausgestellt - eine
        # reale Testeingabe (EPSG:31255 -> EPSG:3035) wurde dabei um bis zu
        # 156km falsch berechnet, obwohl eine unabhaengige Nachrechnung per
        # pyproj die korrekten Werte lieferte. Deshalb wird hier NIE
        # parameterAsExtent() fuer eine Umrechnung verwendet - nur noch, um
        # die AOI in ihrer EIGENEN Original-CRS zu bekommen (kein Ziel-CRS =
        # keine Umrechnung noetig), und jede tatsaechliche Umrechnung
        # passiert ausschliesslich ueber unser eigenes, mehrfach validiertes
        # reproject_bbox_osr().
        raw_value = parameters.get(self.EXTENT)
        match = re.match(
            r"\s*([\-0-9.eE]+)\s*,\s*([\-0-9.eE]+)\s*,\s*([\-0-9.eE]+)\s*,\s*([\-0-9.eE]+)"
            r"\s*(?:\[\s*([^\]]+?)\s*\])?\s*$",
            raw_value) if isinstance(raw_value, str) else None
        if match:
            orig_bbox = tuple(float(match.group(i)) for i in range(1, 5))
            orig_crs = QgsCoordinateReferenceSystem(match.group(5)) if match.group(5) \
                else self.parameterAsExtentCrs(parameters, self.EXTENT, context)
        else:
            extent_native = self.parameterAsExtent(parameters, self.EXTENT, context)
            orig_bbox = (extent_native.xMinimum(), extent_native.yMinimum(),
                         extent_native.xMaximum(), extent_native.yMaximum())
            orig_crs = self.parameterAsExtentCrs(parameters, self.EXTENT, context)
        feedback.pushInfo(
            f"AOI in Original-CRS ({orig_crs.authid()}): xmin={orig_bbox[0]:.4f}, ymin={orig_bbox[1]:.4f}, "
            f"xmax={orig_bbox[2]:.4f}, ymax={orig_bbox[3]:.4f}")

        aoi_bbox = orig_bbox if orig_crs.authid() == BEV_CRS \
            else reproject_bbox_osr(orig_bbox, orig_crs.authid(), BEV_CRS)
        feedback.pushInfo(
            f"AOI in {BEV_CRS}: xmin={aoi_bbox[0]:.1f}, ymin={aoi_bbox[1]:.1f}, "
            f"xmax={aoi_bbox[2]:.1f}, ymax={aoi_bbox[3]:.1f}")

        if not looks_like_valid_3035_austria(aoi_bbox):
            feedback.reportError(
                f"Die AOI wurde nach {BEV_CRS} umgerechnet, liegt aber weit ausserhalb von "
                "Oesterreich. Das deutet auf eine fehlgeschlagene CRS-Umrechnung hin, oft weil "
                "ein benoetigtes PROJ-Datumsgitter auf diesem System fehlt und die Umrechnung "
                "deshalb unveraendert durchgereicht wurde. Bitte in QGIS unter Einstellungen > "
                "Optionen > CRS-Verwaltung den Netzwerk-Download von PROJ-Gittern aktivieren.")
            return {}

        # Ordnername aus den (ganzzahligen) AOI-Koordinaten - Produkte
        # desselben Laufs landen so gemeinsam in einem AOI-eigenen Ordner,
        # statt sich einen einzigen "DOP_RGB"/"DOP_RGBI"-Ordner ueber
        # verschiedene Laeufe hinweg zu teilen.
        aoi_folder = "_".join(str(int(round(v))) for v in aoi_bbox)

        aoi_bbox_4326 = orig_bbox if orig_crs.authid() == "EPSG:4326" \
            else reproject_bbox_osr(orig_bbox, orig_crs.authid(), "EPSG:4326")

        # WICHTIG: Warnung VOR jedem Download - bei 20cm Aufloesung wächst
        # das Datenvolumen pro Flaeche schnell. Der Nutzer kann an dieser
        # Stelle noch bequem abbrechen, bevor ueberhaupt Bandbreite
        # verbraucht wird.
        width_km = (aoi_bbox[2] - aoi_bbox[0]) / 1000
        height_km = (aoi_bbox[3] - aoi_bbox[1]) / 1000
        area_km2 = width_km * height_km
        feedback.pushInfo(f"AOI-Größe: {width_km:.1f} × {height_km:.1f} km ({area_km2:.0f} km²).")
        if area_km2 > WARN_AREA_KM2:
            feedback.pushWarning(
                f"Achtung: Das gewählte Gebiet ist mit {area_km2:.0f} km² sehr groß. Bei 20cm Auflösung "
                "kann dieser Lauf sehr lange dauern und viel Bandbreite/Speicherplatz benötigen. Falls "
                "das nicht beabsichtigt ist, jetzt über das Abbrechen-Symbol stoppen, bevor der Download "
                "beginnt.")

        # WICHTIG: RGBI (Operat) laeuft immer - kein Auswahl-Menu mehr
        # noetig. Das feste RGB-Gitterprodukt wird nur ZUSAETZLICH geladen,
        # wenn unter "Erweiterte Einstellungen" aktiviert (siehe
        # initAlgorithm) - ersetzt RGBI nicht, ergaenzt es nur.
        do_rgbi = True
        do_rgb = self.parameterAsBoolean(parameters, self.USE_GRID_RGB, context)
        include_infrared = self.parameterAsBoolean(parameters, self.INCLUDE_INFRARED, context)
        rgbi_stack = self.parameterAsBoolean(parameters, self.RGBI_STACK, context)
        build_overviews = self.parameterAsBoolean(parameters, self.BUILD_OVERVIEWS, context)
        target_crs = self.parameterAsCrs(parameters, self.TARGET_CRS, context)
        out_root = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)

        results = {}
        any_canceled = False

        def build_vrt_and_warp(pieces, out_dir, label, extra_sources=None, separate=False):
            """Baut aus einer oder mehreren Kacheln die Ausgabe. Das VRT ist
            bewusst das tatsaechliche Endergebnis - keine materialisierte
            Kopie der Pixel, auch beim Baender-Stapeln (RGB+NIR) nicht mehr
            (fruehere Version uebersetzte das Stack-VRT sofort in eine echte
            Datei; das blockierte bei grossen Stacks lange ohne Fortschritt
            und ohne Abbruch-Moeglichkeit, da gdal.Translate dort ohne
            Callback lief). Frueher fuehrte ein geoeffnetes VRT wiederholt zu
            'Cannot open GDAL dataset'; die eigentliche Ursache (manche
            Quellen sind "Sueden-oben" ausgerichtet) wird inzwischen schon
            VOR diesem Punkt behoben (siehe compute_src_window/
            _attempt_windowed_translate) - build_verified_vrt() bleibt
            trotzdem als generelle Absicherung bestehen."""
            try:
                sources = extra_sources if extra_sources is not None else pieces
                tile_crs = detect_crs(pieces[0], feedback=feedback, label=label)

                if len(sources) == 1 and not separate:
                    labeled_path = unique_path(os.path.join(out_dir, f"{label}.tif"))
                    if robust_replace(sources[0], labeled_path, feedback=feedback, cache_key=label):
                        feedback.pushInfo(f"{label}: nur eine Datei - umbenannt zu {labeled_path}.")
                        merged_path = labeled_path
                    else:
                        feedback.pushWarning(f"{label}: Umbenennen fehlgeschlagen - verwende Originalnamen.")
                        merged_path = sources[0]
                else:
                    if not separate:
                        feedback.pushInfo(f"{label}: führe {len(sources)} Kacheln zu einem VRT zusammen ...")
                        # Vorsichtsmassnahme: jede Quelldatei einzeln auf
                        # Lesbarkeit pruefen, BEVOR versucht wird, sie
                        # zusammenzufuehren - die Quellen sind ggf. gerade
                        # erst frisch geschrieben worden.
                        for src in sources:
                            for attempt in range(5):
                                try:
                                    check_ds = gdal.Open(src)
                                    if check_ds is not None:
                                        check_ds = None
                                        break
                                except Exception:
                                    pass
                                feedback.pushInfo(
                                    f"{label}: Quelldatei {os.path.basename(src)} noch nicht lesbar, "
                                    "warte kurz ...")
                                time.sleep(2)
                    merged_path = build_verified_vrt(sources, out_dir, label, tile_crs,
                                                      feedback=feedback, separate=separate)

                final_path = merged_path
                if target_crs.isValid():
                    safe_authid = target_crs.authid().replace(":", "_") or "custom_crs"
                    # WICHTIG: Den einfachen EPSG-Code uebergeben, nicht die
                    # volle WKT-Beschreibung (target_crs.toWkt()) - moderne
                    # PROJ-Versionen erzeugen dabei sehr lange, komplexe
                    # WKT2-Strings, die GDALs oder QGIS' eigene CRS-Erkennung
                    # beim spaeteren Oeffnen der Datei ausbremsen koennten.
                    # Nur bei einer CRS ohne EPSG-Code (selten) auf die volle
                    # WKT zurueckfallen.
                    dst_crs_str = target_crs.authid() or target_crs.toWkt()
                    final_path = build_verified_vrt(
                        [merged_path], out_dir, f"{label}_{safe_authid}", tile_crs,
                        feedback=feedback, dst_crs=dst_crs_str)
                    feedback.pushInfo(f"{label}: nach {target_crs.authid()} umprojiziert -> {final_path}")

                # WICHTIG: Pyramiden erst hier, EINMAL, auf der tatsaechlichen
                # Enddatei (nach Mosaik UND nach einer eventuellen
                # Umprojektion) - nicht vorher auf Einzelkachelstuecken oder
                # Zwischenstufen, deren Pyramiden sonst nutzlos waeren.
                if build_overviews:
                    add_overviews(final_path, feedback=feedback)
                return final_path
            except Exception as e:
                feedback.pushWarning(f"{label}: Mosaik-Erstellung/Umprojektion fehlgeschlagen ({e}) - übersprungen.")
                return None

        # ------------------------------------------------------------------
        # Produkt 1: Orthofoto RGBI (DOP RGBI, Operat) - laeuft immer,
        # Katalogsuche + lokale Überschneidungsprüfung, RGB+NIR getrennt
        # oder als Stack.
        # ------------------------------------------------------------------
        if do_rgbi and not feedback.isCanceled():
            operate = find_rgbi_operate(aoi_bbox_4326, feedback=feedback)
            if not operate:
                feedback.pushWarning("Orthofoto RGBI: keine Operate für diese AOI gefunden.")
            else:
                out_dir = os.path.join(out_root, aoi_folder, "DOP_RGBI")
                cache_dir = os.path.join(out_root, "_cache", "DOP_RGBI")
                os.makedirs(out_dir, exist_ok=True)
                rgbi_years = year_suffix(extract_year(op.get("temporal")) for op in operate)

                def process_operat(op):
                    # WICHTIG: Grosszuegiges Limit - bei 60 Zeichen wurden
                    # die letzten, unterscheidenden Ziffern der Operat-
                    # Jahreszahl abgeschnitten (z.B. "Operat 2025160" ->
                    # "Operat_20"), wodurch mehrere Operate denselben Namen
                    # bekamen und Log-Zeilen/Diagnose nicht mehr
                    # unterscheidbar waren. 120 Zeichen ist immer noch weit
                    # unter jedem Dateisystem-Limit (255 Zeichen je
                    # Pfadteil), laesst aber genug Spielraum fuer den
                    # tatsaechlichen Titel plus Suffixe wie "_RGB_<Fenster>".
                    safe_title = "".join(c if c.isalnum() else "_" for c in op["title"])[:120]

                    # WICHTIG: RGBI-Operate liegen NICHT zwingend in
                    # EPSG:3035 wie das Gitterprodukt - das war eine falsche
                    # Annahme im ersten Testlauf (fuehrte zu einem leeren,
                    # unsinnig berechneten Pixel-Fenster fuer alle Operate
                    # gleichermassen). Die tatsaechliche CRS wird deshalb aus
                    # der echten Datei gelesen, nicht angenommen.
                    op_crs = detect_crs("/vsicurl/" + op["rgb_url"], feedback=feedback, label=safe_title)
                    aoi_bbox_op_crs = aoi_bbox if op_crs == BEV_CRS else reproject_bbox_osr(aoi_bbox, BEV_CRS, op_crs)
                    op_bbox_native = reproject_bbox_osr(op["bbox"], "EPSG:4326", op_crs)
                    window = (max(aoi_bbox_op_crs[0], op_bbox_native[0]), max(aoi_bbox_op_crs[1], op_bbox_native[1]),
                              min(aoi_bbox_op_crs[2], op_bbox_native[2]), min(aoi_bbox_op_crs[3], op_bbox_native[3]))
                    if window[0] >= window[2] or window[1] >= window[3]:
                        return None
                    window_key = "_".join(str(int(round(v))) for v in window)

                    rgb_path = os.path.join(out_dir, f"{safe_title}_RGB_{window_key}.tif")
                    if not os.path.exists(rgb_path):
                        rgb_path = windowed_read(op["rgb_url"], rgb_path, window, cache_dir,
                                                  f"{safe_title}_RGB", feedback=feedback)
                    if not rgb_path:
                        return None

                    nir_path = None
                    if include_infrared and op["nir_url"] and not feedback.isCanceled():
                        nir_path = os.path.join(out_dir, f"{safe_title}_NIR_{window_key}.tif")
                        if not os.path.exists(nir_path):
                            nir_path = windowed_read(op["nir_url"], nir_path, window, cache_dir,
                                                      f"{safe_title}_NIR", feedback=feedback)
                    return (rgb_path, nir_path)

                op_results, canceled = run_parallel(
                    operate, process_operat, feedback, max_workers=3,
                    progress_base=0, progress_span=50 if do_rgb else 100)
                any_canceled = any_canceled or canceled
                rgb_pieces = [r[0] for r in op_results if r]
                nir_pieces = [r[1] for r in op_results if r and r[1]]

                if canceled:
                    feedback.pushWarning("Orthofoto RGBI: abgebrochen.")
                elif rgb_pieces:
                    feedback.pushInfo(
                        f"Orthofoto RGBI: {len(rgb_pieces)} von {len(operate)} Operat(en) erfolgreich verarbeitet "
                        f"({len(nir_pieces)} davon mit Infrarot-Kanal).")

                    if rgbi_stack and len(nir_pieces) == len(rgb_pieces):
                        stack_label = "DOP_RGBI_Stack" + rgbi_years
                        try:
                            tile_crs = detect_crs(rgb_pieces[0], feedback=feedback, label="DOP_RGBI")
                            rgb_vrt_path = build_verified_vrt(
                                rgb_pieces, out_dir, "DOP_RGBI_RGB_intern", tile_crs, feedback=feedback)
                            nir_vrt_path = build_verified_vrt(
                                nir_pieces, out_dir, "DOP_RGBI_NIR_intern", tile_crs, feedback=feedback)
                            final_path = build_vrt_and_warp(
                                [rgb_vrt_path, nir_vrt_path], out_dir, stack_label,
                                extra_sources=[rgb_vrt_path, nir_vrt_path], separate=True)
                            if final_path:
                                results[stack_label] = final_path
                        except Exception as e:
                            feedback.pushWarning(f"Orthofoto RGBI: 4-Kanal-Stack fehlgeschlagen ({e}) - "
                                                  "gebe RGB/NIR stattdessen getrennt aus.")
                            rgbi_stack = False

                    if not rgbi_stack or len(nir_pieces) != len(rgb_pieces):
                        rgb_label = "DOP_RGBI_RGB" + rgbi_years
                        nir_label = "DOP_RGBI_NIR" + rgbi_years
                        final_rgb = build_vrt_and_warp(rgb_pieces, out_dir, rgb_label)
                        if final_rgb:
                            results[rgb_label] = final_rgb
                        if nir_pieces:
                            final_nir = build_vrt_and_warp(nir_pieces, out_dir, nir_label)
                            if final_nir:
                                results[nir_label] = final_nir
                else:
                    feedback.pushWarning("Orthofoto RGBI: keine Operate erfolgreich verarbeitet.")

        # ------------------------------------------------------------------
        # Produkt 2: Orthofoto RGB (DOP, Gitter) - nur wenn unter "Erweiterte
        # Einstellungen" aktiviert, ZUSAETZLICH zu RGBI. Kachel-ID wird
        # direkt aus den AOI-Koordinaten berechnet, Download-URL per
        # Katalogsuche.
        # ------------------------------------------------------------------
        if do_rgb and not feedback.isCanceled():
            tiles = tiles_for_bbox(aoi_bbox)
            feedback.pushInfo(f"Orthofoto RGB: {len(tiles)} Kachel(n) betroffen: " +
                               ", ".join(f"N{n}E{e}" for n, e in tiles))
            out_dir = os.path.join(out_root, aoi_folder, "DOP_RGB")
            cache_dir = os.path.join(out_root, "_cache", "DOP_RGB")
            os.makedirs(out_dir, exist_ok=True)

            # Erst alle benoetigten Kachel-URLs auflösen.
            tile_urls = []
            years_found = []
            for n, e in tiles:
                url, temporal = find_dop_tile_url(n, e, feedback=feedback)
                if url:
                    tile_urls.append((n, e, url))
                    years_found.append(extract_year(temporal))
                else:
                    feedback.pushWarning(f"N{n}E{e}: keine DOP-Kachel im Katalog gefunden - übersprungen.")
            rgb_label = "DOP_RGB" + year_suffix(years_found)

            # Jede Kachel einzeln herunterladen (Fenster-Lesen, eigene
            # Rueckfallebene je Kachel), dann per VRT zusammenfuehren -
            # dasselbe Vorgehen wie beim RGBI-Produkt, statt eines
            # gesonderten "alles auf einmal"-Wegs nur fuer dieses Produkt.
            def process_rgb_tile(tile_info):
                n, e, url = tile_info
                tile_bounds = (e, n, e + TILE_SIZE, n + TILE_SIZE)
                window = (max(aoi_bbox[0], tile_bounds[0]), max(aoi_bbox[1], tile_bounds[1]),
                          min(aoi_bbox[2], tile_bounds[2]), min(aoi_bbox[3], tile_bounds[3]))
                if window[0] >= window[2] or window[1] >= window[3]:
                    return None
                key = f"DOP_N{n}E{e}"
                window_key = "_".join(str(int(round(v))) for v in window)
                out_path = os.path.join(out_dir, f"{key}_{window_key}.tif")
                if os.path.exists(out_path):
                    return out_path
                result_path = windowed_read(url, out_path, window, cache_dir, key, feedback=feedback)
                if result_path:
                    try:
                        ds = gdal.Open(result_path)
                        band_count = ds.RasterCount if ds is not None else 0
                        ds = None
                        if band_count != 3:
                            feedback.pushWarning(
                                f"{key}: heruntergeladene Datei hat {band_count} Band/Bänder statt der "
                                "erwarteten 3 für ein RGB-Orthofoto - das deutet auf ein falsches "
                                "Produkt hin (z.B. versehentlich eine Höhendaten-Kachel). Bitte Ergebnis "
                                "manuell prüfen, bevor du weiterarbeitest.")
                    except Exception:
                        pass
                return result_path

            final_path = None
            if tile_urls:
                pieces, canceled = run_parallel(
                    tile_urls, process_rgb_tile, feedback,
                    progress_base=50 if do_rgbi else 0, progress_span=50 if do_rgbi else 100)
                any_canceled = any_canceled or canceled
                if canceled:
                    feedback.pushWarning("Orthofoto RGB: abgebrochen.")
                elif pieces:
                    feedback.pushInfo(
                        f"Orthofoto RGB: {len(pieces)} von {len(tile_urls)} Kachel(n) erfolgreich verarbeitet.")
                    final_path = build_vrt_and_warp(pieces, out_dir, rgb_label)

            if final_path:
                results[rgb_label] = final_path
            elif tile_urls and not any_canceled:
                feedback.pushWarning("Orthofoto RGB: keine Kacheln erfolgreich verarbeitet.")

        if any_canceled:
            feedback.pushInfo(f"Abgebrochen - {len(results)} bereits vollständig verarbeitete Layer "
                               "werden trotzdem hinzugefügt.")

        for label, path in results.items():
            context.addLayerToLoadOnCompletion(
                path, QgsProcessingContext.LayerDetails(label, context.project(), label))
            feedback.pushInfo(f"Layer '{label}' zum Laden vorgemerkt -> {path}")

        summary = f"Fertig: {len(results)} Layer hinzugefügt."
        if any_canceled:
            summary += " Lauf wurde vom Nutzer abgebrochen."
        feedback.pushInfo(summary)
        return {}

    def name(self) -> str:
        """
        Gibt den Algorithmus-Namen zurueck, der zur Identifizierung des
        Algorithmus verwendet wird. Dieser String sollte fuer den
        Algorithmus fest sein und darf nicht lokalisiert werden.
        """
        return "bev_orthofoto_bulk_download"

    def displayName(self) -> str:
        """
        Gibt den uebersetzten Algorithmus-Namen zurueck, der fuer jede
        nutzersichtbare Anzeige des Algorithmus-Namens verwendet werden
        sollte.
        """
        return "BEV Orthofoto Bulk-Download"

    def group(self) -> str:
        """
        Gibt den Namen der Gruppe zurueck, zu der dieser Algorithmus
        gehoert.
        """
        return "LiberGIS"

    def groupId(self) -> str:
        """
        Gibt die eindeutige ID der Gruppe zurueck, zu der dieser Algorithmus
        gehoert. Dieser String sollte fuer den Algorithmus fest sein und
        darf nicht lokalisiert werden.
        """
        return "libergis"

    def shortHelpString(self) -> str:
        """
        Gibt einen lokalisierten Kurz-Hilfetext fuer den Algorithmus zurueck.
        """
        return (
            "<p>Lädt Orthofotos (20cm Auflösung) für das gewählte Gebiet aus dem bundesweiten "
            "BEV-Datenkatalog - deckt ganz Österreich ab.</p>"
            "<p><b>Standardeinstellung:</b><br>"
            "<b>Orthofoto RGBI</b> - liegt in unregelmäßigen Befliegungs-Operaten, wird komplett "
            "per Katalog-Volltextsuche gefunden. Deckt laut BEV in Summe ganz Österreich ab, "
            "allerdings mit unterschiedlichen Befliegungsjahren je nach Operat (rollierender "
            "3-Jahres-Zyklus). Der Nahinfrarot-Kanal (separate Datei) lässt sich per Häkchen "
            "abschalten, oder auf Wunsch zu einem 4-Kanal-Stack mit RGB kombinieren.</p>"
            "<p><b>Erweiterte Einstellungen:</b><br>"
            "<b>Zusätzlich Orthofoto RGB</b> – als Alternative Quelle für den Orthofoto-Download. "
            "Liegt auf einem festen 50×50-km-Gitter, 3 Kanäle, flächendeckend mit einem "
            "einheitlichen Stichtag pro Kachel, aber ohne Infrarot. Wird als eigener, "
            "zusätzlicher Layer geladen, nicht anstelle von RGBI.<br>"
            "<b>Ziel-CRS</b> - Es lässt sich auch ein Ziel-CRS für alle Ergebnisse festlegen - da "
            "dafür virtuell umprojiziert wird, kann das die Verarbeitungszeit spürbar erhöhen.</p>"
            "<p>Beide Produkte lesen nur den benötigten Ausschnitt per HTTP-Range direkt aus der "
            "Cloud-optimierten GeoTIFF, ohne die komplette (oft mehrere GB große) Datei "
            "herunterzuladen. Bei großen Gebieten warnt das Tool vor Beginn des Downloads.</p>"
            "<p>Ergebnisdateien werden nach dem Befliegungsjahr benannt (laut Katalog-Metadaten) "
            "und erhalten optional interne Pyramiden für schnelleres Anzeigen in QGIS.</p>"
            "<p>Ergebnisse werden als VRT-Dateien im Projekt geladen - die heruntergeladenen "
            "Kacheln bleiben als TIF-Dateien erhalten.</p>"
            "<p>Quelle: Bundesamt für Eich- und Vermessungswesen (BEV), "
            "https://www.bev.gv.at, CC-BY-4.0</p>"
        )

    def createInstance(self):
        return self.__class__()
