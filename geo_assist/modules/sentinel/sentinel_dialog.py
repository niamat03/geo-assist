"""
Sentinel_dialog.py  v1.0
Plugin QGIS professionnel
"""

import os, json, shutil, time, zipfile, glob
import numpy as np
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QStackedWidget, QWidget, QGroupBox, QPushButton, QLabel,
    QLineEdit, QDoubleSpinBox, QComboBox, QFileDialog,
    QMessageBox, QTableWidget, QTableWidgetItem, QDateEdit,
    QHeaderView, QSizePolicy, QProgressBar, QFrame,
    QScrollArea, QSlider, QAbstractItemView, QSplitter,
    QApplication
)
from qgis.PyQt.QtCore import Qt, QDate, QThread, pyqtSignal, QPoint, QRect
from qgis.PyQt.QtGui import QFont, QColor, QCursor, QBrush

from qgis.core import (
    QgsProject, QgsRasterLayer, QgsVectorLayer,
    QgsMessageLog, Qgis,
    QgsCoordinateReferenceSystem, QgsRectangle,
    QgsGeometry, QgsFeature,
    QgsFields, QgsField, QgsPointXY,
    QgsCoordinateTransform
)
from qgis.PyQt.QtCore import QVariant


# CONSTANTES

DATE_TODAY  = QDate.currentDate()
DATE_S1_MIN = QDate(2014, 4,  1)
DATE_S2_MIN = QDate(2015, 6, 23)

CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu"
    "/auth/realms/CDSE/protocol/openid-connect/token"
)
CDSE_ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
CDSE_DL    = (
    "https://catalogue.dataspace.copernicus.eu"
    "/odata/v1/Products({pid})/$value"
)
NOMINATIM = "https://nominatim.openstreetmap.org/search"


# HELPERS UI
def _sep():
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFrameShadow(QFrame.Sunken)
    return f

def _lbl(text=""):
    l = QLabel(text)
    l.setWordWrap(True)
    return l

def _bold(text):
    l = QLabel(text)
    f = QFont()
    f.setBold(True)
    l.setFont(f)
    return l

def _date_edit(date, min_d):
    de = QDateEdit(date)
    de.setCalendarPopup(True)
    de.setMinimumDate(min_d)
    de.setMaximumDate(DATE_TODAY)
    de.setDisplayFormat("dd/MM/yyyy")
    return de

def _set_status(label, text, level="info"):
    icons = {"info": "[i]", "ok": "[OK]", "error": "[ERR]", "warn": "[!]"}
    label.setText(f"{icons.get(level, '')} {text}")

def _log(msg):
    QgsMessageLog.logMessage(msg, "SentinelVision", Qgis.Info)


# MODULE AOI 


def geocode_place(name, timeout=20):
    params = urlencode({"q": name, "format": "json", "limit": 1})
    req = Request(
        f"{NOMINATIM}?{params}",
        headers={"User-Agent": "SentinelVision-QGIS/10.1"})
    try:
        with urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except URLError as e:
        raise RuntimeError(f"Erreur reseau Nominatim : {e}")
    if not data:
        raise RuntimeError(f"Aucun resultat pour '{name}'.")
    d  = data[0]
    bb = d.get("boundingbox", [])
    if len(bb) == 4:
        la0, la1, lo0, lo1 = (float(v) for v in bb)
    else:
        lat, lon = float(d["lat"]), float(d["lon"])
        la0, la1, lo0, lo1 = lat-.05, lat+.05, lon-.05, lon+.05
    return {
        "bbox":  [lo0, la0, lo1, la1],
        "label": d.get("display_name", name),
        "lat":   float(d["lat"]),
        "lon":   float(d["lon"]),
    }

def bbox_from_manual(lo0, la0, lo1, la1):
    if not (-180 <= lo0 < lo1 <= 180):
        raise ValueError(f"Longitudes invalides : {lo0} / {lo1}")
    if not (-90 <= la0 < la1 <= 90):
        raise ValueError(f"Latitudes invalides : {la0} / {la1}")
    return {
        "bbox":  [lo0, la0, lo1, la1],
        "label": f"[{lo0:.4f},{la0:.4f},{lo1:.4f},{la1:.4f}]",
        "lat":   (la0 + la1) / 2,
        "lon":   (lo0 + lo1) / 2,
    }

# MODULE CDSE — authentification + catalogue

def cdse_get_token(email, password, timeout=30):
    body = urlencode({
        "client_id":  "cdse-public",
        "grant_type": "password",
        "username":   email.strip(),
        "password":   password.strip(),
    }).encode()
    req = Request(
        CDSE_TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    try:
        with urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except HTTPError as e:
        raise RuntimeError(
            f"Authentification echouee (HTTP {e.code}) : "
            f"{e.read().decode(errors='replace')[:300]}")
    except URLError as e:
        raise RuntimeError(f"Erreur reseau : {e}")
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Token absent dans la reponse CDSE.")
    return token


def cdse_search(token, bbox, d_start, d_end,
                satellite="S2", max_cloud=20, max_results=25):
    lo0, la0, lo1, la1 = bbox
    poly = (f"POLYGON(({lo0} {la0},{lo1} {la0},"
            f"{lo1} {la1},{lo0} {la1},{lo0} {la0}))")

    if satellite == "S2":
        fp = [
            "Collection/Name eq 'SENTINEL-2'",
            ("Attributes/OData.CSC.StringAttribute/any("
             "att:att/Name eq 'productType' and "
             "att/OData.CSC.StringAttribute/Value eq 'S2MSI2A')"),
            f"ContentDate/Start gt {d_start}T00:00:00.000Z",
            f"ContentDate/Start lt {d_end}T23:59:59.000Z",
            f"OData.CSC.Intersects(area=geography'SRID=4326;{poly}')",
        ]
        if max_cloud < 100:
            fp.append(
                "Attributes/OData.CSC.DoubleAttribute/any("
                "att:att/Name eq 'cloudCover' and "
                f"att/OData.CSC.DoubleAttribute/Value le {max_cloud:.1f})"
            )
    else:
        fp = [
            "Collection/Name eq 'SENTINEL-1'",
            ("Attributes/OData.CSC.StringAttribute/any("
             "att:att/Name eq 'productType' and "
             "att/OData.CSC.StringAttribute/Value eq 'GRD')"),
            f"ContentDate/Start gt {d_start}T00:00:00.000Z",
            f"ContentDate/Start lt {d_end}T23:59:59.000Z",
            f"OData.CSC.Intersects(area=geography'SRID=4326;{poly}')",
        ]

    params = urlencode({
        "$filter":  " and ".join(fp),
        "$orderby": "ContentDate/Start desc",
        "$top":     max_results,
        "$expand":  "Attributes",
    })
    req = Request(f"{CDSE_ODATA}?{params}",
                  headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
    except HTTPError as e:
        raise RuntimeError(
            f"Catalogue CDSE (HTTP {e.code}) : {e.read().decode()[:300]}")
    except URLError as e:
        raise RuntimeError(f"Erreur reseau catalogue : {e}")

    results = []
    for item in data.get("value", []):
        attrs  = {a["Name"]: a.get("Value")
                  for a in item.get("Attributes", [])}
        cloud  = attrs.get("cloudCover") or attrs.get("cloud_cover")
        start  = item.get("ContentDate", {}).get("Start", "")[:10]
        size_b = item.get("ContentLength") or 0
        geo    = item.get("GeoFootprint") or item.get("Footprint")

        cloud_val = None
        if cloud is not None:
            try:
                cloud_val = round(float(cloud), 1)
            except (TypeError, ValueError):
                cloud_val = None

        results.append({
            "date":        start,
            "product_id":  item.get("Id", "?"),
            "title":       item.get("Name", "?"),
            "cloud_cover": cloud_val,
            "size_mb":     round(size_b / 1_048_576, 1),
            "online":      item.get("Online", True),
            "footprint":   geo,
        })
    return results


# ANALYSE COUVERTURE AOI
def analyse_aoi_coverage(aoi_bbox, results):
    lo0, la0, lo1, la1 = aoi_bbox
    aoi_geom = QgsGeometry.fromRect(QgsRectangle(lo0, la0, lo1, la1))
    aoi_area = aoi_geom.area()
    import math
    lat_c = (la0 + la1) / 2
    km_per_deg_lon = 111.0 * math.cos(math.radians(lat_c))
    aoi_km2 = (lo1-lo0) * km_per_deg_lon * (la1-la0) * 111.0

    if aoi_area <= 0:
        return {"needed_ids": [], "coverage_pct": 0.0, "n_needed": 0,
                "covered_km2": 0.0, "aoi_km2": aoi_km2}

    fp_list = []
    for r in results:
        geo  = r.get("footprint")
        qgeo = _geo_to_qgs(geo)
        if qgeo and not qgeo.isEmpty():
            fp_list.append((r["product_id"], qgeo))

    if not fp_list:
        return {"needed_ids": [], "coverage_pct": 0.0, "n_needed": 0,
                "covered_km2": 0.0, "aoi_km2": aoi_km2}

    remaining  = QgsGeometry(aoi_geom)
    needed_ids = []

    while not remaining.isEmpty() and remaining.area() > 1e-10:
        best_id = None; best_geom = None; best_area = 0.0
        for pid, fgeom in fp_list:
            if pid in needed_ids:
                continue
            inter = remaining.intersection(fgeom)
            if inter is None or inter.isEmpty():
                continue
            a = inter.area()
            if a > best_area:
                best_area = a; best_id = pid; best_geom = fgeom
        if best_id is None:
            break
        needed_ids.append(best_id)
        remaining = remaining.difference(best_geom)
        if remaining is None or remaining.isEmpty():
            break

    remaining_area = remaining.area() if (remaining and not remaining.isEmpty()) else 0
    covered_area   = aoi_area - remaining_area
    coverage_pct   = min(100.0, round(covered_area / aoi_area * 100, 1))
    covered_km2    = round(covered_area / aoi_area * aoi_km2, 1)

    return {
        "needed_ids":   needed_ids,
        "coverage_pct": coverage_pct,
        "n_needed":     len(needed_ids),
        "covered_km2":  covered_km2,
        "aoi_km2":      round(aoi_km2, 1),
    }


def _geo_to_qgs(geo):
    if not geo or not isinstance(geo, dict):
        return None
    try:
        gtype = geo.get("type", "")
        if gtype == "Polygon":
            coords = geo["coordinates"][0]
            pts    = [QgsPointXY(c[0], c[1]) for c in coords]
            return QgsGeometry.fromPolygonXY([pts])
        elif gtype == "MultiPolygon":
            polys = []
            for ring_list in geo["coordinates"]:
                polys.append([QgsPointXY(c[0], c[1]) for c in ring_list[0]])
            return QgsGeometry.fromMultiPolygonXY([polys])
    except Exception:
        pass
    return None

# MODULE telechargement 

def _is_valid_zip(path):
    """Verifie les magic bytes PK d'un fichier ZIP."""
    try:
        with open(path, "rb") as f:
            header = f.read(4)
            return header == b"PK\x03\x04"
    except Exception:
        return False


def _check_zip_integrity(path):
    """
    Verification complete d'un ZIP :
    - Existence du fichier
    - Taille minimale
    - Magic bytes PK
    - Test interne zipfile (testzip)
    Retourne (True, "") si OK, (False, message_erreur) sinon.
    """
    if not os.path.isfile(path):
        return False, f"Fichier introuvable : {path}"

    size = os.path.getsize(path)
    if size < 100_000:
        return False, (
            f"Fichier trop petit ({size} octets).\n"
            f"Le telechargement est probablement incomplet.\n"
            f"Supprimez ce fichier et retelechargez.")

    if not _is_valid_zip(path):
        try:
            with open(path, "rb") as f:
                sample = f.read(200).decode(errors="replace")
        except Exception:
            sample = ""
        return False, (
            f"Ce fichier n'est pas un ZIP valide (magic bytes PK absents).\n"
            f"Debut du fichier : {sample[:150]}\n"
            f"Cause probable : session expiree ou erreur serveur CDSE.\n"
            f"Reconnectez-vous et retelechargez.")

    try:
        with zipfile.ZipFile(path, "r") as z:
            bad = z.testzip()
            if bad:
                return False, (
                    f"Archive ZIP corrompue.\n"
                    f"Fichier invalide detecte : {bad}\n"
                    f"Retelechargez le produit.")
            names = z.namelist()
            if not names:
                return False, "Archive ZIP vide (aucun fichier)."
            _log(f"ZIP valide : {len(names)} fichiers, {size/1_048_576:.1f} MB")
    except zipfile.BadZipFile as e:
        return False, f"Archive ZIP corrompue : {e}"
    except Exception as e:
        return False, f"Erreur lecture ZIP : {e}"

    return True, ""


def download_product(token, product_id, product_title,
                     output_dir, progress_cb=None,
                     cache_dir=None, cancel_flag=None):
    """
    Telechargement du produit Sentinel et sauvegarde au format ZIP.
    Aucune extraction automatique n'est realisee.
    """
    if cache_dir is None:
        cache_dir = os.path.join(output_dir, ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    safe_name   = "".join(c if c.isalnum() or c in "-_." else "_"
                          for c in product_title)
    filename    = safe_name if safe_name.lower().endswith(".zip") \
                  else f"{safe_name}.zip"
    cache_path  = os.path.join(cache_dir, filename)
    output_path = os.path.join(output_dir, filename)

    # Verifier le cache
    if os.path.isfile(cache_path):
        ok, err = _check_zip_integrity(cache_path)
        if ok:
            _log(f"Cache hit valide : {cache_path}")
            shutil.copy2(cache_path, output_path)
            return output_path
        else:
            _log(f"Cache invalide, suppression : {err}")
            try:
                os.remove(cache_path)
            except Exception:
                pass

    url = CDSE_DL.format(pid=product_id)
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    t0  = time.time()
    _log(f"Telechargement demarre : {filename}")

    try:
        with urlopen(req, timeout=600) as resp:
            ct    = resp.headers.get("Content-Type", "")
            total = int(resp.headers.get("Content-Length", 0))
            if "text/html" in ct or "application/json" in ct:
                body = resp.read(2000).decode(errors="replace")
                raise RuntimeError(
                    f"Reponse invalide du serveur (Content-Type={ct}).\n"
                    f"Debut : {body[:400]}\n"
                    f"Reconnectez-vous.")
            done = 0
            with open(cache_path, "wb") as f:
                while True:
                    if cancel_flag and cancel_flag[0]:
                        _log("Annule par l'utilisateur.")
                        raise RuntimeError("__CANCELLED__")
                    chunk = resp.read(131072)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb and total:
                        progress_cb(done, total, time.time() - t0)
    except HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Erreur download CDSE (HTTP {e.code}) :\n{body}")
    except URLError as e:
        raise RuntimeError(f"Erreur reseau download : {e}")

    if not os.path.isfile(cache_path):
        raise RuntimeError("Fichier non cree sur le disque.")

    # Verification complete du ZIP telecharge
    ok, err = _check_zip_integrity(cache_path)
    if not ok:
        try:
            os.remove(cache_path)
        except Exception:
            pass
        raise RuntimeError(f"Fichier telecharge invalide :\n{err}")

    _log("ZIP telecharge et valide. Copie vers dossier de sortie.")
    shutil.copy2(cache_path, output_path)
    return output_path



# MODULE EXTRACTION 

def _s1_warp_band_to_epsg4326(gdal, src_path, dst_path, progress_cb=None):
    """
    Reprojecte un fichier .tiff Sentinel-1 GRD brut (avec GCPs) vers
    un GeoTIFF affine standard en EPSG:4326.

    Les fichiers measurement/*.tiff d'un produit S1 GRD contiennent des
    GCPs (Ground Control Points) au lieu d'une geotransform affine classique.
    gdal.Translate ou BuildVRT appliques directement sur ces fichiers
    preservent les GCPs et produisent un raster que QGIS ne peut pas
    superposer correctement sur OSM.

    gdal.Warp resout ce probleme en :
      1. Lisant les GCPs et en calculant la transformation georeferentielle
      2. Regrillant les donnees dans une projection cartographique standard
      3. Produisant un GeoTIFF avec geotransform affine + CRS EPSG:4326

    Parametres
    ----------
    gdal     : module osgeo.gdal (deja importe par l'appelant)
    src_path : str  chemin du .tiff brut S1
    dst_path : str  chemin de sortie pour le GeoTIFF reprojete
    progress_cb : callable(str) ou None

    Retourne dst_path si succes, leve RuntimeError sinon.
    """
    if progress_cb:
        progress_cb(f"Warp -> EPSG:4326 : {os.path.basename(src_path)}")

    _log(f"[S1 Warp] {os.path.basename(src_path)} -> {os.path.basename(dst_path)}")

    # Verifier que la source est ouvrable
    src_ds = gdal.Open(src_path, gdal.GA_ReadOnly)
    if src_ds is None:
        raise RuntimeError(
            f"Impossible d'ouvrir le fichier source :\n{src_path}\n"
            f"Verifiez que le dossier est correctement decompresse.")

    # Diagnostiquer le type de georeferencement present dans le fichier source
    gt = src_ds.GetGeoTransform()
    gcps = src_ds.GetGCPs()
    has_valid_gt = (gt is not None and gt != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
    has_gcps     = (gcps is not None and len(gcps) > 0)

    _log(f"[S1 Warp] Source : {src_ds.RasterXSize}x{src_ds.RasterYSize} px  "
         f"GCPs={len(gcps) if gcps else 0}  valid_gt={has_valid_gt}")

    src_ds = None  # Fermer avant Warp

    if not has_valid_gt and not has_gcps:
        raise RuntimeError(
            f"Le fichier source ne contient ni geotransform valide ni GCPs :\n"
            f"{src_path}\n"
            f"Le produit est peut-etre incomplet ou corrompu.")

    # Options Warp :
    #   dstSRS       : projection cible EPSG:4326 (WGS84 geographique)
    #   resampleAlg  : bilinear = bon compromis qualite/vitesse pour SAR
    #   targetAlignedPixels : aligne les pixels sur la grille de coordonnees
    #   creationOptions : compression LZW + tuiles pour performances QGIS
    #   multithread  : True pour acceler le calcul
    warp_opts = gdal.WarpOptions(
        dstSRS               = "EPSG:4326",
        resampleAlg          = gdal.GRA_Bilinear,
        creationOptions      = ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"],
        multithread          = True,
        warpMemoryLimit      = 512,       # MB alloues au Warp
        errorThreshold       = 0.125,     # pixels (precision acceptable)
        format               = "GTiff",
    )

    result_ds = gdal.Warp(dst_path, src_path, options=warp_opts)

    if result_ds is None:
        # Nettoyer le fichier partiel si present
        if os.path.isfile(dst_path):
            try:
                os.remove(dst_path)
            except Exception:
                pass
        raise RuntimeError(
            f"gdal.Warp a echoue pour :\n{src_path}\n"
            f"Verifiez l'integrite du produit decompresse.")

    # Verifier que le fichier de sortie a bien une geotransform affine
    out_gt = result_ds.GetGeoTransform()
    out_crs = result_ds.GetProjection()
    result_ds = None  # Fermer le dataset

    if not out_gt or out_gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
        raise RuntimeError(
            f"Warp produit mais la geotransform de sortie est invalide.\n"
            f"Fichier : {dst_path}")

    _log(f"[S1 Warp] OK  gt={out_gt[:4]}  crs={out_crs[:40] if out_crs else 'NONE'}")

    if not os.path.isfile(dst_path):
        raise RuntimeError(f"Fichier Warp introuvable : {dst_path}")

    return dst_path


def extract_geotiff_from_safe(safe_or_product_dir, output_dir,
                               satellite="S2", progress_cb=None):
    """
    Cree des GeoTIFF multibandes a partir d'un dossier produit Sentinel
    deja decompresse (.SAFE pour S2, dossier S1 pour Sentinel-1).

    Parametres
    ----------
    safe_or_product_dir : str
        - Sentinel-2 : chemin vers le dossier .SAFE
          Exemple : D:/data/S2A_MSIL2A_20260520T110619.SAFE
        - Sentinel-1 : chemin vers le dossier du produit GRD
          Exemple : D:/data/S1C_IW_GRDH_1SDV_20260503T182513
    output_dir : str
        Dossier de sortie pour les GeoTIFF generes.
    satellite : str
        "S2" pour Sentinel-2, "S1" pour Sentinel-1.
    progress_cb : callable ou None
        Callback(msg: str) pour le suivi de progression.

    Retourne un dict avec les chemins des GeoTIFF crees.
    """
    try:
        from osgeo import gdal
        gdal.UseExceptions()
    except ImportError:
        raise RuntimeError(
            "GDAL n'est pas disponible dans cet environnement QGIS.\n"
            "Verifiez l'installation de QGIS.")

    result = {
        "tiff_10m":    None,
        "tiff_20m":    None,
        "tiff_full":   None,
        "tiff_s1":     None,
        "bands_found": [],
        "product_dir": safe_or_product_dir,
    }

    if not os.path.isdir(safe_or_product_dir):
        raise RuntimeError(
            f"Dossier introuvable :\n{safe_or_product_dir}\n\n"
            f"Verifiez que vous avez bien selectionne le dossier "
            f"du produit decompresse, et non un fichier ZIP.")

    # Verification droits d'ecriture
    os.makedirs(output_dir, exist_ok=True)
    test_file = os.path.join(output_dir, "_sv_write_test_")
    try:
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
    except OSError as e:
        raise RuntimeError(
            f"Impossible d'ecrire dans le dossier de sortie :\n{output_dir}\n"
            f"Erreur : {e}\nVerifiez les permissions.")

    base = os.path.splitext(os.path.basename(safe_or_product_dir))[0]


    # SENTINEL-2
    if satellite == "S2":
        if progress_cb:
            progress_cb("Analyse de la structure SAFE Sentinel-2...")

        granule_dir = os.path.join(safe_or_product_dir, "GRANULE")
        search_dirs_10m = []
        search_dirs_20m = []
        search_dirs_all = []

        if os.path.isdir(granule_dir):
            search_dirs_10m = glob.glob(
                os.path.join(granule_dir, "*", "IMG_DATA", "R10m"))
            search_dirs_20m = glob.glob(
                os.path.join(granule_dir, "*", "IMG_DATA", "R20m"))
            search_dirs_all = glob.glob(
                os.path.join(granule_dir, "*", "IMG_DATA"))

        # Fallback si pas de structure R10m / R20m
        if not search_dirs_10m and not search_dirs_20m:
            search_dirs_all = search_dirs_all or [safe_or_product_dir]

        bands_10m = {}
        bands_20m = {}
        bands_all = {}

        def _scan_jp2(dirs, band_dict, res_tag=None):
            for img_dir in dirs:
                if not os.path.isdir(img_dir):
                    continue
                jp2_files = glob.glob(os.path.join(img_dir, "*.jp2"))
                jp2_files += glob.glob(os.path.join(img_dir, "**", "*.jp2"),
                                       recursive=True)
                for jp2 in jp2_files:
                    bn = os.path.basename(jp2).upper()
                    for band in ["B02", "B03", "B04", "B08",
                                 "B8A", "B11", "B12"]:
                        if (f"_{band}_" in bn or
                            f"_{band}." in bn or
                            bn.endswith(f"_{band}.JP2")):
                            if band not in band_dict:
                                band_dict[band] = jp2

        _scan_jp2(search_dirs_10m, bands_10m)
        _scan_jp2(search_dirs_20m, bands_20m)
        _scan_jp2(search_dirs_all, bands_all)

        # Fusionner : bands_all contient toutes les bandes trouvees
        for d in (bands_10m, bands_20m):
            for k, v in d.items():
                if k not in bands_all:
                    bands_all[k] = v

        result["bands_found"] = list(bands_all.keys())
        _log(f"[EXTRACT S2] Bandes trouvees : {result['bands_found']}")

        if not bands_all:
            raise RuntimeError(
                "Aucune bande JP2 trouvee dans le dossier SAFE.\n\n"
                "Verifiez que vous avez bien selectionne le dossier "
                ".SAFE et que l'archive a ete correctement decompressee.")

        if progress_cb:
            progress_cb(f"Bandes detectees : {', '.join(result['bands_found'])}")

        def _stack_s2(band_dict, order, suffix):
            paths = [band_dict[b] for b in order if b in band_dict]
            if not paths:
                return None
            out = os.path.join(output_dir, f"{base}_{suffix}.tif")
            if os.path.isfile(out):
                _log(f"[EXTRACT S2] GeoTIFF existe : {out}")
                return out
            if progress_cb:
                progress_cb(
                    f"Creation GeoTIFF {suffix} ({len(paths)} bandes)...")
            _log(f"[EXTRACT S2] {suffix} : {len(paths)} bandes -> {out}")
            try:
                vrt = gdal.BuildVRT(
                    "", paths,
                    options=gdal.BuildVRTOptions(separate=True))
                if vrt is None:
                    raise RuntimeError("gdal.BuildVRT a retourne None.")
                gdal.Translate(
                    out, vrt,
                    options=gdal.TranslateOptions(
                        format="GTiff",
                        creationOptions=[
                            "COMPRESS=LZW",
                            "TILED=YES",
                            "BIGTIFF=IF_SAFER"
                        ]))
                vrt = None
            except Exception as e:
                _log(f"[EXTRACT S2] Erreur {suffix} : {e}")
                return None
            if os.path.isfile(out):
                sz = os.path.getsize(out) / 1_048_576
                _log(f"[EXTRACT S2] GeoTIFF cree : {out} ({sz:.1f} MB)")
                return out
            return None

        if bands_10m:
            result["tiff_10m"] = _stack_s2(
                bands_10m, ["B02", "B03", "B04", "B08"], "10m_BGRN")
        if bands_20m:
            result["tiff_20m"] = _stack_s2(
                bands_20m,
                ["B03", "B04", "B08", "B8A", "B11", "B12"],
                "20m_multi")
        if bands_all and not result["tiff_10m"] and not result["tiff_20m"]:
            result["tiff_full"] = _stack_s2(
                bands_all,
                ["B02", "B03", "B04", "B08", "B8A", "B11", "B12"],
                "full_multi")

        if progress_cb:
            progress_cb("GeoTIFF Sentinel-2 crees avec succes.")

    # SENTINEL-1 
   
    else:
        if progress_cb:
            progress_cb("Analyse du dossier produit Sentinel-1...")

        # ---- 1. Localiser les bandes brutes VV et VH ----
        # Les fichiers .tiff GRD se trouvent dans measurement/
        # Nommage ESA : s1x-<mode>-grd-<pol>-<date>-<orbit>-<n>.tiff
        # On cherche aussi a la racine en fallback.
        search_patterns = [
            os.path.join(safe_or_product_dir, "measurement", "*.tiff"),
            os.path.join(safe_or_product_dir, "measurement", "*.tif"),
            os.path.join(safe_or_product_dir, "*.tiff"),
            os.path.join(safe_or_product_dir, "*.tif"),
        ]
        tiff_files = []
        for pat in search_patterns:
            tiff_files.extend(glob.glob(pat))
        # Dedupliquer en preservant l'ordre
        seen = set()
        tiff_files_unique = []
        for t in tiff_files:
            nt = os.path.normpath(t)
            if nt not in seen:
                seen.add(nt)
                tiff_files_unique.append(t)
        tiff_files = tiff_files_unique

        vv_path = None
        vh_path = None
        for t in tiff_files:
            bn = os.path.basename(t).lower()
            # Patterns de nommage S1 (ESA) : -vv- ou _vv_ ou commence par vv
            if "-vv-" in bn or "_vv_" in bn or bn.startswith("vv"):
                if vv_path is None:
                    vv_path = t
            elif "-vh-" in bn or "_vh_" in bn or bn.startswith("vh"):
                if vh_path is None:
                    vh_path = t

        bands_found = []
        if vv_path:
            bands_found.append("VV")
        if vh_path:
            bands_found.append("VH")
        result["bands_found"] = bands_found

        _log(f"[EXTRACT S1] Bandes brutes trouvees : {bands_found}")
        _log(f"[EXTRACT S1] VV={vv_path}")
        _log(f"[EXTRACT S1] VH={vh_path}")

        if not vv_path and not vh_path:
            raise RuntimeError(
                "Aucune bande VV ou VH trouvee dans le dossier produit.\n\n"
                "Verifiez que vous avez bien selectionne le dossier "
                "du produit Sentinel-1 decompresse\n"
                "(il doit contenir un sous-dossier 'measurement' "
                "avec des fichiers .tiff).")

        # ---- 2. Vérifier le fichier de sortie en cache ----
        out = os.path.join(output_dir, f"{base}_S1_dual_pol.tif")
        if os.path.isfile(out):
            # Verifier que le GeoTIFF existant a bien une geotransform affine
            cached_ds = gdal.Open(out, gdal.GA_ReadOnly)
            if cached_ds is not None:
                cached_gt = cached_ds.GetGeoTransform()
                cached_valid = (cached_gt is not None and
                                cached_gt != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
                cached_ds = None
                if cached_valid:
                    _log(f"[EXTRACT S1] GeoTIFF cache valide : {out}")
                    result["tiff_s1"] = out
                    if progress_cb:
                        progress_cb("GeoTIFF Sentinel-1 existant (cache) charge.")
                    return result
                else:
                    _log(f"[EXTRACT S1] GeoTIFF cache invalide (geotransform incorrecte), recalcul...")
                    try:
                        os.remove(out)
                    except Exception:
                        pass

        # ---- 3. Etape CRITIQUE : gdal.Warp chaque bande vers EPSG:4326 ----
        #
        # POURQUOI CETTE ETAPE EST OBLIGATOIRE :
        # Les fichiers measurement/*.tiff d'un produit S1 GRD sont stockes
        # avec des GCPs (Ground Control Points) incorpores dans les metadonnees
        # TIFF, et non avec une geotransform affine classique.
        #
        # Si on applique BuildVRT + Translate directement sur ces fichiers :
        #   - Les GCPs sont copies tels quels dans le VRT/TIF de sortie
        #   - QGIS ne peut pas calculer correctement la transformation
        #     pixel -> coordonnees geographiques
        #   - La couche S1 s'affiche decalee, pivotee ou completement
        #     hors zone par rapport aux couches OSM/EPSG:3857
        #
        # gdal.Warp resout ce probleme en :
        #   1. Lisant les GCPs et en interpolant la grille de coordonnees
        #   2. Re-echantillonnant les pixels dans la projection EPSG:4326
        #   3. Produisant un GeoTIFF avec une geotransform affine standard
        #      compatible avec tous les SIG (QGIS, ArcGIS, GDAL, etc.)
        #
        # On warpe chaque bande (VV, VH) individuellement dans un fichier
        # temporaire, puis on les empile en un seul GeoTIFF dual-pol final.

        tmp_dir    = os.path.join(output_dir, "_sv_s1_tmp_")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_files  = []

        try:
            paths_to_stack = []  # [vv_warped, vh_warped] dans cet ordre

            for pol_name, raw_path in [("VV", vv_path), ("VH", vh_path)]:
                if raw_path is None:
                    continue
                tmp_out = os.path.join(
                    tmp_dir,
                    f"{base}_{pol_name}_warp4326.tif")
                tmp_files.append(tmp_out)

                if os.path.isfile(tmp_out):
                    # Verifier validite du fichier temporaire existant
                    tmp_ds = gdal.Open(tmp_out, gdal.GA_ReadOnly)
                    if tmp_ds is not None:
                        tmp_gt = tmp_ds.GetGeoTransform()
                        tmp_valid = (tmp_gt is not None and
                                     tmp_gt != (0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
                        tmp_ds = None
                        if tmp_valid:
                            _log(f"[EXTRACT S1] Tmp cache valide : {tmp_out}")
                            paths_to_stack.append(tmp_out)
                            continue
                        else:
                            try:
                                os.remove(tmp_out)
                            except Exception:
                                pass

                if progress_cb:
                    progress_cb(
                        f"Reprojection {pol_name} vers EPSG:4326 "
                        f"(peut prendre quelques minutes)...")

                warped = _s1_warp_band_to_epsg4326(
                    gdal, raw_path, tmp_out, progress_cb=progress_cb)
                paths_to_stack.append(warped)

            if not paths_to_stack:
                raise RuntimeError(
                    "Aucune bande valide apres reprojection Sentinel-1.")

            # ---- 4. Empiler VV + VH en un seul GeoTIFF dual-pol ----
            if progress_cb:
                n_bands = len(paths_to_stack)
                pols    = "+".join(bands_found[:n_bands])
                progress_cb(
                    f"Creation GeoTIFF dual-pol final ({pols})...")

            _log(f"[EXTRACT S1] Empilement {len(paths_to_stack)} bandes -> {out}")

            vrt = gdal.BuildVRT(
                "",
                paths_to_stack,
                options=gdal.BuildVRTOptions(separate=True))
            if vrt is None:
                raise RuntimeError(
                    "gdal.BuildVRT a retourne None lors de l'empilement "
                    "des bandes Sentinel-1.")

            gdal.Translate(
                out, vrt,
                options=gdal.TranslateOptions(
                    format="GTiff",
                    creationOptions=[
                        "COMPRESS=LZW",
                        "TILED=YES",
                        "BIGTIFF=IF_SAFER"
                    ]))
            vrt = None  # Liberer le VRT en memoire

            # ---- 5. Verification finale ----
            if not os.path.isfile(out):
                raise RuntimeError(
                    f"Le GeoTIFF final n'a pas ete cree : {out}")

            final_ds = gdal.Open(out, gdal.GA_ReadOnly)
            if final_ds is None:
                raise RuntimeError(
                    f"Le GeoTIFF final ne peut pas etre ouvert : {out}")

            final_gt  = final_ds.GetGeoTransform()
            final_crs = final_ds.GetProjection()
            final_nb  = final_ds.RasterCount
            final_ds  = None

            if not final_gt or final_gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
                raise RuntimeError(
                    f"Le GeoTIFF final a une geotransform invalide.\n"
                    f"gt = {final_gt}\n"
                    f"Fichier : {out}")

            sz = os.path.getsize(out) / 1_048_576
            _log(
                f"[EXTRACT S1] GeoTIFF cree : {out} ({sz:.1f} MB)  "
                f"bandes={final_nb}  gt={final_gt[:4]}  "
                f"crs={final_crs[:40] if final_crs else 'NONE'}")

            result["tiff_s1"] = out

        except Exception as e:
            # Nettoyage des fichiers partiels en cas d'erreur
            if os.path.isfile(out):
                try:
                    os.remove(out)
                except Exception:
                    pass
            raise RuntimeError(
                f"Erreur creation GeoTIFF Sentinel-1 : {e}")

        finally:
            # Nettoyage du repertoire temporaire de Warp
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        if progress_cb:
            progress_cb("GeoTIFF Sentinel-1 cree avec succes (EPSG:4326).")

    return result


# MODULE CARTE 

def _make_aoi_layer(bbox):
    lo0, la0, lo1, la1 = bbox
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "AOI", "memory")
    pr    = layer.dataProvider()
    feat  = QgsFeature()
    feat.setGeometry(QgsGeometry.fromRect(QgsRectangle(lo0, la0, lo1, la1)))
    pr.addFeature(feat)
    layer.updateExtents()
    from qgis.core import QgsFillSymbol
    sym = QgsFillSymbol.createSimple({
        "color":         "0,0,0,0",
        "outline_color": "220,50,50,255",
        "outline_width": "1.0",
        "outline_style": "dash"})
    layer.renderer().setSymbol(sym)
    return layer


def _make_single_footprint_layer(result):
    geo  = result.get("footprint")
    qgeo = _geo_to_qgs(geo)
    if qgeo is None or qgeo.isEmpty():
        return None
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326",
                           "Footprint selectionne", "memory")
    pr    = layer.dataProvider()
    feat  = QgsFeature()
    feat.setGeometry(qgeo)
    pr.addFeature(feat)
    layer.updateExtents()
    from qgis.core import QgsFillSymbol
    sym = QgsFillSymbol.createSimple({
        "color":         "41,128,185,60",
        "outline_color": "41,128,185,255",
        "outline_width": "1.2"})
    layer.renderer().setSymbol(sym)
    return layer

# OUTIL DE DESSIN AOI sur QgsMapCanvas

from qgis.core import QgsWkbTypes

class AoiRectangleTool:
    """
    Outil de dessin de rectangle AOI sur le canvas QGIS.
    Callback on_bbox_cb(lo0, la0, lo1, la1) en EPSG:4326.
    """

    def __init__(self, canvas, on_bbox_cb):
        self._canvas    = canvas
        self._cb        = on_bbox_cb
        self._prev_tool = None
        self._tool      = None
        self._rubber    = None
        self._pt_start  = None
        self._active    = False
        self._build_tool()

    def _build_tool(self):
        from qgis.gui import QgsMapTool
        canvas   = self._canvas
        draw_ref = self

        class _RectTool(QgsMapTool):
            def __init__(self, canvas):
                super().__init__(canvas)
                self.setCursor(QCursor(Qt.CrossCursor))

            def canvasPressEvent(self, e):
                if e.button() == Qt.LeftButton:
                    draw_ref._pt_start = self.toMapCoordinates(e.pos())
                    draw_ref._init_rubber()

            def canvasMoveEvent(self, e):
                if draw_ref._pt_start is not None:
                    pt2 = self.toMapCoordinates(e.pos())
                    draw_ref._update_rubber(draw_ref._pt_start, pt2)

            def canvasReleaseEvent(self, e):
                if e.button() == Qt.LeftButton and draw_ref._pt_start is not None:
                    pt2 = self.toMapCoordinates(e.pos())
                    draw_ref._finish(draw_ref._pt_start, pt2)
                    draw_ref._pt_start = None

            def keyPressEvent(self, e):
                if e.key() == Qt.Key_Escape:
                    draw_ref.deactivate()

        self._tool = _RectTool(canvas)

    def _init_rubber(self):
        self._clear_rubber()
        try:
            from qgis.gui import QgsRubberBand
            self._rubber = QgsRubberBand(self._canvas, QgsWkbTypes.PolygonGeometry)
            self._rubber.setColor(QColor(255, 120, 0, 140))
            self._rubber.setFillColor(QColor(255, 180, 0, 40))
            self._rubber.setWidth(2)
        except Exception as e:
            _log(f"RubberBand init : {e}")

    def _update_rubber(self, pt1, pt2):
        if self._rubber is None:
            return
        try:
            rect = QgsRectangle(
                min(pt1.x(), pt2.x()), min(pt1.y(), pt2.y()),
                max(pt1.x(), pt2.x()), max(pt1.y(), pt2.y()))
            self._rubber.reset(QgsWkbTypes.PolygonGeometry)
            self._rubber.addGeometry(QgsGeometry.fromRect(rect), None)
        except Exception:
            pass

    def _clear_rubber(self):
        if self._rubber is not None:
            try:
                self._rubber.reset()
                self._rubber.hide()
            except Exception:
                pass
            self._rubber = None

    def activate(self):
        if self._tool is None:
            return
        self._active    = True
        self._pt_start  = None
        self._prev_tool = self._canvas.mapTool()
        self._canvas.setMapTool(self._tool)

    def deactivate(self):
        self._active   = False
        self._pt_start = None
        self._clear_rubber()
        if self._prev_tool is not None:
            try:
                self._canvas.setMapTool(self._prev_tool)
            except Exception:
                pass
            self._prev_tool = None
        else:
            self._canvas.unsetMapTool(self._tool)

    def _finish(self, pt1, pt2):
        self._clear_rubber()
        self.deactivate()
        crs_canvas = self._canvas.mapSettings().destinationCrs()
        crs_4326   = QgsCoordinateReferenceSystem("EPSG:4326")
        tr = QgsCoordinateTransform(crs_canvas, crs_4326, QgsProject.instance())
        try:
            p1  = tr.transform(QgsPointXY(pt1.x(), pt1.y()))
            p2  = tr.transform(QgsPointXY(pt2.x(), pt2.y()))
            lo0 = min(p1.x(), p2.x()); lo1 = max(p1.x(), p2.x())
            la0 = min(p1.y(), p2.y()); la1 = max(p1.y(), p2.y())
            if lo1 - lo0 > 0.0001 and la1 - la0 > 0.0001:
                self._cb(lo0, la0, lo1, la1)
            else:
                _log("AOI dessinee trop petite — ignoree.")
        except Exception as e:
            _log(f"Dessin AOI reprojection : {e}")


AoiDrawTool = AoiRectangleTool

# MODULE indices spectraux

def _read_band(ds, idx):
    b   = ds.GetRasterBand(idx)
    arr = b.ReadAsArray().astype(np.float32)
    nd  = b.GetNoDataValue()
    if nd is not None:
        arr[arr == nd] = np.nan
    return arr

def _ratio(num, den, lo=-1., hi=1.):
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(np.abs(den) < 1e-6, np.nan, num / den)
    return np.clip(r, lo, hi)

def compute_ndvi(ds):
    n = ds.RasterCount
    if n == 4:
        red, nir = _read_band(ds, 3), _read_band(ds, 4)
    elif n >= 5:
        # Dans 20m_multi : B03=1, B04=2, B08=3
        red, nir = _read_band(ds, 2), _read_band(ds, 3)
    else:
        raise ValueError(f"NDVI : >= 4 bandes attendues. Trouve : {n}")
    return _ratio(nir - red, nir + red)

def compute_ndwi(ds):
    n = ds.RasterCount
    if n == 4:
        g, nir = _read_band(ds, 2), _read_band(ds, 4)
    elif n >= 5:
        # Dans 20m_multi : B03=1, B08=3
        g, nir = _read_band(ds, 1), _read_band(ds, 3)
    else:
        raise ValueError(f"NDWI : >= 4 bandes attendues. Trouve : {n}")
    return _ratio(g - nir, g + nir)

def compute_nmsi(ds):
    n = ds.RasterCount
    if n >= 5:
        # B08=bande 3, B11=bande 5 dans le 20m_multi
        nir  = _read_band(ds, 3)
        swir = _read_band(ds, 5)
    elif n >= 11:
        nir  = _read_band(ds, 8)
        swir = _read_band(ds, 11)
    else:
        raise ValueError(
            f"NMSI necessite >= 5 bandes (GeoTIFF 20m).\n"
            f"Trouve : {n} bandes.\n"
            f"Chargez le fichier _20m_multi.tif")
    return _ratio(swir - nir, swir + nir)

def compute_ndbi(ds):
    n = ds.RasterCount
    if n < 5:
        raise ValueError(
            "NDBI necessite >= 5 bandes (GeoTIFF 20m).\n"
            "Chargez _20m_multi ou _full_multi.")
    return compute_nmsi(ds)

def compute_ratio_vv_vh(ds):
    if ds.RasterCount < 2:
        raise ValueError("Sentinel-1 : 2 bandes (VV + VH) attendues.")
    vv = _read_band(ds, 1); vh = _read_band(ds, 2)
    vv = np.where(vv <= 0, np.nan, vv)
    vh = np.where(vh <= 0, np.nan, vh)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(np.isnan(vh) | (vh < 1e-6), np.nan, vv / vh)
    return np.clip(r, .1, 50.)

def compute_rvi_s1(ds):
    if ds.RasterCount < 2:
        raise ValueError("Sentinel-1 : 2 bandes (VV + VH) attendues.")
    vv = _read_band(ds, 1); vh = _read_band(ds, 2)
    if np.nanmean(vv) < 0:
        vv = 10 ** (vv / 10.0); vh = 10 ** (vh / 10.0)
    vv = np.where(vv <= 0, np.nan, vv)
    vh = np.where(vh <= 0, np.nan, vh)
    denom = vv + vh
    with np.errstate(divide="ignore", invalid="ignore"):
        rvi = np.where((denom < 1e-10) | np.isnan(denom),
                       np.nan, 4.0 * vh / denom)
    return np.clip(rvi, 0.0, 1.0)


def compute_index_by_blocks(ds, func, out_path,
                             block_size=1024, nodata=-9999.):
    from osgeo import gdal
    rows  = ds.RasterYSize
    cols  = ds.RasterXSize
    gt    = ds.GetGeoTransform()
    if gt is None or gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
        raise RuntimeError("Dataset sans geotransform valide.")
    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        out_path, cols, rows, 1, gdal.GDT_Float32,
        options=["COMPRESS=LZW", "TILED=YES",
                 "BLOCKXSIZE=512", "BLOCKYSIZE=512"])
    out_ds.SetGeoTransform(gt)
    out_ds.SetProjection(ds.GetProjection())
    out_band = out_ds.GetRasterBand(1)
    out_band.SetNoDataValue(nodata)
    count = 0; mean = 0.0; M2 = 0.0
    vmin = float("inf"); vmax = float("-inf")

    class _BlockDS:
        def __init__(self, src, ro, co, w, h):
            self._src = src; self._ro = ro; self._co = co
            self._w = w; self._h = h
            self.RasterCount = src.RasterCount
        def GetRasterBand(self, idx):
            return _BlockBand(self._src, idx,
                              self._ro, self._co, self._w, self._h)

    class _BlockBand:
        def __init__(self, src, idx, ro, co, w, h):
            self._src = src; self._idx = idx
            self._ro = ro; self._co = co; self._w = w; self._h = h
        def ReadAsArray(self):
            return self._src.GetRasterBand(self._idx).ReadAsArray(
                self._co, self._ro, self._w, self._h)
        def GetNoDataValue(self):
            return self._src.GetRasterBand(self._idx).GetNoDataValue()

    for row_off in range(0, rows, block_size):
        h = min(block_size, rows - row_off)
        for col_off in range(0, cols, block_size):
            w = min(block_size, cols - col_off)
            try:
                arr = func(_BlockDS(ds, row_off, col_off, w, h))
            except Exception:
                arr = np.full((h, w), np.nan, dtype=np.float32)
            out_band.WriteArray(
                np.where(np.isnan(arr), nodata, arr),
                col_off, row_off)
            valid = arr[~np.isnan(arr)]
            if valid.size > 0:
                vmin = min(vmin, float(valid.min()))
                vmax = max(vmax, float(valid.max()))
                for x in valid:
                    count += 1
                    delta = x - mean
                    mean += delta / count
                    M2   += delta * (x - mean)

    out_band.FlushCache()
    out_ds = None
    std = float(np.sqrt(M2 / count)) if count > 1 else 0.0
    return {
        "min":  float(vmin) if count > 0 else 0.0,
        "max":  float(vmax) if count > 0 else 0.0,
        "mean": float(mean) if count > 0 else 0.0,
        "std":  float(std),
    }

def write_raster(out_path, arr, ref_ds, nodata=-9999.):
    """
    Ecrit un tableau numpy 2D en GeoTIFF Float32 en copiant le
    georeferencement du dataset de reference.

    CORRECTION v10.1 :
    Si ref_ds possede une geotransform invalide (identite ou nulle),
    ce qui peut arriver si l'utilisateur charge accidentellement un
    fichier S1 brut non warp, une RuntimeError explicite est levee
    plutot que de produire silencieusement un raster mal georeference.
    """
    from osgeo import gdal

    # Verifier la geotransform du dataset de reference
    gt = ref_ds.GetGeoTransform()
    if gt is None or gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
        raise RuntimeError(
            "Le dataset de reference n'a pas de geotransform affine valide.\n"
            "Assurez-vous d'avoir charge le GeoTIFF _S1_dual_pol.tif "
            "genere par 'Creer GeoTIFF' (etape de reprojection incluse),\n"
            "et non un fichier .tiff brut du dossier measurement/.")

    rows, cols = arr.shape
    ds = gdal.GetDriverByName("GTiff").Create(
        out_path, cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform(gt)
    ds.SetProjection(ref_ds.GetProjection())
    b = ds.GetRasterBand(1)
    b.WriteArray(np.where(np.isnan(arr), nodata, arr))
    b.SetNoDataValue(nodata)
    b.FlushCache()
    ds = None


def raster_stats(arr):
    v = arr[~np.isnan(arr)]
    if v.size == 0:
        return {}
    return {
        "min":  float(v.min()),
        "max":  float(v.max()),
        "mean": float(v.mean()),
        "std":  float(v.std()),
    }

def load_in_qgis(iface, path, name):
    if iface is None:
        return
    layer = QgsRasterLayer(path, name)
    if not layer.isValid():
        raise RuntimeError(f"Raster invalide : {path}")
    QgsProject.instance().addMapLayer(layer)


# HISTORIQUE

def _hist_path(d):
    return os.path.join(d, "sentinelvision_history.json")

def _load_hist(d):
    p = _hist_path(d)
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            r = json.load(f)
        return r if isinstance(r, list) else []
    except Exception:
        return []

def _save_hist(d, entries):
    with open(_hist_path(d), "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

def hist_add(plugin_dir, etype, **kw):
    entries = _load_hist(plugin_dir)
    e = {"type": etype,
         "timestamp": datetime.now().isoformat(timespec="seconds"),
         **kw}
    entries.append(e)
    _save_hist(plugin_dir, entries)
    return e

def hist_all(plugin_dir):
    return sorted(_load_hist(plugin_dir),
                  key=lambda x: x.get("timestamp", ""),
                  reverse=True)

def hist_clear(plugin_dir):
    _save_hist(plugin_dir, [])

def hist_to_rows(entries):
    rows = []
    for e in entries:
        t = e.get("type", "?")
        if t == "download":
            detail = e.get("title", e.get("product_id", "?"))[:60]
            status = "Termine"
        elif t == "index":
            s      = e.get("stats", {})
            detail = f"Indice {e.get('index', '?')}"
            if isinstance(s.get("mean"), float):
                detail += f" moy={s['mean']:.3f}"
            status = "Calcule"
        elif t == "extract":
            detail = e.get("product_dir", e.get("zip_file", "?"))[:60]
            status = "Extrait"
        else:
            detail = str(e)[:60]
            status = "?"
        rows.append([
            e.get("timestamp", "?"),
            t.capitalize(),
            detail,
            status,
            os.path.basename(e.get("output_file", "?")),
        ])
    return rows

# WORKERS

class _DlWorker(QThread):
    progress = pyqtSignal(int, int, float)
    done     = pyqtSignal(str)
    error    = pyqtSignal(str)

    def __init__(self, token, product_id, product_title,
                 output_dir, cache_dir=None):
        super().__init__()
        self._token    = token
        self._pid      = product_id
        self._title    = product_title
        self._outdir   = output_dir
        self._cachedir = cache_dir
        self._cancel   = [False]

    def cancel(self):
        self._cancel[0] = True

    def run(self):
        def _cb(done, total, elapsed):
            self.progress.emit(done, total, elapsed)
        try:
            path = download_product(
                self._token, self._pid, self._title,
                self._outdir, progress_cb=_cb,
                cache_dir=self._cachedir,
                cancel_flag=self._cancel)
            self.done.emit(path)
        except RuntimeError as e:
            msg = str(e)
            self.error.emit("__CANCELLED__" if "__CANCELLED__" in msg else msg)
        except Exception as e:
            self.error.emit(str(e))


class _ExtractWorker(QThread):
    """Worker pour l'extraction GeoTIFF depuis un dossier SAFE / produit S1."""
    progress = pyqtSignal(str)
    done     = pyqtSignal(object)
    error    = pyqtSignal(str)

    def __init__(self, product_dir, output_dir, satellite="S2"):
        super().__init__()
        self._product_dir = product_dir
        self._outdir      = output_dir
        self._satellite   = satellite

    def run(self):
        try:
            result = extract_geotiff_from_safe(
                self._product_dir,
                self._outdir,
                satellite   = self._satellite,
                progress_cb = lambda msg: self.progress.emit(msg))
            self.done.emit(result)
        except Exception as e:
            self.error.emit(str(e))


# DIALOG HISTORIQUE

class HistoriqueDialog(QDialog):
    def __init__(self, plugin_dir, parent=None):
        super().__init__(parent)
        self.plugin_dir = plugin_dir
        self.setWindowTitle("Historique — SentinelVision")
        self.resize(860, 420)
        self._build()

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)
        bar = QHBoxLayout()
        bar.addWidget(_bold("Historique des operations"))
        bar.addStretch()
        for label, cb in [
            ("Actualiser",   self._refresh),
            ("Effacer tout", self._clear),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(24)
            b.clicked.connect(cb)
            bar.addWidget(b)
            bar.addSpacing(4)
        v.addLayout(bar)

        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(
            ["Horodatage", "Type", "Detail", "Statut", "Fichier"])
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setAlternatingRowColors(True)
        v.addWidget(self.tbl)
        self._refresh()

    def _refresh(self):
        rows = hist_to_rows(hist_all(self.plugin_dir))
        self.tbl.blockSignals(True)
        self.tbl.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QTableWidgetItem(str(val))
                item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                self.tbl.setItem(r, c, item)
        self.tbl.blockSignals(False)

    def _clear(self):
        if QMessageBox.question(
                self, "Effacer",
                "Effacer tout l'historique ?",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            hist_clear(self.plugin_dir)
            self._refresh()

    def _open_folder(self):
        import subprocess, sys
        p = self.plugin_dir
        if sys.platform == "win32":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])

# DIALOG PRINCIPAL 

class SentinelVisionDialog(QDialog):
    PAGE_DL     = 0
    PAGE_RASTER = 1

    def __init__(self, iface=None, parent=None):
        super().__init__(parent,
            Qt.Window |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowCloseButtonHint)
        self.iface      = iface
        self.plugin_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))

        # State
        self._aoi         = None
        self._token       = None
        self._results     = []
        self._coverage    = None
        self._sel_result  = None
        self._last_outdir = ""
        self._last_zip    = ""
        self._s2_ds       = None
        self._s1_ds       = None
        self._s2_path     = None
        self._s1_path     = None
        self._dl_worker   = None
        self._ex_worker   = None
        self._hist_dlg    = None

        # Panier de telechargement
        self._cart           = []
        self._cart_worker    = None
        self._cart_running   = False

        # Couches carte
        self._layer_osm   = None
        self._layer_aoi   = None
        self._layer_fp    = None

        # Outil dessin
        self._draw_tool   = None

        self.setWindowTitle("GeoAssist Sentinel")
        self.resize(1150, 800)
        self._build_ui()

    # CONSTRUCTION UI

    def _build_ui(self):
        root = QVBoxLayout(self)        
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Barre de titre PLEINE LARGEUR (comme vecteur.py)
        root.addWidget(self._build_title_bar())
        root.addWidget(_sep())

        # Corps : sidebar + contenu principal
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # -- Sidebar --
        sidebar = QWidget()
        sidebar.setFixedWidth(182)
        sv = QVBoxLayout(sidebar)
        sv.setContentsMargins(4, 10, 4, 8)
        sv.setSpacing(2)

        sv.addSpacing(6)
        sv.addWidget(_bold("Navigation"))
        sv.addSpacing(4)

        self._nav_btns = []
        for label, idx in [
            ("Telechargement",   self.PAGE_DL),
            ("Calcul d'indices", self.PAGE_RASTER),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setObjectName("modeBtn")
            btn.clicked.connect(lambda _, i=idx: self._switch_page(i))
            sv.addWidget(btn)
            self._nav_btns.append(btn)

        sv.addSpacing(6)
        sv.addWidget(_sep())
        sv.addSpacing(4)
        btn_hist = QPushButton("Historique")
        btn_hist.clicked.connect(self._open_hist)
        sv.addWidget(btn_hist)
        sv.addStretch()
        lver = QLabel("v1.0 — CDSE / GDAL")
        lver.setAlignment(Qt.AlignCenter)
        sv.addWidget(lver)

        body.addWidget(sidebar)

        # -- Zone principale --
        main = QWidget()
        mv = QVBoxLayout(main)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(0)

        topbar = QWidget()
        topbar.setFixedHeight(30)
        tv = QHBoxLayout(topbar)
        tv.setContentsMargins(10, 0, 8, 0)
        self.lbl_page_title = QLabel("Telechargement")
        f3 = QFont(); f3.setBold(True); f3.setPointSize(10)
        self.lbl_page_title.setFont(f3)
        tv.addWidget(self.lbl_page_title)
        tv.addStretch()
        mv.addWidget(topbar)
        mv.addWidget(_sep())

        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_download())
        self.stack.addWidget(self._page_raster())
        mv.addWidget(self.stack)

        body.addWidget(main)
        root.addLayout(body, 1)

        self.setStyleSheet(self.styleSheet() + """
            #titleBar { background: #ffffff; border-bottom: 1px solid #dddddd; }
            #pluginIcon { color: #333333; background: #eeeeee;
                        border: 1px solid #cccccc; border-radius: 4px; }
            #pluginTitle  { color: #222222; }
            #pluginSubtitle { color: #777777; font-size: 11px; }
            QGroupBox {
                font-weight: bold; font-size: 11px; color: #333333;
                border: 1px solid #dddddd; border-radius: 4px;
                margin-top: 8px; padding-top: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 10px; padding: 0 6px;
            }
            QPushButton#modeBtn {
                background: #ffffff; color: #444444;
                border: 1px solid #cccccc; border-radius: 4px;
                padding: 6px 14px; font-size: 11px;
            }
            QPushButton#modeBtn:checked {
                background: #333333; color: #ffffff; border-color: #333333;
            }
            QPushButton#modeBtn:hover { background: #f0f0f0; }
            QPushButton#modeBtn:checked:hover { background: #555555; }              
        """)
        self._switch_page(self.PAGE_DL)

    def _build_title_bar(self):
        bar = QFrame()
        bar.setObjectName("titleBar")
        bar.setFixedHeight(48)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(10)

        # Badge logo
        icon = QLabel("GS")
        icon.setFont(QFont("Arial", 11, QFont.Bold))
        icon.setObjectName("pluginIcon")
        icon.setFixedWidth(32)
        icon.setAlignment(Qt.AlignCenter)

        # Titre principal
        title = QLabel("GeoAssist Sentinel")
        title.setObjectName("pluginTitle")
        title.setFont(QFont("Arial", 13, QFont.Bold))

        # Sous-titre
        sub = QLabel("Téléchargement et analyse Sentinel ")
        sub.setObjectName("pluginSubtitle")

        lay.addWidget(icon)
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addStretch()

        # Badge connexion (déplacé ici depuis topbar)
        self.lbl_conn_indicator = QPushButton("🟠 Vérification...")
        self.lbl_conn_indicator.setEnabled(False)
        self.lbl_conn_indicator.setFixedHeight(26)
        self.lbl_conn_indicator.setFixedWidth(160)
        self.lbl_conn_indicator.setToolTip("Aucune connexion active ou authentification invalide")
        self.lbl_conn_indicator.setStyleSheet("""
            QPushButton {
                background-color: #e67e22;
                color: white;
                border-radius: 12px;
                font-weight: bold;
                font-size: 11px;
                border: none;
                padding: 0 10px;
            }
        """)
        lay.addWidget(self.lbl_conn_indicator)

        return bar

    # PAGE 0 — TELECHARGEMENT

    def _page_download(self):
        splitter = QSplitter(Qt.Horizontal)

        # -- Gauche --
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        left = QWidget()
        v = QVBoxLayout(left)
        v.setContentsMargins(10, 10, 6, 10)
        v.setSpacing(8)

        #  Connexion CDSE 
        grp_conn = QGroupBox("Connexion Copernicus")
        gc = QVBoxLayout(grp_conn); gc.setSpacing(5)
        rc = QHBoxLayout(); rc.setSpacing(4)
        self.inp_email = QLineEdit()
        self.inp_email.setPlaceholderText("Email")
        self.inp_pwd = QLineEdit()
        self.inp_pwd.setPlaceholderText("Mot de passe")
        self.inp_pwd.setEchoMode(QLineEdit.Password)
        btn_conn = QPushButton("Connexion")
        btn_conn.setFixedWidth(80)
        btn_conn.clicked.connect(self._connect_cdse)
        rc.addWidget(self.inp_email)
        rc.addWidget(self.inp_pwd)
        rc.addWidget(btn_conn)
        gc.addLayout(rc)
        self.lbl_conn = _lbl("Non connecte.")
        gc.addWidget(self.lbl_conn)
        v.addWidget(grp_conn)

        #AOI 
        grp_aoi = QGroupBox("Zone d'interet (AOI)")
        ga = QVBoxLayout(grp_aoi)
        ga.setSpacing(5)

        ga.addWidget(QLabel("Nom de lieu :"))
        rg = QHBoxLayout(); rg.setSpacing(4)
        self.inp_place = QLineEdit()
        self.inp_place.setPlaceholderText("Ville, region, pays...")
        self.inp_place.returnPressed.connect(self._geocode)
        btn_geo = QPushButton("Rechercher")
        btn_geo.setFixedWidth(90)
        btn_geo.clicked.connect(self._geocode)
        rg.addWidget(self.inp_place); rg.addWidget(btn_geo)
        ga.addLayout(rg)

        ga.addWidget(_sep())
        ga.addWidget(QLabel("Coordonnees manuelles (EPSG:4326) :"))

        def _sp(lo, hi, val):
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi); sp.setValue(val); sp.setDecimals(4)
            sp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            return sp

        self.sp_lo0 = _sp(-180, 180, -6.31)
        self.sp_la0 = _sp(-90,   90, 33.50)
        self.sp_lo1 = _sp(-180, 180, -6.22)
        self.sp_la1 = _sp(-90,   90, 34.90)

        grid = QGridLayout(); grid.setSpacing(4)
        for c, t in enumerate(["Lon min (W)", "Lat min (S)",
                                "Lon max (E)", "Lat max (N)"]):
            grid.addWidget(QLabel(t), 0, c)
        for c, sp in enumerate([self.sp_lo0, self.sp_la0,
                                 self.sp_lo1, self.sp_la1]):
            grid.addWidget(sp, 1, c)
        ga.addLayout(grid)

        rb = QHBoxLayout(); rb.addStretch()
        btn_bbox = QPushButton("Valider")
        btn_bbox.setFixedWidth(80)
        btn_bbox.clicked.connect(self._set_manual_bbox)
        rb.addWidget(btn_bbox)
        ga.addLayout(rb)

        ga.addWidget(_sep())
        ga.addWidget(QLabel("Dessiner sur la carte :"))
        rdraw = QHBoxLayout(); rdraw.setSpacing(4)
        self.btn_draw_aoi = QPushButton("Dessiner AOI")
        self.btn_draw_aoi.setFixedHeight(26)
        self.btn_draw_aoi.setToolTip(
            "Cliquez puis faites glisser un rectangle sur la carte.")
        self.btn_draw_aoi.clicked.connect(self._start_draw_aoi)

        self.btn_delete_aoi = QPushButton("Supprimer AOI")
        self.btn_delete_aoi.setFixedHeight(26)
        self.btn_delete_aoi.setToolTip("Supprime l'AOI courante.")
        self.btn_delete_aoi.clicked.connect(self._clear_aoi)

        rdraw.addWidget(self.btn_draw_aoi)
        rdraw.addWidget(self.btn_delete_aoi)
        rdraw.addStretch()
        ga.addLayout(rdraw)

        self.lbl_aoi = _lbl("Aucune AOI definie.")
        ga.addWidget(self.lbl_aoi)
        v.addWidget(grp_aoi)

        # Resultats catalogue
        grp_cat = QGroupBox("Resultats catalogue")
        gcat = QVBoxLayout(grp_cat); gcat.setSpacing(5)

        rs = QHBoxLayout(); rs.setSpacing(6)
        rs.addWidget(QLabel("Satellite :"))
        self.cmb_sat = QComboBox()
        self.cmb_sat.addItems(["Sentinel-2 L2A", "Sentinel-1 GRD"])
        self.cmb_sat.setFixedWidth(148)
        self.cmb_sat.currentIndexChanged.connect(self._on_sat_changed)
        rs.addWidget(self.cmb_sat)
        rs.addSpacing(8)
        rs.addWidget(QLabel("Du :"))
        self.dt_start = _date_edit(DATE_TODAY.addMonths(-1), DATE_S2_MIN)
        rs.addWidget(self.dt_start)
        rs.addWidget(QLabel("Au :"))
        self.dt_end = _date_edit(DATE_TODAY, DATE_S2_MIN)
        rs.addWidget(self.dt_end)
        rs.addStretch()
        gcat.addLayout(rs)

        rcc = QHBoxLayout(); rcc.setSpacing(6)
        rcc.addWidget(QLabel("Nuages max :"))
        self.sld_cloud = QSlider(Qt.Horizontal)
        self.sld_cloud.setRange(0, 100)
        self.sld_cloud.setValue(20)
        self.lbl_cloud_val = QLabel("20 %")
        self.lbl_cloud_val.setFixedWidth(36)
        self.sld_cloud.valueChanged.connect(
            lambda v: self.lbl_cloud_val.setText(f"{v} %"))
        rcc.addWidget(self.sld_cloud)
        rcc.addWidget(self.lbl_cloud_val)
        rcc.addStretch()
        btn_search = QPushButton("Lancer la recherche")
        btn_search.setFixedWidth(148)
        btn_search.clicked.connect(self._search_catalogue)
        rcc.addWidget(btn_search)
        gcat.addLayout(rcc)

        self.tbl_cat = QTableWidget(0, 5)
        self.tbl_cat.setHorizontalHeaderLabels(
            ["Date", "Nom produit", "Nuages (%)", "Taille (MB)", "Panier"])
        hh = self.tbl_cat.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tbl_cat.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_cat.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_cat.setAlternatingRowColors(True)
        self.tbl_cat.setMinimumHeight(150)
        self.tbl_cat.itemSelectionChanged.connect(self._on_cat_selection)
        gcat.addWidget(self.tbl_cat)

        radd = QHBoxLayout(); radd.setSpacing(6)
        self.btn_add_all = QPushButton("+ Tout ajouter au panier")
        self.btn_add_all.setFixedHeight(26)
        self.btn_add_all.setEnabled(False)
        self.btn_add_all.setToolTip(
            "Ajoute tous les resultats de la recherche dans le panier.")
        self.btn_add_all.clicked.connect(self._cart_add_all)
        radd.addWidget(self.btn_add_all); radd.addStretch()
        gcat.addLayout(radd)
        v.addWidget(grp_cat)

        # Panier 
        grp_cart = QGroupBox("Panier de telechargement")
        gcart = QVBoxLayout(grp_cart); gcart.setSpacing(5)

        cart_hdr = QHBoxLayout(); cart_hdr.setSpacing(6)
        self.lbl_cart_count = _lbl("0 item — 0.0 MB total")
        cart_hdr.addWidget(self.lbl_cart_count); cart_hdr.addStretch()
        self.btn_cart_start = QPushButton("Telecharger tout le panier")
        self.btn_cart_start.setFixedHeight(26); self.btn_cart_start.setEnabled(False)
        self.btn_cart_start.setToolTip(
            "Lance le telechargement sequentiel de tous les items.\n"
            "Chaque ZIP est verifie et enregistre dans le dossier de sortie.")
        self.btn_cart_start.clicked.connect(self._cart_start_queue)
        btn_cart_vider = QPushButton("Vider le panier")
        btn_cart_vider.setFixedHeight(26)
        btn_cart_vider.clicked.connect(self._cart_clear)
        cart_hdr.addWidget(self.btn_cart_start)
        cart_hdr.addWidget(btn_cart_vider)
        gcart.addLayout(cart_hdr)

        self.tbl_cart = QTableWidget(0, 5)
        self.tbl_cart.setHorizontalHeaderLabels(
            ["Produit", "Date", "MB", "Statut", "Action"])
        hc = self.tbl_cart.horizontalHeader()
        hc.setSectionResizeMode(QHeaderView.Stretch)
        hc.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hc.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hc.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hc.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.tbl_cart.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_cart.setAlternatingRowColors(True)
        self.tbl_cart.setMinimumHeight(110)
        gcart.addWidget(self.tbl_cart)

        self.pb_cart = QProgressBar()
        self.pb_cart.setRange(0, 100); self.pb_cart.setValue(0)
        self.pb_cart.setFixedHeight(16); self.pb_cart.setVisible(False)
        gcart.addWidget(self.pb_cart)
        self.lbl_cart_prog = QLabel(); self.lbl_cart_prog.setVisible(False)
        gcart.addWidget(self.lbl_cart_prog)
        self.lbl_cart_status = _lbl("Panier vide.")
        gcart.addWidget(self.lbl_cart_status)
        v.addWidget(grp_cart)

        # Telechargement produit selectionne 
        grp_dl = QGroupBox("Telechargement du produit selectionne")
        gdl = QVBoxLayout(grp_dl); gdl.setSpacing(5)

        rod = QHBoxLayout(); rod.setSpacing(4)
        rod.addWidget(QLabel("Dossier :"))
        self.inp_outdir = QLineEdit()
        self.inp_outdir.setPlaceholderText("Dossier de sortie...")
        btn_br = QPushButton("Parcourir")
        btn_br.setFixedWidth(82)
        btn_br.clicked.connect(self._browse_outdir)
        rod.addWidget(self.inp_outdir)
        rod.addWidget(btn_br)
        gdl.addLayout(rod)

        rdb = QHBoxLayout(); rdb.setSpacing(6)
        self.btn_dl = QPushButton("Telecharger")
        self.btn_dl.setFixedHeight(28)
        self.btn_dl.clicked.connect(self._start_dl)

        self.btn_cancel = QPushButton("Annuler")
        self.btn_cancel.setFixedHeight(28)
        self.btn_cancel.setFixedWidth(70)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_dl)

        rdb.addWidget(self.btn_dl)
        rdb.addWidget(self.btn_cancel)
        rdb.addStretch()
        gdl.addLayout(rdb)

        self.pb = QProgressBar()
        self.pb.setRange(0, 100)
        self.pb.setValue(0)
        self.pb.setVisible(False)
        self.pb.setFixedHeight(18)
        self.pb.setTextVisible(True)
        gdl.addWidget(self.pb)
        self.lbl_pb = QLabel()
        self.lbl_pb.setVisible(False)
        gdl.addWidget(self.lbl_pb)
        self.lbl_dl = _lbl("En attente.")
        gdl.addWidget(self.lbl_dl)

        v.addWidget(grp_dl)
        v.addStretch()
        scroll.setWidget(left)

        #  Carte 
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 10, 10, 10)
        rv.setSpacing(6)
        rv.addWidget(_bold("Carte"))

        self._canvas = self._build_canvas()
        if self._canvas is not None:
            rv.addWidget(self._canvas)
            mb = QHBoxLayout(); mb.setSpacing(4)

            btn_plus = QPushButton("+")
            btn_plus.setFixedSize(28, 24)
            btn_plus.setToolTip("Zoom avant")
            btn_plus.clicked.connect(lambda: self._canvas.zoomIn())

            btn_minus = QPushButton("-")
            btn_minus.setFixedSize(28, 24)
            btn_minus.setToolTip("Zoom arriere")
            btn_minus.clicked.connect(lambda: self._canvas.zoomOut())

            self.btn_draw_map = QPushButton("Dessiner AOI")
            self.btn_draw_map.setFixedHeight(24)
            self.btn_draw_map.setToolTip(
                "Cliquez puis faites glisser un rectangle sur la carte.")
            self.btn_draw_map.clicked.connect(self._start_draw_aoi)

            self.btn_delete_map = QPushButton("Supprimer AOI")
            self.btn_delete_map.setFixedHeight(24)
            self.btn_delete_map.setToolTip("Supprime l'AOI actuellement affichée.")
            self.btn_delete_map.clicked.connect(self._clear_aoi)


            self.btn_fp_toggle = QPushButton("Footprint : OFF")
            self.btn_fp_toggle.setFixedHeight(24)
            self.btn_fp_toggle.setCheckable(True)
            self.btn_fp_toggle.setToolTip(
                "Afficher ou masquer le footprint du produit selectionne.\n"
                "Zoome automatiquement sur le footprint a l'activation.")
            self.btn_fp_toggle.clicked.connect(self._toggle_footprint)

            for b in [btn_plus, btn_minus, self.btn_draw_map, self.btn_delete_map, self.btn_fp_toggle]:
                mb.addWidget(b)
            mb.addStretch()
            rv.addLayout(mb)
        else:
            rv.addWidget(_lbl("QgsMapCanvas non disponible."))

        splitter.addWidget(scroll)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 52)
        splitter.setStretchFactor(1, 48)
        return splitter


    # PAGE 1 — CALCUL D'INDICES

    def _page_raster(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(14)

        # Choix satellite 
        grp_sat = QGroupBox("Choix du satellite")
        gs = QHBoxLayout(grp_sat)
        gs.setSpacing(12)
        gs.setContentsMargins(10, 8, 10, 8)
        gs.addWidget(QLabel("Satellite :"))
        self.cmb_sat_r = QComboBox()
        self.cmb_sat_r.addItems(["Sentinel-2", "Sentinel-1"])
        self.cmb_sat_r.setFixedWidth(180)
        self.cmb_sat_r.currentIndexChanged.connect(self._on_sat_raster_changed)
        gs.addWidget(self.cmb_sat_r)
        gs.addStretch()
        v.addWidget(grp_sat)

        # ── Extraction GeoTIFF depuis dossier SAFE / produit decompresse ──────
        # grp_ext est stocke en attribut pour permettre la mise a jour
        # dynamique du titre par _on_sat_raster_changed()
        self.grp_ext = QGroupBox("Preparation des donnees Sentinel-2")
        grp_ext = self.grp_ext          # alias local conserve pour la suite
        ge = QVBoxLayout(grp_ext)
        ge.setSpacing(8)
        ge.setContentsMargins(10, 10, 10, 10)

        # Hint initialise pour S2 ; mis a jour par _on_sat_raster_changed()
        self.lbl_ext_hint = _lbl(
            "Selectionnez le dossier .SAFE decompresse.\n\n"
            "Cette operation creera :\n"
            "  * Une image multispectrale 10 m (NDVI, NDWI)\n"
            "  * Une image multispectrale 20 m (NDVI, NDWI, NDBI, NMSI)")
        ge.addWidget(self.lbl_ext_hint)

        rex = QHBoxLayout(); rex.setSpacing(8)
        self.btn_select_safe = QPushButton("Selectionner le dossier")
        self.btn_select_safe.setFixedSize(200, 30)
        self.btn_select_safe.clicked.connect(self._select_safe_dir)

        self.btn_extract_safe = QPushButton("Creer GeoTIFF")
        self.btn_extract_safe.setFixedSize(150, 30)
        self.btn_extract_safe.setEnabled(False)
        self.btn_extract_safe.setToolTip(
            "Genere les GeoTIFF multibandes a partir du dossier selectionne.\n"
            "Pour Sentinel-1, une reprojection gdal.Warp vers EPSG:4326\n"
            "est effectuee pour assurer la superposition OSM.")
        self.btn_extract_safe.clicked.connect(self._start_extract_safe)
        rex.addWidget(self.btn_select_safe)
        rex.addWidget(self.btn_extract_safe)
        rex.addStretch()
        ge.addLayout(rex)

        self.lbl_safe_selected = _lbl("Aucun dossier selectionne.")
        ge.addWidget(self.lbl_safe_selected)

        self.pb_extract = QProgressBar()
        self.pb_extract.setRange(0, 0)   # Mode indeterminate
        self.pb_extract.setFixedHeight(16)
        self.pb_extract.setVisible(False)
        ge.addWidget(self.pb_extract)

        self.lbl_extract_status = _lbl("")
        ge.addWidget(self.lbl_extract_status)
        v.addWidget(grp_ext)

        # Chargement image pour calcul d'indices 
        # grp_load stocke en attribut pour mise a jour dynamique du titre
        self.grp_load = QGroupBox("Choisir l'image pour le calcul des indices")
        grp_load = self.grp_load          # alias local : aucune autre ligne a changer
        gl = QVBoxLayout(grp_load)
        gl.setSpacing(10)
        gl.setContentsMargins(10, 10, 10, 10)
        # Hint initialise pour S2 ; mis a jour par _update_load_group_ui()
        self.lbl_load_hint = _lbl(
            "Chargez l'une des images multispectrales creees precedemment :\n"
            "  * Image multispectrale 10 m\n"
            "  * Image multispectrale 20 m\n\n"
            "Choisissez le fichier adapte a l'indice que vous souhaitez calculer.")
        gl.addWidget(self.lbl_load_hint)

        rl = QHBoxLayout(); rl.setSpacing(10)
        self.btn_load = QPushButton("Charger une image")
        self.btn_load.setFixedSize(160, 30)
        self.btn_load.clicked.connect(self._load_raster_auto)
        self.btn_load_recent = QPushButton("Dossier recent")
        self.btn_load_recent.setFixedSize(140, 30)
        self.btn_load_recent.setToolTip("Ouvre le dernier dossier utilise")
        self.btn_load_recent.clicked.connect(self._load_from_recent)
        rl.addWidget(self.btn_load)
        rl.addWidget(self.btn_load_recent)
        rl.addStretch()
        gl.addLayout(rl)

        self.lbl_loaded = _lbl("Aucune image selectionnee.")
        gl.addWidget(self.lbl_loaded)
        v.addWidget(grp_load)

        # Calcul d'indices
        grp_idx = QGroupBox("Calcul d'indices")
        gi = QVBoxLayout(grp_idx)
        gi.setSpacing(12)
        gi.setContentsMargins(10, 10, 10, 10)
        gi.addWidget(QLabel("Selectionner un indice a calculer :"))

        # Sentinel-2
        self.row_s2 = QWidget()
        r2 = QHBoxLayout(self.row_s2)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.setSpacing(10)
        self.btn_ndvi = QPushButton("NDVI")
        self.btn_ndwi = QPushButton("NDWI")
        self.btn_nmsi = QPushButton("NMSI")
        self.btn_ndbi = QPushButton("NDBI")
        for btn, tip, ix in [
            (self.btn_ndvi,
             "NDVI = (NIR - Red) / (NIR + Red)\n"
             "Bandes : B08 (NIR), B04 (Red)\nVegetation active — [-1, +1]",
             "NDVI"),
            (self.btn_ndwi,
             "NDWI = (Green - NIR) / (Green + NIR)\n"
             "Bandes : B03 (Green), B08 (NIR)\nEaux de surface — [-1, +1]",
             "NDWI"),
            (self.btn_nmsi,
             "NMSI = (SWIR - NIR) / (SWIR + NIR)\n"
             "Bandes : B11 (SWIR), B08 (NIR)\nHumidite vegetation — [-1, +1]",
             "NMSI"),
            (self.btn_ndbi,
             "NDBI = (SWIR - NIR) / (SWIR + NIR)\n"
             "Bandes : B11 (SWIR), B08A (NIR)\nZones baties\n"
             "Necessite GeoTIFF 20m (>= 5 bandes)",
             "NDBI"),
        ]:
            btn.setEnabled(False)
            btn.setToolTip(tip)
            btn.setFixedSize(100, 32)
            btn.clicked.connect(lambda _, i=ix: self._run_index(i))
            r2.addWidget(btn)
        r2.addStretch()
        gi.addWidget(self.row_s2)

        # Sentinel-1
        self.row_s1 = QWidget()
        r1 = QHBoxLayout(self.row_s1)
        r1.setContentsMargins(0, 0, 0, 0)
        r1.setSpacing(10)
        self.btn_ratio = QPushButton("Ratio VV/VH")
        self.btn_rvi   = QPushButton("RVI")
        for btn, tip, ix in [
            (self.btn_ratio,
             "Ratio VV / VH\nRugosite de surface, detection eau libre\n"
             "Clip [0.1-50]",
             "RATIO"),
            (self.btn_rvi,
             "RVI = 4xVH / (VV + VH)\nRadar Vegetation Index\n"
             "0=surface nue  -> 1=vegetation dense\n[0, 1]",
             "RVI"),
        ]:
            btn.setEnabled(False)
            btn.setToolTip(tip)
            btn.setFixedSize(130, 32)
            btn.clicked.connect(lambda _, i=ix: self._run_index(i))
            r1.addWidget(btn)
        r1.addStretch()
        gi.addWidget(self.row_s1)
        self.row_s1.setVisible(False)
        v.addWidget(grp_idx)

        # Resultats 
        grp_res = QGroupBox("Resultats")
        gr = QVBoxLayout(grp_res)
        gr.setSpacing(8)
        gr.setContentsMargins(10, 10, 10, 10)
        self.lbl_result = _lbl("En attente de calcul.")
        gr.addWidget(self.lbl_result)
        v.addWidget(grp_res)

        v.addStretch()
        scroll.setWidget(w)

        # Forcer l'etat initial coherent avec cmb_sat_r (index 0 = S2)
        self._update_ext_group_ui(is_s2=True)

        v.addStretch()
        scroll.setWidget(w)
        
        return scroll


    # CARTE

    def _build_canvas(self):
        try:
            from qgis.gui import QgsMapCanvas
            canvas = QgsMapCanvas()
            canvas.setMinimumHeight(300)
            canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            canvas.enableAntiAliasing(True)
            crs = QgsCoordinateReferenceSystem("EPSG:3857")
            canvas.setDestinationCrs(crs)

            osm_uri = (
                "type=xyz"
                "&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
                "&zmax=19&zmin=0&crs=EPSG:3857")
            self._layer_osm = QgsRasterLayer(osm_uri, "OpenStreetMap", "wms")
            if self._layer_osm.isValid():
                QgsProject.instance().addMapLayer(self._layer_osm, False)

            rect_4326 = QgsRectangle(-17.0, 20.0, 2.0, 37.0)
            tr = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"), crs,
                QgsProject.instance())
            canvas.setExtent(tr.transformBoundingBox(rect_4326))
            layers = ([self._layer_osm]
                      if (self._layer_osm and self._layer_osm.isValid())
                      else [])
            canvas.setLayers(layers)
            canvas.refresh()

            self._draw_tool = AoiDrawTool(canvas, self._on_aoi_drawn)
            return canvas

        except Exception as e:
            QgsMessageLog.logMessage(
                f"QgsMapCanvas : {e}", "SentinelVision", Qgis.Warning)
            return None

    def _update_canvas_layers(self):
        if self._canvas is None:
            return
        try:
            layers = []
            if self._layer_fp  and self._layer_fp.isValid():
                layers.append(self._layer_fp)
            if self._layer_aoi and self._layer_aoi.isValid():
                layers.append(self._layer_aoi)
            if self._layer_osm and self._layer_osm.isValid():
                layers.append(self._layer_osm)
            self._canvas.setLayers(layers)
            self._canvas.refresh()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Erreur canvas : {e}", "SentinelVision", Qgis.Warning)

    def _update_map_aoi_only(self):
        if self._canvas is None:
            return
        try:
            crs = self._canvas.mapSettings().destinationCrs()
            tr  = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"), crs,
                QgsProject.instance())
            if self._layer_aoi:
                QgsProject.instance().removeMapLayer(self._layer_aoi.id())
                self._layer_aoi = None
            if self._aoi:
                self._layer_aoi = _make_aoi_layer(self._aoi["bbox"])
                QgsProject.instance().addMapLayer(self._layer_aoi, False)
            self._update_canvas_layers()
            if self._aoi:
                lo0, la0, lo1, la1 = self._aoi["bbox"]
                rect = tr.transformBoundingBox(
                    QgsRectangle(lo0, la0, lo1, la1))
                rect = rect.buffered(
                    max(rect.width(), rect.height()) * 0.15)
                self._canvas.setExtent(rect)
                self._canvas.refresh()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Erreur update AOI : {e}", "SentinelVision", Qgis.Warning)

    def _toggle_footprint(self):
        checked = self.btn_fp_toggle.isChecked()
        if checked:
            if self._sel_result is None:
                self.btn_fp_toggle.setChecked(False)
                self.btn_fp_toggle.setText("Footprint : OFF")
                _set_status(self.lbl_dl,
                            "Selectionnez d'abord un produit dans le tableau.",
                            "warn")
                return
            self._show_selected_footprint()
            self._zoom_to_footprint()
            self.btn_fp_toggle.setText("Footprint : ON")
        else:
            if self._layer_fp:
                try:
                    QgsProject.instance().removeMapLayer(self._layer_fp.id())
                except Exception:
                    pass
                self._layer_fp = None
            self._update_canvas_layers()
            self.btn_fp_toggle.setText("Footprint : OFF")
            if self._aoi:
                self._update_map_aoi_only()

    def _show_selected_footprint(self):
        if self._sel_result is None or self._canvas is None:
            return
        try:
            if self._layer_fp:
                QgsProject.instance().removeMapLayer(self._layer_fp.id())
                self._layer_fp = None
            layer = _make_single_footprint_layer(self._sel_result)
            if layer:
                self._layer_fp = layer
                QgsProject.instance().addMapLayer(self._layer_fp, False)
            self._update_canvas_layers()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Footprint : {e}", "SentinelVision", Qgis.Warning)

    def _zoom_to_footprint(self):
        if self._sel_result is None or self._canvas is None:
            return
        try:
            geo  = self._sel_result.get("footprint")
            qgeo = _geo_to_qgs(geo)
            if qgeo is None or qgeo.isEmpty():
                return
            crs  = self._canvas.mapSettings().destinationCrs()
            tr   = QgsCoordinateTransform(
                QgsCoordinateReferenceSystem("EPSG:4326"), crs,
                QgsProject.instance())
            bbox = qgeo.boundingBox()
            proj = tr.transformBoundingBox(bbox)
            proj = proj.buffered(max(proj.width(), proj.height()) * 0.1)
            self._canvas.setExtent(proj)
            self._canvas.refresh()
        except Exception as e:
            QgsMessageLog.logMessage(
                f"Zoom footprint : {e}", "SentinelVision", Qgis.Warning)


    # OUTIL DESSIN AOI

    def _start_draw_aoi(self):
        if self._draw_tool is None:
            self._err("Carte non disponible",
                      "La carte n'a pas pu etre initialisee.")
            return
        if hasattr(self, "btn_draw_map"):
            self.btn_draw_map.setText("Dessin en cours...")
        self.btn_draw_aoi.setText("Dessin en cours...")
        self._draw_tool.activate()

    def _on_aoi_drawn(self, lo0, la0, lo1, la1):
        self.btn_draw_aoi.setText("Dessiner AOI")
        if hasattr(self, "btn_draw_map"):
            self.btn_draw_map.setText("Dessiner AOI")
        self.sp_lo0.setValue(round(lo0, 4))
        self.sp_la0.setValue(round(la0, 4))
        self.sp_lo1.setValue(round(lo1, 4))
        self.sp_la1.setValue(round(la1, 4))
        try:
            self._aoi = bbox_from_manual(lo0, la0, lo1, la1)
            _set_status(self.lbl_aoi,
                        f"AOI definie : [{lo0:.4f},{la0:.4f},"
                        f"{lo1:.4f},{la1:.4f}]", "ok")
            self._update_map_aoi_only()
        except Exception as e:
            _set_status(self.lbl_aoi, str(e), "error")

    def _clear_aoi(self):
        """Supprime l'AOI courante et nettoie les couches associees."""
        self._aoi = None
        _set_status(self.lbl_aoi, "AOI supprimee.", "warn")
        if self._layer_aoi:
            try:
                QgsProject.instance().removeMapLayer(self._layer_aoi.id())
            except Exception:
                pass
            self._layer_aoi = None
        if self._layer_fp:
            try:
                QgsProject.instance().removeMapLayer(self._layer_fp.id())
            except Exception:
                pass
            self._layer_fp = None
        if hasattr(self, "btn_fp_toggle"):
            self.btn_fp_toggle.setChecked(False)
            self.btn_fp_toggle.setText("Footprint : OFF")
        self._update_canvas_layers()

  
    # NAVIGATION

    def _switch_page(self, idx):
        self.stack.setCurrentIndex(idx)
        titles = ["Telechargement", "Calcul d'indices"]
        self.lbl_page_title.setText(titles[idx])
        for i, btn in enumerate(self._nav_btns):
            btn.setChecked(i == idx)

    def _open_hist(self):
        if self._hist_dlg is None or not self._hist_dlg.isVisible():
            self._hist_dlg = HistoriqueDialog(self.plugin_dir, parent=self)
        self._hist_dlg.show()
        self._hist_dlg.raise_()
        self._hist_dlg._refresh()

   
    # ACTIONS — AOI

    def _geocode(self):
        place = self.inp_place.text().strip()
        if not place:
            return
        try:
            self._aoi = geocode_place(place)
            _set_status(self.lbl_aoi,
                        f"AOI : {self._aoi['label'][:90]}", "ok")
            lo0, la0, lo1, la1 = self._aoi["bbox"]
            self.sp_lo0.setValue(round(lo0, 4))
            self.sp_la0.setValue(round(la0, 4))
            self.sp_lo1.setValue(round(lo1, 4))
            self.sp_la1.setValue(round(la1, 4))
            self._update_map_aoi_only()
        except Exception as e:
            _set_status(self.lbl_aoi, str(e), "error")

    def _set_manual_bbox(self):
        try:
            self._aoi = bbox_from_manual(
                self.sp_lo0.value(), self.sp_la0.value(),
                self.sp_lo1.value(), self.sp_la1.value())
            _set_status(self.lbl_aoi,
                        f"AOI : {self._aoi['label']}", "ok")
            self._update_map_aoi_only()
        except Exception as e:
            self._err("Bbox invalide", str(e))

    # ACTIONS — CONNEXION


    def _connect_cdse(self):
        email = self.inp_email.text().strip()
        pwd   = self.inp_pwd.text().strip()
        if not email or not pwd:
            self._err("Champs manquants", "Email et mot de passe requis.")
            return
        _set_status(self.lbl_conn, "Connexion en cours...", "info")
        self.repaint()
        try:
            self._token = cdse_get_token(email, pwd)
            _set_status(self.lbl_conn, "Connecte a Copernicus.", "ok")
            self.lbl_conn_indicator.setText("🟢 Connecté")
            self.lbl_conn_indicator.setToolTip("Connecté au service Copernicus Data Space Ecosystem")
            self.lbl_conn_indicator.setStyleSheet("""
                QPushButton {
                    background-color: #27ae60;
                    color: white;
                    border-radius: 12px;
                    font-weight: bold;
                    font-size: 11px;
                    border: none;
                    padding: 0 10px;
                }
            """)               
        except Exception as e:
            self._token = None
            _set_status(self.lbl_conn, str(e), "error")
            self.lbl_conn_indicator.setText("🔴 Non connecté")
            self.lbl_conn_indicator.setToolTip("Aucune connexion active ou authentification invalide")
            self.lbl_conn_indicator.setStyleSheet("""
                QPushButton {
                    background-color: #c0392b;
                    color: white;
                    border-radius: 12px;
                    font-weight: bold;
                    font-size: 11px;
                    border: none;
                    padding: 0 10px;
                }
            """)              


    # ACTIONS — CATALOGUE


    def _on_sat_changed(self, idx):
        is_s2 = (idx == 0)
        min_d = DATE_S2_MIN if is_s2 else DATE_S1_MIN
        self.dt_start.setMinimumDate(min_d)
        self.dt_end.setMinimumDate(min_d)
        if self.dt_start.date() < min_d:
            self.dt_start.setDate(min_d)
        if self.dt_end.date() < min_d:
            self.dt_end.setDate(min_d)
        self.sld_cloud.setEnabled(is_s2)
        self.lbl_cloud_val.setEnabled(is_s2)

    def _search_catalogue(self):
        if not self._check():
            return
        sat     = "S2" if "Sentinel-2" in self.cmb_sat.currentText() else "S1"
        d_start = self.dt_start.date().toString("yyyy-MM-dd")
        d_end   = self.dt_end.date().toString("yyyy-MM-dd")
        cloud   = self.sld_cloud.value()

        if self._layer_fp:
            try:
                QgsProject.instance().removeMapLayer(self._layer_fp.id())
            except Exception:
                pass
            self._layer_fp = None
        self._update_canvas_layers()
        self.repaint()

        try:
            self._results = cdse_search(
                self._token, self._aoi["bbox"],
                d_start, d_end, sat, cloud)

            self._fill_catalogue_table()
            n = len(self._results)
            self.btn_add_all.setEnabled(n > 0)

            self._coverage = None
            if n > 0:
                try:
                    self._coverage = analyse_aoi_coverage(
                        self._aoi["bbox"], self._results)
                except Exception as e_cov:
                    QgsMessageLog.logMessage(
                        f"Analyse couverture : {e_cov}",
                        "SentinelVision", Qgis.Warning)

        except Exception as e:
            QgsMessageLog.logMessage(
                f"Recherche catalogue : {e}", "SentinelVision", Qgis.Critical)
            self._err("Erreur recherche", str(e))

    def _on_cat_selection(self):
        sel = self.tbl_cat.currentRow()
        if not (0 <= sel < len(self._results)):
            return
        self._sel_result = self._results[sel]
        if hasattr(self, "btn_fp_toggle") and self.btn_fp_toggle.isChecked():
            self._show_selected_footprint()
            self._zoom_to_footprint()

  
    # ACTIONS — TELECHARGEMENT

    def _browse_outdir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Dossier de sortie", self._last_outdir or "")
        if d:
            self.inp_outdir.setText(d)

    def _start_dl(self):
        if not self._check():
            return
        if self._dl_worker and self._dl_worker.isRunning():
            self._err("En cours", "Un telechargement est deja en cours.")
            return
        if not self._results:
            self._err("Aucun produit", "Lancez d'abord une recherche.")
            return
        outdir = self.inp_outdir.text().strip()
        if not outdir:
            self._err("Dossier manquant", "Choisissez un dossier de sortie.")
            return

        sel  = self.tbl_cat.currentRow()
        idx  = sel if 0 <= sel < len(self._results) else 0
        prod = self._results[idx]

        if not prod.get("online", True):
            if QMessageBox.question(
                    self, "Produit hors ligne",
                    "Ce produit est 'offline' sur CDSE.\nContinuer ?",
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return

        self.pb.setValue(0)
        self.pb.setVisible(True)
        self.lbl_pb.setVisible(True)
        self.lbl_pb.setText("Initialisation...")
        self.btn_dl.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        _set_status(self.lbl_dl,
                    f"Telechargement : {prod['title'][:55]}...", "info")

        self._dl_worker = _DlWorker(
            self._token, prod["product_id"], prod["title"], outdir)
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.done.connect(self._on_dl_done)
        self._dl_worker.error.connect(self._on_dl_error)
        self._dl_worker.start()

    def _cancel_dl(self):
        if self._dl_worker and self._dl_worker.isRunning():
            self._dl_worker.cancel()
            _set_status(self.lbl_dl, "Annulation en cours...", "warn")
            self.btn_cancel.setEnabled(False)

    def _on_dl_progress(self, done, total, elapsed):
        pct      = int(done * 100 / total) if total else 0
        done_mb  = done  / 1_048_576
        total_mb = total / 1_048_576
        speed_kb = (done / elapsed / 1024) if elapsed > 0.1 else 0
        eta = ""
        if speed_kb > 0 and done < total:
            remaining = (total - done) / (done / elapsed)
            mins, secs = divmod(int(remaining), 60)
            eta = f"  Reste : {mins:02d}:{secs:02d}"
        self.pb.setValue(pct)
        self.lbl_pb.setText(
            f"{done_mb:.1f} / {total_mb:.1f} MB  "
            f"({speed_kb:.0f} KB/s){eta}")

    def _on_dl_done(self, path):
        """
        Telechargement termine — sauvegarde ZIP uniquement.
        Aucune extraction automatique.
        """
        path = str(path)
        self.pb.setValue(100)
        self.pb.setVisible(False)
        self.lbl_pb.setVisible(False)
        self.btn_dl.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._last_outdir = os.path.dirname(path)
        self._last_zip    = path

        sel  = self.tbl_cat.currentRow()
        idx  = sel if 0 <= sel < len(self._results) else 0
        prod = self._results[idx] if self._results else {}
        hist_add(self.plugin_dir, "download",
                 satellite   = self.cmb_sat.currentText(),
                 title       = prod.get("title", "?"),
                 product_id  = prod.get("product_id", "?"),
                 output_file = path)

        size_mb = os.path.getsize(path) / 1_048_576
        _set_status(
            self.lbl_dl,
            f"[OK] Telechargement termine avec succes.\n\n"
            f"Produit telecharge :\n"
            f"  {os.path.basename(path)}\n"
            f"  Taille : {size_mb:.1f} MB\n"
            f"  Dossier : {os.path.dirname(path)}\n\n"
            f"Etape suivante :\n"
            f"  1. Decompressez le fichier ZIP avec votre logiciel d'archivage.\n"
            f"  2. Allez dans 'Calcul d'indices' > 'Extraction GeoTIFF'.\n"
            f"  3. Selectionnez le dossier decompresse (.SAFE ou produit S1).",
            "ok")

    def _on_dl_error(self, msg):
        self.pb.setVisible(False)
        self.lbl_pb.setVisible(False)
        self.btn_dl.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if msg == "__CANCELLED__":
            _set_status(self.lbl_dl, "Telechargement annule.", "warn")
        else:
            _set_status(self.lbl_dl, msg, "error")

    # ACTIONS — EXTRACTION SAFE (page Calcul d'indices)

    _safe_dir_selected = ""

    def _select_safe_dir(self):
        """Ouvre un dialogue de selection de dossier SAFE ou produit S1."""
        start = self._last_outdir or ""
        sat   = self.cmb_sat_r.currentIndex()
        title = (
            "Selectionner le dossier .SAFE (Sentinel-2)"
            if sat == 0 else
            "Selectionner le dossier du produit Sentinel-1 decompresse"
        )
        d = QFileDialog.getExistingDirectory(self, title, start)
        if not d:
            return

        self._safe_dir_selected = d
        base = os.path.basename(d)

        sat_code = "S2" if sat == 0 else "S1"
        warnings = []
        if sat_code == "S2":
            if not d.upper().endswith(".SAFE"):
                warnings.append(
                    "Le dossier ne se termine pas par .SAFE.\n"
                    "Verifiez que vous avez bien selectionne le bon dossier.")
            if not os.path.isdir(os.path.join(d, "GRANULE")):
                warnings.append(
                    "Sous-dossier GRANULE absent — structure SAFE non standard.")
        else:
            if not any(os.path.isdir(os.path.join(d, sub))
                       for sub in ("measurement", "annotation", "support")):
                warnings.append(
                    "Structure Sentinel-1 non detectee.\n"
                    "Verifiez que vous avez selectionne le bon dossier.")

        txt = f"Dossier selectionne :\n{d}"
        if warnings:
            txt += "\n\n[!] Avertissement :\n" + "\n".join(warnings)

        _set_status(self.lbl_safe_selected, txt,
                    "warn" if warnings else "ok")
        self.btn_extract_safe.setEnabled(True)

    def _start_extract_safe(self):
        """Lance l'extraction GeoTIFF depuis le dossier SAFE / produit S1."""
        d = self._safe_dir_selected
        if not d or not os.path.isdir(d):
            self._err("Dossier invalide",
                      "Selectionnez d'abord un dossier valide.")
            return

        outdir = self._last_outdir or os.path.dirname(d)
        sat    = "S2" if self.cmb_sat_r.currentIndex() == 0 else "S1"

        self.btn_extract_safe.setEnabled(False)
        self.btn_extract_safe.setText("Extraction...")
        self.pb_extract.setVisible(True)
        _set_status(self.lbl_extract_status,
                    "Extraction GeoTIFF en cours...", "info")
        self.repaint()

        self._ex_worker = _ExtractWorker(d, outdir, satellite=sat)
        self._ex_worker.progress.connect(
            lambda msg: _set_status(self.lbl_extract_status, msg, "info"))
        self._ex_worker.done.connect(self._on_extract_safe_done)
        self._ex_worker.error.connect(self._on_extract_safe_error)
        self._ex_worker.start()

    def _on_extract_safe_done(self, result):
        self.btn_extract_safe.setEnabled(True)
        self.btn_extract_safe.setText("Creer GeoTIFF")
        self.pb_extract.setVisible(False)

        if not result:
            _set_status(self.lbl_extract_status,
                        "Extraction terminee (aucun resultat).", "warn")
            return

        lines = ["[OK] GeoTIFF crees avec succes."]
        bands = result.get("bands_found", [])
        if bands:
            lines.append(f"Bandes detectees : {', '.join(bands)}")

        loaded = []
        tiff_keys = [
            ("tiff_10m",  "GeoTIFF 10m  (B02 B03 B04 B08)"),
            ("tiff_20m",  "GeoTIFF 20m  (B03 B04 B08 B8A B11 B12)"),
            ("tiff_full", "GeoTIFF complet 7 bandes"),
            ("tiff_s1",   "GeoTIFF Sentinel-1 (VV + VH) — EPSG:4326"),
        ]
        for key, label in tiff_keys:
            p = result.get(key)
            if p and os.path.isfile(p):
                size_mb = os.path.getsize(p) / 1_048_576
                lines.append(f"  {label}")
                lines.append(f"  -> {p}  ({size_mb:.1f} MB)")
                loaded.append(
                    (p, os.path.splitext(os.path.basename(p))[0]))
                self._last_outdir = os.path.dirname(p)

        if not loaded:
            lines.append(
                "[!] Aucun GeoTIFF genere.\n"
                "Verifiez la structure du dossier et les bandes disponibles.")

        lines.append(
            "\nEtape suivante :\n"
            "  Chargez le GeoTIFF dans 'Charger une image'\n"
            "  puis calculez l'indice souhaite.")

        _set_status(self.lbl_extract_status, "\n".join(lines), "ok")

        for p, name in loaded:
            try:
                load_in_qgis(self.iface, p, name)
            except Exception as e:
                QgsMessageLog.logMessage(
                    str(e), "SentinelVision", Qgis.Warning)

        hist_add(
            self.plugin_dir, "extract",
            product_dir = self._safe_dir_selected,
            output_file = loaded[0][0] if loaded else "?",
            bands_found = result.get("bands_found", []))

    def _on_extract_safe_error(self, msg):
        self.btn_extract_safe.setEnabled(True)
        self.btn_extract_safe.setText("Creer GeoTIFF")
        self.pb_extract.setVisible(False)
        _set_status(
            self.lbl_extract_status,
            f"[ERR] Erreur lors de l'extraction :\n\n{msg}\n\n"
            f"Points a verifier :\n"
            f"  1. Avez-vous selectionne le bon dossier ?\n"
            f"     (dossier .SAFE pour S2, dossier produit pour S1)\n"
            f"  2. Le dossier est-il correctement decompresse ?\n"
            f"  3. Avez-vous les droits d'ecriture dans le dossier de sortie ?\n"
            f"  4. L'espace disque est-il suffisant ?",
            "error")
        _log(f"[EXTRACT ERROR] {msg}")


    # ACTIONS — CALCUL D'INDICES

    def _on_sat_raster_changed(self, idx):
        is_s2 = (idx == 0)

        # --- Indices : comportement inchange ---
        self.row_s2.setVisible(is_s2)
        self.row_s1.setVisible(not is_s2)

        # --- Remise a zero complete a chaque changement de satellite ---
        self._safe_dir_selected = ""
        self.btn_extract_safe.setEnabled(False)
        _set_status(self.lbl_safe_selected, "Aucun dossier selectionne.", "info")
        _set_status(self.lbl_extract_status, "", "info")   # efface le resultat precedent

        # --- Mise a jour dynamique des textes et du message image ---
        # (remet aussi lbl_loaded a "Aucune image selectionnee.")
        self._update_ext_group_ui(is_s2)

 
    def _update_ext_group_ui(self, is_s2):
        """
        Met a jour les titres et textes descriptifs des blocs
        'Preparation des donnees' et 'Choisir l'image' selon le satellite.
        Appele par _on_sat_raster_changed(). UI uniquement, aucune logique metier.
        """
        if is_s2:
            self.grp_ext.setTitle("Preparation des donnees Sentinel-2")
            self.lbl_ext_hint.setText(
                "Selectionnez le dossier .SAFE decompresse.\n\n"
                "Cette operation creera :\n"
                "  * Une image multispectrale 10 m (NDVI, NDWI)\n"
                "  * Une image multispectrale 20 m (NDVI, NDWI, NDBI, NMSI)")
            self.lbl_load_hint.setText(
                "Chargez l'une des images multispectrales creees precedemment :\n"
                "  * Image multispectrale 10 m\n"
                "  * Image multispectrale 20 m\n\n"
                "Choisissez le fichier adapte a l'indice que vous souhaitez calculer.")
        else:
            self.grp_ext.setTitle("Preparation des donnees Sentinel-1")
            self.lbl_ext_hint.setText(
                "Selectionnez le dossier du produit Sentinel-1 decompresse.\n\n"
                "Cette operation creera :\n"
                "  * Une image radar dual-polarisation (VV/VH)\n\n"
                "Cette image sera utilisee pour le calcul du RVI "
                "et du ratio VV/VH.")
            self.lbl_load_hint.setText(
                "Chargez l'image radar dual-polarisation (VV/VH) "
                "creee lors de la preparation des donnees.\n\n"
                "Cette image sera utilisee pour le calcul du RVI "
                "et du ratio VV/VH.")
        # Remise a zero du message de chargement a chaque changement de satellite
        self.lbl_loaded.setText("Aucune image selectionnee.")

    def _load_raster_auto(self):
        start = self._last_outdir or ""
        if self.cmb_sat_r.currentIndex() == 0:
            self._load_s2(start)
        else:
            self._load_s1(start)

    def _load_from_recent(self):
        d = self._last_outdir or ""
        if not d or not os.path.isdir(d):
            self._err("Aucun dossier recent",
                      "Effectuez d'abord un telechargement ou une extraction.")
            return
        if self.cmb_sat_r.currentIndex() == 0:
            self._load_s2(d)
        else:
            self._load_s1(d)

    def _load_s2(self, start_dir=""):
        path, _ = QFileDialog.getOpenFileName(
            self, "Ouvrir GeoTIFF Sentinel-2", start_dir,
            "GeoTIFF (*.tif *.tiff);;Tous (*)")
        if not path:
            return
        try:
            from osgeo import gdal
            ds = gdal.Open(path)
            if ds is None:
                raise RuntimeError("GDAL ne peut pas ouvrir ce fichier.")
            self._s2_ds   = ds
            self._s2_path = path
            n = ds.RasterCount
            # Determination du type d'image pour l'affichage uniquement
            if n == 4:
                detected_type = "Sentinel-2 (10 m)"
            elif n >= 5:
                detected_type = "Sentinel-2 (20 m)"
            else:
                detected_type = f"Sentinel-2 ({n} bandes)"
            _set_status(
                self.lbl_loaded,
                f"Image selectionnee : {os.path.basename(path)}\n"
                f"Type detecte : {detected_type}  |  "
                f"{ds.RasterXSize} x {ds.RasterYSize} px",
                "ok")
            for b in [self.btn_ndvi, self.btn_ndwi]:
                b.setEnabled(True)
            self.btn_nmsi.setEnabled(n >= 5)
            self.btn_ndbi.setEnabled(n >= 5)
        except Exception as e:
            _set_status(self.lbl_loaded, str(e), "error")

    def _load_s1(self, start_dir=""):
        path, _ = QFileDialog.getOpenFileName(
            self, "Ouvrir GeoTIFF Sentinel-1", start_dir,
            "GeoTIFF (*.tif *.tiff);;Tous (*)")
        if not path:
            return
        try:
            from osgeo import gdal
            ds = gdal.Open(path)
            if ds is None:
                raise RuntimeError("GDAL ne peut pas ouvrir ce fichier.")

            # Avertir si le fichier charge n'a pas de geotransform affine valide
            gt = ds.GetGeoTransform()
            if gt is None or gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
                QgsMessageLog.logMessage(
                    f"[WARN] Fichier S1 charge sans geotransform affine : {path}\n"
                    f"Utilisez 'Creer GeoTIFF' pour generer le fichier _S1_dual_pol.tif",
                    "SentinelVision", Qgis.Warning)

            self._s1_ds   = ds
            self._s1_path = path
            _set_status(
                self.lbl_loaded,
                f"Image selectionnee : {os.path.basename(path)}\n"
                f"Type detecte : Sentinel-1 VV/VH  |  "
                f"{ds.RasterXSize} x {ds.RasterYSize} px",
                "ok")
            self.btn_ratio.setEnabled(True)
            self.btn_rvi.setEnabled(True)
        except Exception as e:
            _set_status(self.lbl_loaded, str(e), "error")

    def _run_index(self, idx):
        if idx in ("NDVI", "NDWI", "NMSI", "NDBI"):
            ds, src = self._s2_ds, self._s2_path
            funcs   = {
                "NDVI": compute_ndvi,
                "NDWI": compute_ndwi,
                "NMSI": compute_nmsi,
                "NDBI": compute_ndbi,
            }
        else:
            ds, src = self._s1_ds, self._s1_path
            funcs   = {
                "RATIO": compute_ratio_vv_vh,
                "RVI":   compute_rvi_s1,
            }

        if ds is None:
            self._err("Aucun raster charge",
                      "Chargez d'abord un GeoTIFF dans la section ci-dessus.")
            return

        default_dir = os.path.dirname(src) if src else (self._last_outdir or "")
        base        = os.path.splitext(os.path.basename(src or "output"))[0]
        default     = os.path.join(default_dir, f"{base}_{idx}.tif")

        out, _ = QFileDialog.getSaveFileName(
            self, f"Enregistrer {idx}", default,
            "GeoTIFF (*.tif)")
        if not out:
            return

        try:
            _set_status(self.lbl_result,
                        f"Calcul {idx} en cours...", "info")
            self.repaint()
            stats = compute_index_by_blocks(ds, funcs[idx], out)
            load_in_qgis(
                self.iface, out,
                f"{idx} — {os.path.basename(src or '')}")
            _set_status(
                self.lbl_result,
                f"[OK] {idx} calcule.\n"
                f"Min={stats.get('min', 0):.4f}  "
                f"Max={stats.get('max', 0):.4f}  "
                f"Moy={stats.get('mean', 0):.4f}  "
                f"Ecart={stats.get('std', 0):.4f}\n"
                f"Fichier : {os.path.basename(out)}",
                "ok")
            hist_add(self.plugin_dir, "index",
                     index=idx,
                     source_file=src,
                     output_file=out,
                     stats=stats)
        except Exception as e:
            _set_status(self.lbl_result, f"Erreur {idx} : {e}", "error")
            QgsMessageLog.logMessage(
                str(e), "SentinelVision", Qgis.Critical)


    # CATALOGUE — remplissage avec bouton Panier


    def _fill_catalogue_table(self):
        cart_ids = {item["product_id"] for item in self._cart}
        self.tbl_cat.blockSignals(True)
        self.tbl_cat.setRowCount(len(self._results))
        for r, res in enumerate(self._results):
            cc = (f"{res['cloud_cover']:.1f} %"
                  if res["cloud_cover"] is not None else "—")
            offline_tag = " [OFF]" if not res.get("online", True) else ""
            vals = [
                res["date"],
                res["title"][:60] + offline_tag,
                cc,
                str(res["size_mb"]),
            ]
            for c, val in enumerate(vals):
                item = QTableWidgetItem(str(val))
                item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                if not res.get("online", True):
                    item.setForeground(QBrush(QColor("#e67e22")))
                self.tbl_cat.setItem(r, c, item)

            already_in = res["product_id"] in cart_ids
            btn_add = QPushButton("Dans panier" if already_in else "+ Panier")
            btn_add.setFixedHeight(22)
            btn_add.setEnabled(not already_in)
            if not already_in:
                btn_add.setStyleSheet(
                    "QPushButton{background:#27ae60;color:white;"
                    "border-radius:3px;font-size:11px;}"
                    "QPushButton:hover{background:#2ecc71;}")
                btn_add.clicked.connect(
                    lambda _, ri=r: self._cart_add(ri))
            self.tbl_cat.setCellWidget(r, 4, btn_add)
        self.tbl_cat.blockSignals(False)


    # PANIER 

    _CART_COLORS = {
        "En attente":     "#888888",
        "Telechargement": "#2980b9",
        "Verification":   "#e67e22",
        "Termine":        "#27ae60",
        "Erreur":         "#c0392b",
        "Annule":         "#95a5a6",
    }

    def _cart_add(self, result_idx):
        if not (0 <= result_idx < len(self._results)):
            return
        res = self._results[result_idx]
        if any(i["product_id"] == res["product_id"] for i in self._cart):
            return
        self._cart.append({
            "product_id": res["product_id"],
            "title":      res["title"],
            "date":       res["date"],
            "size_mb":    res["size_mb"],
            "satellite":  "S2" if "Sentinel-2" in self.cmb_sat.currentText() else "S1",
            "online":     res.get("online", True),
            "footprint":  res.get("footprint"),
            "status":     "En attente",
            "zip_path":   None,
            "error_msg":  "",
        })
        self._cart_refresh_table()
        self._cart_update_header()
        self._fill_catalogue_table()

    def _cart_add_all(self):
        cart_ids = {i["product_id"] for i in self._cart}
        added = 0
        for res in self._results:
            if res["product_id"] not in cart_ids:
                self._cart.append({
                    "product_id": res["product_id"],
                    "title":      res["title"],
                    "date":       res["date"],
                    "size_mb":    res["size_mb"],
                    "satellite":  "S2" if "Sentinel-2" in self.cmb_sat.currentText() else "S1",
                    "online":     res.get("online", True),
                    "footprint":  res.get("footprint"),
                    "status":     "En attente",
                    "zip_path":   None,
                    "error_msg":  "",
                })
                added += 1
        if added:
            self._cart_refresh_table()
            self._cart_update_header()
            self._fill_catalogue_table()
            _set_status(self.lbl_cart_status,
                        f"{added} produit(s) ajoute(s) au panier.", "ok")

    def _cart_remove(self, idx):
        if not (0 <= idx < len(self._cart)):
            return
        item = self._cart[idx]
        if item["status"] == "Telechargement":
            if QMessageBox.question(
                    self, "Retirer",
                    f"'{item['title'][:40]}' est en cours.\nRetirer quand meme ?",
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            if self._cart_worker and self._cart_worker.isRunning():
                self._cart_worker.cancel()
        self._cart.pop(idx)
        self._cart_refresh_table()
        self._cart_update_header()
        self._fill_catalogue_table()

    def _cart_clear(self):
        if self._cart_running:
            if QMessageBox.question(
                    self, "Vider le panier",
                    "Des telechargements sont en cours.\nAnnuler et vider ?",
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            if self._cart_worker and self._cart_worker.isRunning():
                self._cart_worker.cancel()
        self._cart.clear()
        self._cart_running = False
        self._cart_refresh_table()
        self._cart_update_header()
        if self._results:
            self._fill_catalogue_table()
        _set_status(self.lbl_cart_status, "Panier vide.", "info")

    def _cart_update_header(self):
        n = len(self._cart)
        total_mb = sum(i.get("size_mb", 0) for i in self._cart)
        self.lbl_cart_count.setText(f"{n} item(s) — {total_mb:.0f} MB total")
        can_dl = (n > 0 and not self._cart_running and
                  any(i["status"] == "En attente" for i in self._cart))
        self.btn_cart_start.setEnabled(can_dl)

    def _cart_refresh_table(self):
        self.tbl_cart.blockSignals(True)
        self.tbl_cart.setRowCount(len(self._cart))
        for r, item in enumerate(self._cart):
            vals = [
                item["title"][:52],
                item["date"],
                str(item["size_mb"]),
                item["status"],
            ]
            for c, val in enumerate(vals):
                wi = QTableWidgetItem(str(val))
                wi.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
                if c == 3:
                    color = self._CART_COLORS.get(item["status"], "#555")
                    wi.setForeground(QBrush(QColor(color)))
                    f = QFont(); f.setBold(True); wi.setFont(f)
                self.tbl_cart.setItem(r, c, wi)
            btn_rm = QPushButton("Retirer")
            btn_rm.setFixedHeight(20)
            btn_rm.setEnabled(item["status"] != "Telechargement")
            btn_rm.clicked.connect(lambda _, ri=r: self._cart_remove(ri))
            self.tbl_cart.setCellWidget(r, 4, btn_rm)
        self.tbl_cart.blockSignals(False)

  
    # PANIER — file d'attente sequentielle 


    def _cart_start_queue(self):
        outdir = self.inp_outdir.text().strip()
        if not outdir:
            self._err("Dossier manquant",
                      "Choisissez un dossier de sortie avant de telecharger.")
            return
        if not self._token:
            self._err("Non connecte", "Reconnectez-vous a Copernicus.")
            return
        if not any(i["status"] == "En attente" for i in self._cart):
            _set_status(self.lbl_cart_status,
                        "Aucun item en attente dans le panier.", "warn")
            return
        self._cart_running = True
        self._cart_update_header()
        _set_status(self.lbl_cart_status,
                    "File de telechargement demarree...", "info")
        self._cart_next(outdir)

    def _cart_next(self, outdir):
        for item in self._cart:
            if item["status"] == "En attente":
                self._cart_launch_dl(item, outdir)
                return
        # File terminee
        self._cart_running = False
        self._cart_update_header()
        done_n = sum(1 for i in self._cart if i["status"] == "Termine")
        err_n  = sum(1 for i in self._cart if i["status"] == "Erreur")
        self.pb_cart.setVisible(False)
        self.lbl_cart_prog.setVisible(False)
        _set_status(
            self.lbl_cart_status,
            f"[OK] Telechargements termines : {done_n} reussi(s), "
            f"{err_n} erreur(s).\n\n"
            f"Decompressez les fichiers ZIP puis utilisez\n"
            f"'Calcul d'indices > Extraction GeoTIFF' pour creer les GeoTIFF.",
            "ok" if err_n == 0 else "warn")

    def _cart_launch_dl(self, item, outdir):
        """Lance le telechargement d'un item du panier (ZIP uniquement)."""
        item["status"] = "Telechargement"
        self._cart_refresh_table()
        self.pb_cart.setVisible(True)
        self.lbl_cart_prog.setVisible(True)
        self.lbl_cart_prog.setText(f"Telechargement : {item['title'][:50]}")
        _set_status(self.lbl_cart_status,
                    f"Telechargement : {item['title'][:55]}...", "info")

        worker = _DlWorker(
            self._token, item["product_id"], item["title"], outdir)
        self._cart_worker = worker

        def _on_progress(done, total, elapsed):
            pct      = int(done * 100 / total) if total else 0
            done_mb  = done / 1_048_576
            total_mb = total / 1_048_576
            speed_kb = (done / elapsed / 1024) if elapsed > 0.1 else 0
            eta = ""
            if speed_kb > 0 and done < total:
                rem = (total - done) / (done / elapsed)
                m, s = divmod(int(rem), 60)
                eta = f"  Reste : {m:02d}:{s:02d}"
            self.pb_cart.setValue(pct)
            self.lbl_cart_prog.setText(
                f"{item['title'][:38]} — "
                f"{done_mb:.1f}/{total_mb:.1f} MB "
                f"({speed_kb:.0f} KB/s){eta}")

        def _on_done(path):
            item["zip_path"] = path
            item["status"]   = "Verification"
            self._cart_refresh_table()
            ok, err = _check_zip_integrity(path)
            if not ok:
                item["status"]    = "Erreur"
                item["error_msg"] = err
                self._cart_refresh_table()
                _set_status(
                    self.lbl_cart_status,
                    f"[ERR] ZIP invalide pour {item['title'][:40]} :\n"
                    f"{err[:200]}", "error")
                self._cart_next(outdir)
                return

            item["status"] = "Termine"
            self._cart_refresh_table()
            size_mb = os.path.getsize(path) / 1_048_576
            self._last_outdir = outdir
            self._last_zip    = path
            hist_add(self.plugin_dir, "download",
                     satellite   = item.get("satellite", "?"),
                     title       = item["title"],
                     product_id  = item["product_id"],
                     output_file = path)
            done_n = sum(1 for i in self._cart if i["status"] == "Termine")
            _set_status(
                self.lbl_cart_status,
                f"[OK] Telecharge : {os.path.basename(path)}"
                f" ({size_mb:.1f} MB)\n"
                f"({done_n}/{len(self._cart)} produit(s) termines)",
                "ok")
            self.pb_cart.setValue(100)
            self._cart_next(outdir)

        def _on_error(msg):
            if msg == "__CANCELLED__":
                item["status"] = "Annule"
            else:
                item["status"]    = "Erreur"
                item["error_msg"] = msg
            self._cart_refresh_table()
            self.pb_cart.setVisible(False)
            self.lbl_cart_prog.setVisible(False)
            if msg != "__CANCELLED__":
                _set_status(
                    self.lbl_cart_status,
                    f"[ERR] Telechargement {item['title'][:40]} :\n"
                    f"{msg[:200]}", "error")
            self._cart_next(outdir)

        worker.progress.connect(_on_progress)
        worker.done.connect(_on_done)
        worker.error.connect(_on_error)
        worker.start()


    # UTILITAIRES

    def _err(self, title, msg):
        QMessageBox.critical(self, title, msg)
        QgsMessageLog.logMessage(
            f"[ERR] {title} — {msg}",
            "SentinelVision", Qgis.Critical)

    def _check(self):
        if self._aoi is None:
            self._err("AOI manquante",
                      "Definissez d'abord une zone d'interet.")
            return False
        if self._token is None:
            self._err("Non connecte",
                      "Connectez-vous d'abord a Copernicus.")
            return False
        return True