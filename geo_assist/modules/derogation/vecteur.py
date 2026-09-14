"""
vecteur.py  v1.0
Plugin QGIS professionnel
"""


import os
import math
import uuid
from datetime import datetime

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QPushButton, QLabel, QLineEdit, QComboBox,
    QFileDialog, QGroupBox, QGridLayout, QMessageBox,
    QFrame, QTextEdit, QAction, QApplication,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QButtonGroup, QRadioButton, QStackedWidget,
    QScrollArea, QSizePolicy, QDoubleSpinBox,
    QListWidget, QListWidgetItem, QSplitter,
)
from qgis.PyQt.QtCore import Qt, QSize, pyqtSignal, QVariant
from qgis.PyQt.QtGui import QFont, QColor, QIcon, QCursor, QPainter, QPixmap

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRectangle,
    QgsGeometry, QgsPointXY, QgsFeature, QgsFields, QgsField,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsDistanceArea, QgsWkbTypes, QgsApplication,
    QgsMapLayerProxyModel, QgsMapSettings,
    QgsMapRendererCustomPainterJob,
    QgsLayerTree, QgsLayerTreeModel, QgsLegendRenderer,
    QgsLegendSettings,
    QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
    QgsSingleSymbolRenderer, QgsSymbol,
)
from qgis.gui import QgsMapToolEmitPoint, QgsMapCanvas

try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, KeepTogether, Image as RLImage
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm, mm
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

try:
    from docx import Document
    from docx.shared import Pt, RGBColor, Cm, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False


# =============================================================================
# CONSTANTES
# =============================================================================

DECISION_RULES = {
    "area_min_ha"          : 1.0,
    "forest_max_pct"       : 10.0,
    "degraded_min_count"   : 5,
    "search_radius_km"     : 1.0,
    "derogation_max_count" : 5,   
}


_USED_IDS = set()


def generate_unique_id():
    
    while True:
        uid = "PRJ-" + uuid.uuid4().hex[:6].upper()
        if uid not in _USED_IDS:
            _USED_IDS.add(uid)
            return uid


def utm_crs_from_point(x, y):
    zone = int((x + 180) / 6) + 1
    epsg = (32600 if y >= 0 else 32700) + zone
    return QgsCoordinateReferenceSystem(f"EPSG:{epsg}")


def project_crs():
    crs = QgsProject.instance().crs()
    if not crs.isValid():
        crs = QgsCoordinateReferenceSystem("EPSG:4326")
    return crs


def transform_point(x, y, from_crs, to_crs):
    if from_crs.authid() == to_crs.authid():
        return x, y
    pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
    pt.transform(QgsCoordinateTransform(from_crs, to_crs, QgsProject.instance()))
    p = pt.asPoint()
    return p.x(), p.y()


def create_buffer_geometry(x, y, radius_km, crs):
    
    pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
    ctx = QgsProject.instance().transformContext()

    if crs.isGeographic():
        utm = utm_crs_from_point(x, y)
        to_utm = QgsCoordinateTransform(crs, utm, ctx)
        to_crs = QgsCoordinateTransform(utm, crs, ctx)
        pt.transform(to_utm)
        buf = pt.buffer(radius_km * 1000.0, 64)
        if buf and not buf.isEmpty():
            buf.transform(to_crs)
    else:
        buf = pt.buffer(radius_km * 1000.0, 64)

    if not buf or buf.isEmpty():
        buf = pt.buffer(radius_km * 1000.0 if not crs.isGeographic() else radius_km / 111.0, 64)

    if buf and not buf.isGeosValid():
        buf = buf.makeValid()
    return buf


def create_geodesic_buffer(x, y, radius_km):
    return create_buffer_geometry(x, y, radius_km, project_crs())


def style_project_point(layer):
    sym = QgsMarkerSymbol.createSimple({
        "name": "circle",
        "color": "#444444",
        "outline_color": "#222222",
        "outline_width": "0.4",
        "size": "4",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.triggerRepaint()


def style_buffer_layer(layer):
    sym = QgsFillSymbol.createSimple({
        "color": "255,182,193,180",
        "outline_color": "255,0,0",
        "outline_width": "1.0",
        "style": "solid",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.triggerRepaint()


def add_layer_to_map(layer):
    
    if not layer or not layer.isValid():
        return
    QgsProject.instance().addMapLayer(layer, False)
    root = QgsProject.instance().layerTreeRoot()
    root.insertLayer(0, layer)
    node = root.findLayer(layer.id())
    if node:
        node.setItemVisibilityChecked(True)
    layer.reload()
    layer.triggerRepaint()


def style_intersection_layer(layer):
    sym = QgsFillSymbol.createSimple({
        "color": "155,89,182,200",
        "outline_color": "74,35,90",
        "outline_width": "0.5",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    layer.triggerRepaint()


def symbol_color_hex(layer):
    try:
        renderer = layer.renderer()
        if renderer:
            sym = renderer.symbol()
            if sym:
                return sym.color().name()
    except Exception:
        pass
    return "#888888"


# =============================================================================
# OUTIL CLIC CARTE
# =============================================================================

class PointMapTool(QgsMapToolEmitPoint):
    point_selected = pyqtSignal(float, float)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas

    def canvasReleaseEvent(self, event):
        pt = self.toMapCoordinates(event.pos())
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        crs = project_crs()
        x, y = transform_point(pt.x(), pt.y(), canvas_crs, crs)
        self.point_selected.emit(x, y)


# =============================================================================
# ANALYSE VECTORIELLE
# =============================================================================

class VectorAnalysis:

    def compute_area_ha(self, layer):
        try:
            calc = QgsDistanceArea()
            calc.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
            calc.setEllipsoid("WGS84")
            total_m2 = 0.0
            for f in layer.getFeatures():
                geom = f.geometry()
                if geom and not geom.isEmpty():
                    area = calc.measureArea(geom)
                    if area is not None and area == area:
                        total_m2 += area
            if total_m2 <= 0:
                return 0.0
            return round(total_m2 / 10000, 2)
        except Exception:
            return 0.0

    def compute_forest_coverage(self, study_layer, forest_layer):
        try:
            study_geom = next((f.geometry() for f in study_layer.getFeatures() if f.geometry()), None)
            if not study_geom:
                return 0.0
            study_area = study_geom.area()
            if study_area == 0:
                return 0.0
            buf_crs = study_layer.crs()
            tgt_crs = forest_layer.crs()
            ctx = QgsProject.instance().transformContext()
            need_transform = buf_crs.authid() != tgt_crs.authid()
            transform = QgsCoordinateTransform(tgt_crs, buf_crs, ctx) if need_transform else None
            forest_area = 0.0
            for f in forest_layer.getFeatures():
                geom = f.geometry()
                if not geom or geom.isEmpty():
                    continue
                if transform:
                    geom = QgsGeometry(geom)
                    try:
                        geom.transform(transform)
                    except Exception:
                        continue
                if geom.intersects(study_geom):
                    forest_area += geom.intersection(study_geom).area()
            return min(round((forest_area / study_area) * 100, 1), 100.0)
        except Exception:
            return 0.0

    def count_degraded_zones(self, study_layer, degraded_layer, radius_km=1.0):
        try:
            study_geom = next((f.geometry() for f in study_layer.getFeatures() if f.geometry()), None)
            if not study_geom:
                return 0
            search_geom = study_geom
            if study_geom.type() == QgsWkbTypes.PointGeometry:
                search_geom = study_geom.buffer(radius_km / 111.0, 32)
            return sum(
                1 for f in degraded_layer.getFeatures()
                if f.geometry() and f.geometry().intersects(search_geom)
            )
        except Exception:
            return 0

    def check_domain_conflicts(self, study_layer, domain_layers):
        results = {}
        try:
            study_geom = next((f.geometry() for f in study_layer.getFeatures() if f.geometry()), None)
            if not study_geom:
                return {k: None for k in domain_layers}
            study_crs = study_layer.crs()
            ctx = QgsProject.instance().transformContext()
            for nom, layer in domain_layers.items():
                if layer is None:
                    results[nom] = None
                    continue
                tgt_crs = layer.crs()
                need_transform = study_crs.authid() != tgt_crs.authid()
                transform = QgsCoordinateTransform(tgt_crs, study_crs, ctx) if need_transform else None
                found = False
                for f in layer.getFeatures():
                    geom = f.geometry()
                    if not geom or geom.isEmpty():
                        continue
                    if transform:
                        geom = QgsGeometry(geom)
                        try:
                            geom.transform(transform)
                        except Exception:
                            continue
                    if geom.intersects(study_geom):
                        found = True
                        break
                results[nom] = found
        except Exception:
            results = {k: None for k in domain_layers}
        return results

    def compute_degraded_on_site(self, study_layer, degraded_layer):
        try:
            study_geom = next((f.geometry() for f in study_layer.getFeatures() if f.geometry()), None)
            if not study_geom:
                return 0.0
            study_area = study_geom.area()
            if study_area <= 0:
                return 0.0
            study_crs = study_layer.crs()
            tgt_crs = degraded_layer.crs()
            ctx = QgsProject.instance().transformContext()
            need_transform = study_crs.authid() != tgt_crs.authid()
            transform = QgsCoordinateTransform(tgt_crs, study_crs, ctx) if need_transform else None
            degrad_area = 0.0
            for f in degraded_layer.getFeatures():
                geom = f.geometry()
                if not geom or geom.isEmpty():
                    continue
                if transform:
                    geom = QgsGeometry(geom)
                    try:
                        geom.transform(transform)
                    except Exception:
                        continue
                if geom.intersects(study_geom):
                    inter = geom.intersection(study_geom)
                    if inter and not inter.isEmpty():
                        degrad_area += inter.area()
            return round((degrad_area / study_area) * 100, 1)
        except Exception:
            return 0.0

    def _buffer_geometry(self, buffer_layer):
        return next(
            (f.geometry() for f in buffer_layer.getFeatures() if f.geometry() and not f.geometry().isEmpty()),
            None,
        )

    def compute_derogation_in_buffer(self, buffer_layer, derogation_layer):
        
        if derogation_layer is None or buffer_layer is None:
            return 0, 0.0
        try:
            buf_geom = self._buffer_geometry(buffer_layer)
            if not buf_geom or buf_geom.isEmpty():
                return 0, 0.0
            buf_crs = buffer_layer.crs()
            tgt_crs = derogation_layer.crs()
            ctx = QgsProject.instance().transformContext()
            need_transform = buf_crs.authid() != tgt_crs.authid()
            transform = QgsCoordinateTransform(tgt_crs, buf_crs, ctx) if need_transform else None
            calc = QgsDistanceArea()
            calc.setSourceCrs(buf_crs, ctx)
            calc.setEllipsoid("WGS84")
            count = 0
            surface_m2 = 0.0
            for f in derogation_layer.getFeatures():
                geom = f.geometry()
                if not geom or geom.isEmpty():
                    continue
                if transform:
                    geom = QgsGeometry(geom)
                    try:
                        geom.transform(transform)
                    except Exception:
                        continue
                if geom.intersects(buf_geom):
                    count += 1
                    inter = geom.intersection(buf_geom)
                    if inter and not inter.isEmpty():
                        area = calc.measureArea(inter)
                        if area and area == area:
                            surface_m2 += area
            surface_ha = round(surface_m2 / 10000, 2)
            return count, surface_ha
        except Exception:
            return 0, 0.0

    def compute_buffer_area_m2(self, buffer_layer):
        try:
            buf_geom = self._buffer_geometry(buffer_layer)
            if not buf_geom:
                return 0.0
            calc = QgsDistanceArea()
            calc.setSourceCrs(buffer_layer.crs(), QgsProject.instance().transformContext())
            calc.setEllipsoid("WGS84")
            area = calc.measureArea(buf_geom)
            return area if area and area == area else 0.0
        except Exception:
            return 0.0

    def compute_layer_intersection_pct(self, buffer_layer, target_layer):
        
        if target_layer is None or buffer_layer is None:
            return None
        try:
            buf_geom = self._buffer_geometry(buffer_layer)
            if not buf_geom or buf_geom.isEmpty():
                return None

            buf_crs = buffer_layer.crs()
            tgt_crs = target_layer.crs()
            ctx = QgsProject.instance().transformContext()

           
            need_transform = buf_crs.authid() != tgt_crs.authid()
            transform = QgsCoordinateTransform(tgt_crs, buf_crs, ctx) if need_transform else None

            calc = QgsDistanceArea()
            calc.setSourceCrs(buf_crs, ctx)
            calc.setEllipsoid("WGS84")
            buffer_area = calc.measureArea(buf_geom)
            if not buffer_area or buffer_area <= 0:
                return 0.0

            intersect_m2 = 0.0
            for f in target_layer.getFeatures():
                geom = f.geometry()
                if not geom or geom.isEmpty():
                    continue
                
                if transform:
                    geom = QgsGeometry(geom)
                    try:
                        geom.transform(transform)
                    except Exception:
                        continue
                if not geom.intersects(buf_geom):
                    continue
                inter = geom.intersection(buf_geom)
                if inter and not inter.isEmpty():
                    area = calc.measureArea(inter)
                    if area and area == area:
                        intersect_m2 += area

            return min(round((intersect_m2 / buffer_area) * 100, 1), 100.0)
        except Exception:
            return None

    def create_intersection_layer(self, buffer_layer, layers_dict, name="Intersected_Layers"):
        
        buf_geom = self._buffer_geometry(buffer_layer)
        if not buf_geom:
            return None
        buf_crs = buffer_layer.crs()
        ctx = QgsProject.instance().transformContext()
        crs = buf_crs.authid()
        mem = QgsVectorLayer(f"Polygon?crs={crs}", name, "memory")
        pr = mem.dataProvider()
        pr.addAttributes([QgsField("domaine", QVariant.String)])
        mem.updateFields()
        for label, layer in layers_dict.items():
            if not layer:
                continue
            tgt_crs = layer.crs()
            need_transform = buf_crs.authid() != tgt_crs.authid()
            transform = QgsCoordinateTransform(tgt_crs, buf_crs, ctx) if need_transform else None
            for f in layer.getFeatures():
                geom = f.geometry()
                if not geom or geom.isEmpty():
                    continue
                if transform:
                    geom = QgsGeometry(geom)
                    try:
                        geom.transform(transform)
                    except Exception:
                        continue
                if not geom.intersects(buf_geom):
                    continue
                inter = geom.intersection(buf_geom)
                if not inter or inter.isEmpty():
                    continue
                parts = inter.asGeometryCollection() if inter.isMultipart() else [inter]
                for part in parts:
                    if not part or part.isEmpty():
                        continue
                    feat = QgsFeature(mem.fields())
                    feat.setGeometry(part)
                    feat.setAttribute("domaine", label)
                    pr.addFeature(feat)
        mem.updateExtents()
        style_intersection_layer(mem)
        return mem if mem.featureCount() > 0 else None


# =============================================================================
# MOTEUR DÉCISIONNEL
# =============================================================================

class DecisionEngine:

    def __init__(self, rules=None):
        self.rules = rules or DECISION_RULES

    def evaluate(self, data):
        criteria = []
        warnings = []
        score_pts = 0
        total_pts = 0

        area    = data.get("area_ha", 0)
        ok_area = area >= self.rules["area_min_ha"]
        criteria.append({
            "nom"    : "Superficie minimale",
            "valeur" : f"{area} ha",
            "requis" : f"≥ {self.rules['area_min_ha']} ha",
            "ok"     : ok_area,
            "message": (
                f"Superficie ({area} ha) conforme."
                if ok_area else
                f"Superficie insuffisante ({area} ha < {self.rules['area_min_ha']} ha)."
            ),
        })
        score_pts += (1 if ok_area else 0)
        total_pts += 1

        forest    = data.get("forest_pct", 0)
        ok_forest = forest <= self.rules["forest_max_pct"]
        absent_f  = data.get("forest_absent", False)
        criteria.append({
            "nom"    : "Couverture forestière",
            "valeur" : "Non évaluée" if absent_f else f"{forest} %",
            "requis" : f"≤ {self.rules['forest_max_pct']} %",
            "ok"     : None if absent_f else ok_forest,
            "message": (
                "Couche forêt absente — critère ignoré."
                if absent_f else
                f"Couverture forêt ({forest}%) conforme."
                if ok_forest else
                f"Couverture forêt trop élevée ({forest}% > {self.rules['forest_max_pct']}%)."
            ),
        })
        if not absent_f:
            score_pts += (1 if ok_forest else 0)
            total_pts += 1
        else:
            warnings.append("Couche 'Forêt' absente — critère non évalué.")

        degraded    = data.get("degraded_count", 0)
        ok_degraded = degraded >= self.rules["degraded_min_count"]
        absent_d    = data.get("degraded_absent", False)
        criteria.append({
            "nom"    : f"Zones dégradées (rayon {self.rules['search_radius_km']} km)",
            "valeur" : "Non évaluée" if absent_d else f"{degraded} zones",
            "requis" : f"≥ {self.rules['degraded_min_count']}",
            "ok"     : None if absent_d else ok_degraded,
            "message": (
                "Couche zones dégradées absente — critère ignoré."
                if absent_d else
                f"{degraded} zones dégradées détectées dans le rayon requis."
                if ok_degraded else
                f"Seulement {degraded} zone(s) dégradée(s) détectée(s)."
            ),
        })
        if not absent_d:
            score_pts += (1 if ok_degraded else 0)
            total_pts += 1
        else:
            warnings.append("Couche 'Zones dégradées' absente — critère non évalué.")

        conflicts = data.get("domain_conflicts", {})
        blocking  = [k for k, v in conflicts.items() if v is True
                     and k in ("domaine_prive", "domaine_public")]
        ok_domain = len(blocking) == 0
        criteria.append({
            "nom"    : "Compatibilité domaines",
            "valeur" : "Aucun conflit" if ok_domain else f"{len(blocking)} conflit(s)",
            "requis" : "Aucun conflit privé/public",
            "ok"     : ok_domain,
            "message": (
                "Site compatible avec les domaines vérifiés."
                if ok_domain else
                "Conflits détectés : " + ", ".join(blocking)
            ),
        })
        score_pts += (1 if ok_domain else 0)
        total_pts += 1

        degrad_site = data.get("degraded_on_site_pct", 0)
        ok_ds       = degrad_site > 0
        criteria.append({
            "nom"    : "Zones dégradées sur le site",
            "valeur" : f"{degrad_site} %",
            "requis" : "> 0 % (favorise l'éligibilité)",
            "ok"     : ok_ds,
            "message": (
                f"{degrad_site}% du site est déjà dégradé — renforce l'éligibilité."
                if ok_ds else
                "Aucune zone dégradée dans l'emprise du site."
            ),
        })
        score_pts += (1 if ok_ds else 0)
        total_pts += 1

        score   = int((score_pts / total_pts) * 100) if total_pts > 0 else 0
        criteres_principaux = [c for c in criteria[:4] if c["ok"] is not None]
        favorable = all(c["ok"] for c in criteres_principaux)

        return {
            "favorable" : favorable,
            "score"     : score,
            "score_pts" : score_pts,
            "total_pts" : total_pts,
            "criteria"  : criteria,
            "warnings"  : warnings,
        }


# =============================================================================
# EXPORT CARTE 
# =============================================================================

def export_map_to_image(output_path, layers, extent, crs, width=900, height=560):
    
    try:
        settings = QgsMapSettings()
        settings.setLayers(layers)
        settings.setExtent(extent)
        settings.setOutputSize(QSize(width, height))
        settings.setBackgroundColor(QColor(255, 255, 255))
        settings.setDestinationCrs(crs)

        pixmap = QPixmap(width, height)
        pixmap.fill(QColor(255, 255, 255))
        painter = QPainter(pixmap)
        job = QgsMapRendererCustomPainterJob(settings, painter)
        job.start()
        job.waitForFinished()
        painter.end()
        pixmap.save(output_path, "PNG")
        return True
    except Exception:
        return False


def build_legend_from_layers(layers):
    
    items = []
    for layer in layers:
        if isinstance(layer, QgsVectorLayer):
            items.append((layer.name(), symbol_color_hex(layer)))
    return items


def padded_extent(extent, factor=0.35):
    x_pad = extent.width() * factor
    y_pad = extent.height() * factor
    return QgsRectangle(
        extent.xMinimum() - x_pad,
        extent.yMinimum() - y_pad,
        extent.xMaximum() + x_pad,
        extent.yMaximum() + y_pad,
    )


# =============================================================================
# GÉNÉRATEUR DE RAPPORT PROFESSIONNEL
# =============================================================================

class ReportGenerator:

    @staticmethod
    def _table_style_header():
        C_HEAD = colors.HexColor("#333333")
        C_BORDER = colors.HexColor("#cccccc")
        C_LIGHT = colors.HexColor("#f5f5f5")
        return [
            ("BACKGROUND", (0, 0), (-1, 0), C_HEAD),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_LIGHT, colors.white]),
            ("GRID", (0, 0), (-1, -1), 0.4, C_BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]

    @staticmethod
    def _export_map(report_layers, map_extent, map_crs):
        import tempfile
        if not report_layers or not map_extent or not map_crs:
            return None
        tmp = tempfile.mktemp(suffix=".png")
        ok = export_map_to_image(
            tmp, report_layers, padded_extent(map_extent), map_crs, 900, 560
        )
        return tmp if ok and os.path.exists(tmp) else None

    @staticmethod
    def generate_pdf(output_path, projet_info, site_info, decision_result,
                     analysis_data, layer_intersections=None,
                     report_layers=None, map_extent=None, map_crs=None):
        if not HAS_REPORTLAB:
            return False, "reportlab non installé (pip install reportlab)"
        try:
            C_DARK = colors.HexColor("#222222")
            C_GRAY = colors.HexColor("#666666")
            C_BORDER = colors.HexColor("#cccccc")

            doc = SimpleDocTemplate(
                output_path, pagesize=A4,
                leftMargin=2 * cm, rightMargin=2 * cm,
                topMargin=2 * cm, bottomMargin=2 * cm,
                title=f"Rapport — {projet_info.get('Nom', '')}",
                author=projet_info.get("Auteur", "GeoDecision"),
            )

            st_title = ParagraphStyle(
                "ptitle", fontSize=18, textColor=C_DARK,
                fontName="Helvetica-Bold", alignment=TA_CENTER, spaceAfter=6,
            )
            st_sub = ParagraphStyle(
                "psub", fontSize=10, textColor=C_GRAY,
                fontName="Helvetica", alignment=TA_CENTER,
            )
            st_sec = ParagraphStyle(
                "psec", fontSize=12, textColor=C_DARK,
                fontName="Helvetica-Bold", spaceBefore=12, spaceAfter=6,
            )
            st_body = ParagraphStyle(
                "pbody", fontSize=10, textColor=colors.black,
                fontName="Helvetica", leading=14,
            )
            st_note = ParagraphStyle(
                "pnote", fontSize=9, textColor=C_GRAY,
                fontName="Helvetica-Oblique", alignment=TA_CENTER,
            )
            st_leg = ParagraphStyle(
                "pleg", fontSize=9, textColor=colors.black, fontName="Helvetica",
            )

            story = []
            story.append(Paragraph("RAPPORT D'AIDE À LA DÉCISION", st_title))
            story.append(Paragraph(
                f"{projet_info.get('Nom', '—')} — {projet_info.get('ID', '—')}", st_sub
            ))
            story.append(Paragraph(
                f"Rédigé par {projet_info.get('Auteur', '—')} — "
                f"{datetime.now().strftime('%d/%m/%Y à %H:%M')}", st_sub
            ))
            story.append(Spacer(1, 12))
            story.append(HRFlowable(width="100%", thickness=1, color=C_BORDER))
            story.append(Spacer(1, 10))

            story.append(Paragraph("1. Informations du projet", st_sec))
            synth = [["Élément", "Information"]]
            for k, v in {**projet_info, **site_info}.items():
                synth.append([k, str(v)])
            synth_tbl = Table(synth, colWidths=[6 * cm, 11 * cm])
            synth_tbl.setStyle(TableStyle(ReportGenerator._table_style_header()))
            story.append(synth_tbl)
            story.append(Spacer(1, 6))
            story.append(Paragraph(
                "Document d'aide à la décision — données et cartographie du site.",
                st_note,
            ))
            story.append(Spacer(1, 12))

            story.append(Paragraph("2. Analyse des intersections avec le buffer", st_sec))
            inter_rows = [["Couche", "Surface (ha)", "% Buffer", "Statut", "Commentaire"]]
            if layer_intersections:
                buf_ha = analysis_data.get("area_ha", 0)
                derog_count = analysis_data.get("derogation_count", 0)
                derog_ha    = analysis_data.get("derogation_ha", 0.0)
                max_derog   = DECISION_RULES["derogation_max_count"]
                max_forest  = DECISION_RULES["forest_max_pct"]
                for nom, pct in layer_intersections.items():
                    if pct is None:
                        surf, statut, comment = "—", "—", "Couche non chargée"
                    elif nom == "Projets dérogués":
                        surf = f"{derog_ha:.2f}"
                        if derog_count < max_derog:
                            statut  = "Éligible"
                            comment = f"{derog_count} projet(s) — sous le seuil de {max_derog}"
                        else:
                            statut  = "Non éligible"
                            comment = f"{derog_count} projet(s) ≥ seuil de {max_derog} — zone saturée"
                    elif nom == "Domaine forestier":
                        surf = f"{round(buf_ha * pct / 100, 2):.2f}" if buf_ha else "—"
                        statut  = "Conforme" if pct <= max_forest else "Non conforme"
                        comment = f"{pct}% {'≤' if pct <= max_forest else '>'} seuil {max_forest}%"
                    else:
                        surf = f"{round(buf_ha * pct / 100, 2):.2f}" if buf_ha else "—"
                        statut  = "Aucune intersection" if pct == 0.0 else "Intersection détectée"
                        comment = f"{pct}% du buffer"
                    inter_rows.append([nom, surf, f"{pct} %" if pct is not None else "—", statut, comment])
            else:
                inter_rows.append(["—", "—", "—", "—", "Analyse non disponible"])
            inter_tbl = Table(inter_rows, colWidths=[4*cm, 2.5*cm, 2*cm, 3*cm, 5.5*cm])
            inter_tbl.setStyle(TableStyle(ReportGenerator._table_style_header()))
            story.append(inter_tbl)
            story.append(Spacer(1, 12))

            story.append(Spacer(1, 12))
            story.append(Paragraph("4. Carte et légende", st_sec))

            map_img = ReportGenerator._export_map(report_layers, map_extent, map_crs)
            legend_items = build_legend_from_layers(report_layers or [])

            if map_img:
                map_rl = RLImage(map_img, width=12 * cm, height=7.5 * cm)
                leg_content = [Paragraph("Légende", ParagraphStyle(
                    "legt", fontSize=10, fontName="Helvetica-Bold", textColor=C_DARK,
                ))]
                for lname, lcol in legend_items:
                    swatch = Table([[""]], colWidths=[0.5 * cm], rowHeights=[0.35 * cm],
                                   style=TableStyle([
                                       ("BACKGROUND", (0, 0), (-1, -1),
                                        colors.HexColor(lcol if lcol.startswith("#") else "#888888")),
                                       ("BOX", (0, 0), (-1, -1), 0.3, C_BORDER),
                                   ]))
                    leg_content.append(Table(
                        [[swatch, Paragraph(lname, st_leg)]],
                        colWidths=[0.7 * cm, 4 * cm],
                        style=[("VALIGN", (0, 0), (-1, -1), "MIDDLE")],
                    ))
                map_tbl = Table([[map_rl, leg_content]], colWidths=[12.5 * cm, 4.5 * cm])
                map_tbl.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
                ]))
                story.append(map_tbl)
            else:
                story.append(Paragraph("Carte non disponible.", st_note))

            story.append(Spacer(1, 12))
            story.append(Paragraph("5. Indicateurs complémentaires", st_sec))
            met_rows = [["Indicateur", "Valeur"]]
            metrics = [
                ("Superficie du buffer", f"{analysis_data.get('area_ha', '—')} ha"),
                ("Couverture forestière", f"{analysis_data.get('forest_pct', '—')} %"),
                ("Zones dégradées dans le buffer", str(analysis_data.get("degraded_count", "—"))),
                ("Zones dégradées sur le site", f"{analysis_data.get('degraded_on_site_pct', '—')} %"),
            ]
            for k, v in metrics:
                met_rows.append([k, v])
            met_tbl = Table(met_rows, colWidths=[10 * cm, 7 * cm])
            met_tbl.setStyle(TableStyle(ReportGenerator._table_style_header()))
            story.append(met_tbl)

            def footer(canvas_obj, doc_obj):
                canvas_obj.saveState()
                canvas_obj.setFont("Helvetica", 7)
                canvas_obj.setFillColor(C_GRAY)
                canvas_obj.drawCentredString(
                    A4[0] / 2, 12,
                    f"GeoDecision — Page {doc_obj.page} — {datetime.now().strftime('%d/%m/%Y')}",
                )
                canvas_obj.restoreState()

            doc.build(story, onFirstPage=footer, onLaterPages=footer)
            if map_img and os.path.exists(map_img):
                try:
                    os.remove(map_img)
                except Exception:
                    pass
            return True, output_path
        except Exception as e:
            return False, str(e)

    @staticmethod
    def generate_word(output_path, projet_info, site_info, decision_result,
                      analysis_data, layer_intersections=None,
                      report_layers=None, map_extent=None, map_crs=None):
        if not HAS_DOCX:
            return False, "python-docx non installé (pip install python-docx)"
        try:
            doc = Document()
            for section in doc.sections:
                section.left_margin = section.right_margin = Cm(2.5)
                section.top_margin = section.bottom_margin = Cm(2)

            titre = doc.add_heading("Rapport d'aide à la décision", 0)
            titre.alignment = WD_ALIGN_PARAGRAPH.CENTER
            doc.add_paragraph(
                f"{projet_info.get('Nom', '—')} — {projet_info.get('ID', '—')}\n"
                f"Auteur : {projet_info.get('Auteur', '—')} — "
                f"{datetime.now().strftime('%d/%m/%Y à %H:%M')}"
            )

            doc.add_heading("1. Informations du projet", level=1)
            for k, v in {**projet_info, **site_info}.items():
                row = doc.add_paragraph()
                row.add_run(f"{k} : ").bold = True
                row.add_run(str(v))

            doc.add_heading("2. Analyse des intersections avec le buffer", level=1)
            inter_tbl = doc.add_table(rows=1, cols=5)
            inter_tbl.style = "Table Grid"
            for i, h in enumerate(["Couche", "Surface (ha)", "% Buffer", "Statut", "Commentaire"]):
                inter_tbl.rows[0].cells[i].text = h
            if layer_intersections:
                buf_ha      = analysis_data.get("area_ha", 0)
                derog_count = analysis_data.get("derogation_count", 0)
                derog_ha    = analysis_data.get("derogation_ha", 0.0)
                max_derog   = DECISION_RULES["derogation_max_count"]
                max_forest  = DECISION_RULES["forest_max_pct"]
                for nom, pct in layer_intersections.items():
                    r = inter_tbl.add_row().cells
                    r[0].text = nom
                    if pct is None:
                        r[1].text = "—"; r[2].text = "—"; r[3].text = "—"; r[4].text = "Couche non chargée"
                    elif nom == "Projets dérogués":
                        r[1].text = f"{derog_ha:.2f}"
                        r[2].text = f"{pct} %" if pct is not None else "—"
                        if derog_count < max_derog:
                            r[3].text = "Éligible"; r[4].text = f"{derog_count} projet(s) — sous le seuil de {max_derog}"
                        else:
                            r[3].text = "Non éligible"; r[4].text = f"{derog_count} projet(s) ≥ seuil de {max_derog}"
                    elif nom == "Domaine forestier":
                        surf = round(buf_ha * pct / 100, 2) if buf_ha else 0
                        r[1].text = f"{surf:.2f}"; r[2].text = f"{pct} %"
                        r[3].text = "Conforme" if pct <= max_forest else "Non conforme"
                        r[4].text = f"{pct}% {'≤' if pct <= max_forest else '>'} seuil {max_forest}%"
                    else:
                        surf = round(buf_ha * pct / 100, 2) if buf_ha else 0
                        r[1].text = f"{surf:.2f}"; r[2].text = f"{pct} %"
                        r[3].text = "Aucune intersection" if pct == 0.0 else "Intersection détectée"
                        r[4].text = f"{pct}% du buffer"

            doc.add_heading("4. Carte et légende", level=1)
            map_img = ReportGenerator._export_map(report_layers, map_extent, map_crs)
            if map_img:
                doc.add_picture(map_img, width=Cm(15))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                doc.add_heading("Légende", level=2)
                leg_tbl = doc.add_table(rows=1, cols=2)
                leg_tbl.style = "Table Grid"
                leg_tbl.rows[0].cells[0].text = "Couche"
                leg_tbl.rows[0].cells[1].text = "Symbole"
                for lname, lcol in build_legend_from_layers(report_layers or []):
                    r = leg_tbl.add_row().cells
                    r[0].text = lname
                    r[1].text = lcol
                try:
                    os.remove(map_img)
                except Exception:
                    pass
            else:
                doc.add_paragraph("Carte non disponible.")

            doc.add_heading("5. Indicateurs complémentaires", level=1)
            met_tbl = doc.add_table(rows=1, cols=2)
            met_tbl.style = "Table Grid"
            met_tbl.rows[0].cells[0].text = "Indicateur"
            met_tbl.rows[0].cells[1].text = "Valeur"
            for k, v in [
                ("Superficie du buffer", f"{analysis_data.get('area_ha', '—')} ha"),
                ("Couverture forestière", f"{analysis_data.get('forest_pct', '—')} %"),
                ("Zones dégradées dans le buffer", str(analysis_data.get("degraded_count", "—"))),
                ("Zones dégradées sur le site", f"{analysis_data.get('degraded_on_site_pct', '—')} %"),
            ]:
                r = met_tbl.add_row().cells
                r[0].text = k
                r[1].text = v

            doc.save(output_path)
            return True, output_path
        except Exception as e:
            return False, str(e)


# =============================================================================
# FORMULAIRE D'UN PROJET
# =============================================================================

class ProjetWidget(QWidget):

    def __init__(self, projet_id, projet_nom, iface, dialog_ref=None, parent=None):
        super().__init__(parent)
        self.iface           = iface
        self.projet_id       = projet_id
        self.projet_nom      = projet_nom
        self._dialog_ref     = dialog_ref  
        self.vector_analysis = VectorAnalysis()
        self.decision_engine = DecisionEngine()
        self.study_layer     = None
        self.buffer_layer    = None
        self.centroid_layer  = None
        self.intersection_layer = None
        self.analysis_data   = {}
        self.layer_intersections = {}
        self.decision_result = None
        self._map_tool       = None
        self._selected_point = None

        self._build_ui()

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        # ── 1. Informations du projet ─────────────────────────
        
        grp_projet = QGroupBox("Informations du projet")
        g0 = QGridLayout(grp_projet)

        g0.addWidget(QLabel("ID du projet :"), 0, 0)
        self.edit_id = QLineEdit(self.projet_id)
        self.edit_id.setReadOnly(True)
        self.edit_id.setToolTip("Identifiant unique généré automatiquement — non modifiable")
        g0.addWidget(self.edit_id, 0, 1)

        lbl_auto = QLabel("(généré automatiquement)")
        lbl_auto.setStyleSheet("color: #888888; font-style: italic; font-size: 10px;")
        g0.addWidget(lbl_auto, 0, 2)

        g0.addWidget(QLabel("Nom du projet :"), 1, 0)
        self.edit_nom = QLineEdit(self.projet_nom)
        self.edit_nom.setPlaceholderText("Saisissez le nom du projet…")
        self.edit_nom.textChanged.connect(self._update_sidebar_title)
        g0.addWidget(self.edit_nom, 1, 1, 1, 2)

        lay.addWidget(grp_projet)

        # ── 2. Couches ─────────────────────────────────────────
        
        grp_couches = QGroupBox("Couches du plan d'aménagement")
        gc = QVBoxLayout(grp_couches)

        grid_couches = QGridLayout()
        self.layer_inputs = {}
        self.layer_combos = {}
        couches = [
            ("foret",       "Domaine forestier"),
            ("public",      "Domaine public"),
            ("prive",       "Domaine privé"),
            ("collectif",   "Domaine collectif"),
            ("communal",    "Domaine communal"),
            ("derogation",  "Projets dérogués"),
        ]

        grid_couches.addWidget(QLabel("Couche"),            0, 0)
        grid_couches.addWidget(QLabel("Couche active QGIS"), 0, 1)
        grid_couches.addWidget(QLabel("Ou fichier SHP"),    0, 2)

        for i, (key, label) in enumerate(couches, start=1):
            grid_couches.addWidget(QLabel(f"{label} :"), i, 0)

            combo = QComboBox()
            combo.addItem("— aucune —", None)
            for layer in QgsProject.instance().mapLayers().values():
                if isinstance(layer, QgsVectorLayer):
                    combo.addItem(layer.name(), layer.id())
            self.layer_combos[key] = combo
            grid_couches.addWidget(combo, i, 1)

            row_shp = QHBoxLayout()
            le = QLineEdit()
            le.setPlaceholderText("Chemin .shp…")
            self.layer_inputs[key] = le
            row_shp.addWidget(le)
            btn = QPushButton("Parcourir")
            btn.setFixedWidth(75)
            btn.clicked.connect(lambda checked, k=key: self._browse_layer(k))
            row_shp.addWidget(btn)
            grid_couches.addLayout(row_shp, i, 2)

        gc.addLayout(grid_couches)

        # Bouton en DESSOUS du tableau des couches
        btn_qgis = QPushButton("Charger automatiquement depuis les couches actives QGIS")
        btn_qgis.clicked.connect(self._load_from_qgis)
        gc.addWidget(btn_qgis)

        lay.addWidget(grp_couches)

        # ── 3. Zone d'étude ─────────────────────────────────────
        grp_zone = QGroupBox("Zone d'étude")
        gz = QVBoxLayout(grp_zone)

        btn_row = QHBoxLayout()
        self.radio_coords  = QPushButton("Coordonnées")
        self.radio_point   = QPushButton("Point sur carte")
        self.radio_fichier = QPushButton("Fichier SHP")
        for btn in [self.radio_coords, self.radio_point, self.radio_fichier]:
            btn.setCheckable(True)
            btn.setObjectName("modeBtn")
            btn_row.addWidget(btn)
        self.radio_coords.setChecked(False)
        self.radio_point.setChecked(True)
        self.radio_coords.clicked.connect(lambda: self._switch_zone_mode(0))
        self.radio_point.clicked.connect(lambda: self._switch_zone_mode(1))
        self.radio_fichier.clicked.connect(lambda: self._switch_zone_mode(2))
        gz.addLayout(btn_row)

        self.zone_stack = QStackedWidget()

        
        w_coords = QWidget()
        g_coords = QGridLayout(w_coords)

        lbl_info = QLabel("Saisissez les coordonnées du centroïde du projet :")
        lbl_info.setStyleSheet("color: #555555; font-style: italic;")
        g_coords.addWidget(lbl_info, 0, 0, 1, 4)

        g_coords.addWidget(QLabel("X (Longitude) :"), 1, 0)
        self.coord_x = QLineEdit("-5.817")
        self.coord_x.setPlaceholderText("Ex : -5.817")
        g_coords.addWidget(self.coord_x, 1, 1)

        g_coords.addWidget(QLabel("Y (Latitude) :"), 1, 2)
        self.coord_y = QLineEdit("35.656")
        self.coord_y.setPlaceholderText("Ex : 35.656")
        g_coords.addWidget(self.coord_y, 1, 3)

        btn_coord = QPushButton("Créer le centroïde et le buffer")
        btn_coord.clicked.connect(self._create_aoi_from_coords)
        g_coords.addWidget(btn_coord, 2, 0, 1, 4)
        self.zone_stack.addWidget(w_coords)

        
        w_point = QWidget()
        g_point = QVBoxLayout(w_point)
        g_point.addWidget(
            QLabel(
                "Cliquez sur « Choisir point sur carte », puis cliquez sur la carte.\n"
                "Le point devient le centroïde (My_Project) ; "
                "un buffer (Buffer_Derogation) est créé automatiquement."
            )
        )
        row_pt = QHBoxLayout()
        self.lbl_point = QLabel("Aucun centroïde défini")
        self.lbl_point.setStyleSheet("font-style: italic; color: #666666;")
        row_pt.addWidget(self.lbl_point, 1)
        btn_pick = QPushButton("Choisir point sur carte")
        btn_pick.clicked.connect(self._activate_map_tool)
        row_pt.addWidget(btn_pick)
        g_point.addLayout(row_pt)
        self.zone_stack.addWidget(w_point)

        
        w_fich = QWidget()
        g_fich = QHBoxLayout(w_fich)
        self.zone_path = QLineEdit()
        self.zone_path.setPlaceholderText("Chemin vers le fichier de la zone d'étude…")
        g_fich.addWidget(self.zone_path)
        btn_browse_z = QPushButton("Parcourir")
        btn_browse_z.clicked.connect(self._browse_zone)
        g_fich.addWidget(btn_browse_z)
        btn_import_z = QPushButton("Importer")
        btn_import_z.clicked.connect(self._import_zone)
        g_fich.addWidget(btn_import_z)
        self.zone_stack.addWidget(w_fich)

        gz.addWidget(self.zone_stack)
        self._switch_zone_mode(1)

        buf_row = QHBoxLayout()
        buf_row.addWidget(QLabel("Rayon du buffer (km) :"))
        self.spin_buffer = QDoubleSpinBox()
        self.spin_buffer.setRange(0.1, 50.0)
        self.spin_buffer.setValue(1.0)
        self.spin_buffer.setSingleStep(0.1)
        self.spin_buffer.setSuffix(" km")
        self.spin_buffer.setToolTip("Rayon du buffer autour du centroïde du projet.")
        self.spin_buffer.valueChanged.connect(self._on_buffer_radius_changed)
        buf_row.addWidget(self.spin_buffer)
        buf_row.addStretch()
        gz.addLayout(buf_row)

        self.lbl_zone_statut = QLabel("Aucune zone définie.")
        self.lbl_zone_statut.setObjectName("zoneStatut")
        gz.addWidget(self.lbl_zone_statut)

        self.lbl_buffer_statut = QLabel("")
        self.lbl_buffer_statut.setObjectName("bufferStatut")
        gz.addWidget(self.lbl_buffer_statut)

        lay.addWidget(grp_zone)

        # ── 4. Analyse ─────────────────────────────────────────
        
        grp_analyse = QGroupBox("Analyse")
        ga = QVBoxLayout(grp_analyse)

        btn_run = QPushButton("Lancer l'analyse vectorielle")
        btn_run.setObjectName("btnRunAnalysis")
        btn_run.setFixedHeight(38)
        btn_run.clicked.connect(self._run_analysis)
        ga.addWidget(btn_run)

        ga.addWidget(QLabel("Résultats de l'analyse — Intersection avec le buffer"))
        self.table_analyse = QTableWidget(0, 5)
        self.table_analyse.setHorizontalHeaderLabels([
            "Couche", "Surface (ha)", "% Buffer", "Statut", "Commentaire"
        ])
        self.table_analyse.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table_analyse.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table_analyse.verticalHeader().setVisible(False)
        self.table_analyse.setMinimumHeight(50)
        self.table_analyse.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.table_analyse.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table_analyse.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table_analyse.setAlternatingRowColors(True)
        ga.addWidget(self.table_analyse)

        grp_analyse.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay.addWidget(grp_analyse)

        # ── 5. Rapport ─────────────────────────────────────────
        grp_rapport = QGroupBox("Rapport d'aide à la décision")
        gr = QVBoxLayout(grp_rapport)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Auteur :"))
        self.edit_auteur = QLineEdit("Ingénieur SIG")
        r1.addWidget(self.edit_auteur)
        gr.addLayout(r1)

        r2 = QHBoxLayout()
        btn_pdf  = QPushButton("Télécharger PDF")
        btn_word = QPushButton("Télécharger Word")
        btn_pdf.clicked.connect(lambda: self._generer_rapport("pdf"))
        btn_word.clicked.connect(lambda: self._generer_rapport("docx"))
        r2.addWidget(btn_pdf)
        r2.addWidget(btn_word)
        gr.addLayout(r2)

        self.lbl_rapport = QLabel("")
        gr.addWidget(self.lbl_rapport)
        grp_rapport.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay.addWidget(grp_rapport)

        scroll.setWidget(container)
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.addWidget(scroll)

    # ─────────────────────────────────────────
    #  Zone d'étude
    # ─────────────────────────────────────────

    def _switch_zone_mode(self, idx):
        self.zone_stack.setCurrentIndex(idx)
        for i, btn in enumerate([self.radio_coords, self.radio_point, self.radio_fichier]):
            btn.setChecked(i == idx)

    def _remove_project_layer(self, layer):
        if not layer:
            return
        try:
            QgsProject.instance().removeMapLayer(layer.id())
        except Exception:
            pass

    def _on_buffer_radius_changed(self):
        if self._selected_point:
            x, y = self._selected_point
            self._create_centroid_and_buffer(x, y)

    def _create_aoi_bbox(self):
        try:
            xmin = float(self.xmin.text()); ymin = float(self.ymin.text())
            xmax = float(self.xmax.text()); ymax = float(self.ymax.text())
        except ValueError:
            QMessageBox.warning(self, "Erreur", "Coordonnées invalides.")
            return
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", f"Zone_{self.edit_id.text()}", "memory")
        pr    = layer.dataProvider()
        feat  = QgsFeature()
        feat.setGeometry(QgsGeometry.fromRect(QgsRectangle(xmin, ymin, xmax, ymax)))
        pr.addFeature(feat); layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self._set_study_layer(layer)
        # Pas de buffer automatique pour bbox, on crée quand même un buffer de rayon
        self._create_buffer_layer(layer)

    def _activate_map_tool(self):
        canvas = self.iface.mapCanvas()
        self._map_tool = PointMapTool(canvas)
        self._map_tool.point_selected.connect(self._on_point_selected)
        canvas.setMapTool(self._map_tool)
        self.lbl_point.setText("Cliquez sur la carte pour sélectionner un point…")

    def _on_point_selected(self, x, y):
        self._selected_point = (x, y)
        self.iface.mapCanvas().unsetMapTool(self._map_tool)
        self._create_centroid_and_buffer(x, y)

    def _create_centroid_and_buffer(self, x, y):
        
        crs = project_crs()
        crs_auth = crs.authid()
        radius_km = self.spin_buffer.value()
        buffer_geom = create_buffer_geometry(x, y, radius_km, crs)

        if not buffer_geom or buffer_geom.isEmpty():
            QMessageBox.warning(
                self, "Erreur",
                "Impossible de calculer la géométrie du buffer.",
            )
            return

        self._remove_project_layer(self.centroid_layer)
        self._remove_project_layer(self.buffer_layer)
        self._remove_project_layer(self.intersection_layer)
        self.centroid_layer = None
        self.buffer_layer = None
        self.intersection_layer = None

        centroid_layer = QgsVectorLayer(
            f"Point?crs={crs_auth}", "My_Project", "memory",
        )
        pr = centroid_layer.dataProvider()
        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        pr.addFeature(feat)
        centroid_layer.updateExtents()
        style_project_point(centroid_layer)

        buf_layer = QgsVectorLayer(
            f"Polygon?crs={crs_auth}", "Buffer_Derogation", "memory",
        )
        pr_buf = buf_layer.dataProvider()
        feat_buf = QgsFeature()
        feat_buf.setGeometry(buffer_geom)
        if not pr_buf.addFeature(feat_buf):
            QMessageBox.warning(self, "Erreur", "Impossible de créer le buffer.")
            return
        buf_layer.updateExtents()
        style_buffer_layer(buf_layer)

        
        add_layer_to_map(buf_layer)
        add_layer_to_map(centroid_layer)
        self.buffer_layer = buf_layer
        self.centroid_layer = centroid_layer

        area_ha = self.vector_analysis.compute_area_ha(buf_layer)
        self._set_study_layer(buf_layer, centroid=(x, y))
        self.lbl_point.setText(
            f"Centroïde : {x:.5f}, {y:.5f}  |  Buffer : {radius_km} km"
        )
        self.lbl_buffer_statut.setText(
            f"Buffer_Derogation créé — {buf_layer.featureCount()} entité, "
            f"{area_ha:.2f} ha, rayon {radius_km} km"
        )
        self.iface.mapCanvas().refreshAllLayers()

    def _create_aoi_from_coords(self):
        
        try:
            x = float(self.coord_x.text().replace(',', '.').strip())
            y = float(self.coord_y.text().replace(',', '.').strip())
        except ValueError:
            QMessageBox.warning(self, "Erreur", "Coordonnées invalides. Vérifiez les valeurs X et Y.")
            return
        self._selected_point = (x, y)
        self._create_centroid_and_buffer(x, y)

    def _create_aoi_point(self):
        
        if not self._selected_point:
            QMessageBox.warning(
                self, "Point manquant",
                "Sélectionnez d'abord un point sur la carte.",
            )
            return
        x, y = self._selected_point
        self._create_centroid_and_buffer(x, y)

    def _create_buffer_layer(self, base_layer):
        
        try:
            geom = next((f.geometry() for f in base_layer.getFeatures() if f.geometry()), None)
            if not geom:
                return
            center = geom.centroid().asPoint()
            self._selected_point = (center.x(), center.y())
            self._create_centroid_and_buffer(center.x(), center.y())
        except Exception as e:
            self.lbl_buffer_statut.setText(f"Buffer non créé : {e}")

    def _browse_zone(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Zone d'étude", "", "Fichiers vecteur (*.shp *.geojson *.kml *.gpkg)"
        )
        if path:
            self.zone_path.setText(path)

    def _import_zone(self):
        path = self.zone_path.text().strip()
        if not path:
            return
        layer = QgsVectorLayer(path, f"Zone_{self.edit_id.text()}", "ogr")
        if not layer.isValid():
            QMessageBox.warning(self, "Erreur", "Fichier invalide ou introuvable.")
            return
        QgsProject.instance().addMapLayer(layer)
        self._set_study_layer(layer)
        self._create_buffer_layer(layer)

    def _set_study_layer(self, layer, centroid=None):
        self.study_layer = layer
        layer.updateExtents()
        area = self.vector_analysis.compute_area_ha(layer)
        if area == 0.0:
            try:
                geom = next((f.geometry() for f in layer.getFeatures() if f.geometry()), None)
                if geom:
                    area_deg2 = geom.area()
                    area = round(area_deg2 * 111000 * 111000 / 10000, 2)
            except Exception:
                area = 0.0
        self.analysis_data["area_ha"] = area
        if centroid:
            self.analysis_data["centroid_x"] = centroid[0]
            self.analysis_data["centroid_y"] = centroid[1]
        self.analysis_data["buffer_km"] = self.spin_buffer.value()
        ok = area >= DECISION_RULES["area_min_ha"]
        statut = "conforme" if ok else "insuffisante"
        if centroid:
            self.lbl_zone_statut.setText(
                f"Centroïde ({centroid[0]:.5f}, {centroid[1]:.5f}) — "
                f"Buffer {layer.name()} — {area:.2f} ha ({statut})"
            )
        else:
            self.lbl_zone_statut.setText(
                f"Zone chargée : {layer.name()} — {area:.2f} ha ({statut})"
            )

    # ─────────────────────────────────────────
    #  Couches
    # ─────────────────────────────────────────

    def _load_from_qgis(self):
        KEYWORDS = {
            "foret"    : ["foret", "forêt", "forest", "forestier"],
            "public"   : ["public"],
            "prive"    : ["prive", "privé", "private", "etat"],
            "collectif": ["collectif"],
            "communal" : ["communal", "commune"],
            "degrade"    : ["degrade", "dégradé", "degraded", "degradation"],
            "derogation" : ["derogation", "derogué", "derogu"],
        }
        found = 0
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                # Chercher dans le nom ET dans le chemin source
                lname = layer.name().lower()
                src = layer.dataProvider().dataSourceUri().split("|")[0]
                lpath = os.path.basename(src).lower() if src else ""
                search_str = lname + " " + lpath  # concaténation pour chercher dans les deux
                for key, kws in KEYWORDS.items():
                    if any(kw in search_str for kw in kws):
                        if key in self.layer_inputs:
                            self.layer_inputs[key].setText(src)
                            # Mettre aussi à jour le combo si la couche est active
                            combo = self.layer_combos.get(key)
                            if combo:
                                for i in range(combo.count()):
                                    if combo.itemData(i) == layer.id():
                                        combo.setCurrentIndex(i)
                                        break
                            found += 1
                        break

    def _browse_layer(self, key):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Couche {key}", "", "Fichiers vecteur (*.shp *.geojson *.kml *.gpkg)"
        )
        if path:
            self.layer_inputs[key].setText(path)

    def _load_layer(self, key):
        combo = self.layer_combos.get(key)
        if combo and combo.currentData():
            layer = QgsProject.instance().mapLayer(combo.currentData())
            if layer and layer.isValid():
                return layer
        path = self.layer_inputs.get(key, QLineEdit()).text().strip()
        if path and os.path.exists(path):
            layer = QgsVectorLayer(path, key, "ogr")
            return layer if layer.isValid() else None
        return None

    # ─────────────────────────────────────────
    #  Analyse
    # ─────────────────────────────────────────

    def _run_analysis(self):
        analysis_layer = self.buffer_layer or self.study_layer
        if not analysis_layer:
            QMessageBox.warning(
                self, "Zone manquante",
                "Définissez d'abord la zone d'étude (centroïde + buffer).",
            )
            return

        area = self.vector_analysis.compute_area_ha(analysis_layer)
        if area == 0.0:
            try:
                geom = next(
                    (f.geometry() for f in analysis_layer.getFeatures() if f.geometry()),
                    None,
                )
                if geom:
                    area = round(geom.area() * 111000 * 111000 / 10000, 2)
            except Exception:
                area = 0.0
        self.analysis_data["area_ha"] = area

        layer_defs = [
            ("foret",      "Domaine forestier"),
            ("public",     "Domaine public"),
            ("prive",      "Domaine privé"),
            ("collectif",  "Domaine collectif"),
            ("communal",   "Domaine communal"),
            ("derogation", "Projets dérogués"),
        ]
        self.layer_intersections = {}
        for key, label in layer_defs:
            layer = self._load_layer(key)
            if layer and analysis_layer:
                pct = self.vector_analysis.compute_layer_intersection_pct(
                    analysis_layer, layer
                )
                self.layer_intersections[label] = pct
            else:
                self.layer_intersections[label] = None

        forest_layer = self._load_layer("foret")
        if forest_layer:
            forest_pct = self.vector_analysis.compute_forest_coverage(
                analysis_layer, forest_layer
            )
            self.analysis_data["forest_pct"] = forest_pct
            self.analysis_data["forest_absent"] = False
        else:
            self.analysis_data["forest_pct"] = 0.0
            self.analysis_data["forest_absent"] = True

        degrade_layer = self._load_layer("degrade")
        if degrade_layer:
            degraded_count = self.vector_analysis.count_degraded_zones(
                analysis_layer, degrade_layer, self.spin_buffer.value()
            )
            degrad_site_pct = self.vector_analysis.compute_degraded_on_site(
                analysis_layer, degrade_layer
            )
            self.analysis_data["degraded_count"] = degraded_count
            self.analysis_data["degraded_on_site_pct"] = degrad_site_pct
            self.analysis_data["degraded_absent"] = False
        else:
            self.analysis_data["degraded_count"] = 0
            self.analysis_data["degraded_on_site_pct"] = 0.0
            self.analysis_data["degraded_absent"] = True

        domain_layers = {
            "domaine_prive": self._load_layer("prive"),
            "domaine_public": self._load_layer("public"),
            "domaine_collectif": self._load_layer("collectif"),
            "domaine_communal": self._load_layer("communal"),
        }
        conflicts = self.vector_analysis.check_domain_conflicts(
            analysis_layer, domain_layers
        )
        self.analysis_data["domain_conflicts"] = conflicts
        self.analysis_data["buffer_km"] = self.spin_buffer.value()

        
        derogation_layer = self._load_layer("derogation")
        if derogation_layer:
            derog_count, derog_ha = self.vector_analysis.compute_derogation_in_buffer(
                analysis_layer, derogation_layer
            )
            self.analysis_data["derogation_count"] = derog_count
            self.analysis_data["derogation_ha"]    = derog_ha
            self.analysis_data["derogation_absent"] = False
        else:
            self.analysis_data["derogation_count"] = 0
            self.analysis_data["derogation_ha"]    = 0.0
            self.analysis_data["derogation_absent"] = True

        layers_for_inter = {
            label: self._load_layer(key) for key, label in layer_defs
        }
        self._remove_project_layer(self.intersection_layer)
        self.intersection_layer = self.vector_analysis.create_intersection_layer(
            analysis_layer, layers_for_inter, "Intersected_Layers"
        )
        if self.intersection_layer:
            add_layer_to_map(self.intersection_layer)

        self.decision_result = self.decision_engine.evaluate(self.analysis_data)
        self._afficher_intersections()
        self._afficher_resultats()
        self.iface.mapCanvas().refresh()

    def _afficher_intersections(self):
        
        self.table_analyse.setRowCount(0)
        buf_area_ha = self.analysis_data.get("area_ha", 0)

        
        max_forest  = DECISION_RULES["forest_max_pct"]
        max_derog   = DECISION_RULES["derogation_max_count"]
        derog_count = self.analysis_data.get("derogation_count", 0)
        derog_ha    = self.analysis_data.get("derogation_ha", 0.0)
        derog_abs   = self.analysis_data.get("derogation_absent", True)

        COLOR_OK   = QColor(230, 255, 230)   
        COLOR_NOK  = QColor(255, 230, 230)   
        COLOR_NA   = QColor(245, 245, 245)   

        def add_row(label, pct, surf_ha, statut_txt, comment, color):
            row = self.table_analyse.rowCount()
            self.table_analyse.insertRow(row)
            items = [
                QTableWidgetItem(label),
                QTableWidgetItem("—" if surf_ha is None else f"{surf_ha:.2f}"),
                QTableWidgetItem("Non chargée" if pct is None else f"{pct} %"),
                QTableWidgetItem(statut_txt),
                QTableWidgetItem(comment),
            ]
            for col, item in enumerate(items):
                item.setBackground(color)
                if col == 3:
                    _f = QFont("Arial", 9); _f.setBold(True); item.setFont(_f)
                self.table_analyse.setItem(row, col, item)

        
        domaine_keys = [
            ("Domaine forestier",  "forest"),
            ("Domaine public",     "public"),
            ("Domaine privé",      "prive"),
            ("Domaine collectif",  "collectif"),
            ("Domaine communal",   "communal"),
        ]
        for label, _ in domaine_keys:
            pct = self.layer_intersections.get(label)
            surf_ha = round(buf_area_ha * pct / 100, 2) if pct is not None and buf_area_ha else None
            if pct is None:
                statut, comment, color = "—", "Couche non chargée", COLOR_NA
            elif label == "Domaine forestier":
                if pct <= max_forest:
                    statut = "Conforme"
                    comment = f"Couverture forêt ({pct}%) ≤ seuil de {max_forest}%"
                    color = COLOR_OK
                else:
                    statut = "Non conforme"
                    comment = f"Couverture forêt ({pct}%) > seuil de {max_forest}%"
                    color = COLOR_NOK
            else:
                if pct == 0.0:
                    statut, comment, color = "Aucune intersection", f"Aucune surface de {label.lower()} dans le buffer", COLOR_OK
                else:
                    statut, comment, color = "Intersection détectée", f"{pct}% du buffer chevauche le {label.lower()}", COLOR_NOK
            add_row(label, pct, surf_ha, statut, comment, color)

        # Ligne Projets dérogués
        pct_derog = self.layer_intersections.get("Projets dérogués")
        if derog_abs:
            add_row("Projets dérogués", None, None, "—", "Couche non chargée", COLOR_NA)
        else:
            if derog_count < max_derog:
                statut  = "Éligible"
                comment = f"{derog_count} projet(s) dérogué(s) dans le buffer — sous le seuil de {max_derog}"
                color   = COLOR_OK
            else:
                statut  = "Non éligible"
                comment = f"{derog_count} projet(s) dérogué(s) dans le buffer ≥ seuil de {max_derog} — zone saturée"
                color   = COLOR_NOK
            add_row("Projets dérogués", pct_derog, derog_ha, statut, comment, color)

        
        row = self.table_analyse.rowCount()
        self.table_analyse.insertRow(row)
        sep = QTableWidgetItem(f"Surface totale du buffer : {buf_area_ha:.2f} ha")
        sep.setBackground(QColor(240, 240, 240))
        _fb = QFont("Arial", 9); _fb.setBold(True); sep.setFont(_fb)
        self.table_analyse.setItem(row, 0, sep)
        self.table_analyse.setSpan(row, 0, 1, 5)

        self.table_analyse.resizeRowsToContents()
        self.table_analyse.resizeColumnsToContents()

        
        header_h = self.table_analyse.horizontalHeader().height()
        rows_h = sum(
            self.table_analyse.rowHeight(i)
            for i in range(self.table_analyse.rowCount())
        )
        self.table_analyse.setFixedHeight(header_h + rows_h + 4)

    def _afficher_resultats(self):
        
        pass

    # ─────────────────────────────────────────
    #  Rapport
    # ─────────────────────────────────────────

    def _get_report_layers(self):
        
        layers = []
        
        if self.centroid_layer:
            layers.append(self.centroid_layer)
        if self.buffer_layer:
            layers.append(self.buffer_layer)
        if self.intersection_layer:
            layers.append(self.intersection_layer)
        
        for key in reversed(("foret", "public", "prive", "collectif", "communal", "degrade")):
            lyr = self._load_layer(key)
            if lyr:
                layers.append(lyr)
        return layers

    def _generer_rapport(self, fmt):
        if not self.decision_result:
            QMessageBox.warning(
                self, "Analyse requise",
                "Lancez d'abord l'analyse avant de télécharger un rapport.",
            )
            return
        ext = "PDF (*.pdf)" if fmt == "pdf" else "Word (*.docx)"
        defaut = f"rapport_{self.edit_id.text().replace(' ', '_')}.{fmt}"
        path, _ = QFileDialog.getSaveFileName(self, "Enregistrer le rapport", defaut, ext)
        if not path:
            return

        cx = self.analysis_data.get("centroid_x")
        cy = self.analysis_data.get("centroid_y")
        centroid_txt = f"{cx:.5f}, {cy:.5f}" if cx is not None else "—"

        projet_info = {
            "ID": self.edit_id.text(),
            "Nom": self.edit_nom.text(),
            "Auteur": self.edit_auteur.text(),
            "Date": datetime.now().strftime("%d/%m/%Y"),
            "Rayon buffer": f"{self.spin_buffer.value()} km",
        }
        site_info = {
            "Centroïde (WGS84)": centroid_txt,
            "Buffer": self.buffer_layer.name() if self.buffer_layer else "—",
            "Superficie buffer": f"{self.analysis_data.get('area_ha', '—')} ha",
        }
        analysis_display = {
            "area_ha": self.analysis_data.get("area_ha", "—"),
            "forest_pct": self.analysis_data.get("forest_pct", "—"),
            "degraded_count": self.analysis_data.get("degraded_count", "—"),
            "degraded_on_site_pct": self.analysis_data.get("degraded_on_site_pct", "—"),
        }
        report_layers = self._get_report_layers()
        map_extent = self.buffer_layer.extent() if self.buffer_layer else None
        map_crs = self.buffer_layer.crs() if self.buffer_layer else None

        kwargs = dict(
            layer_intersections=self.layer_intersections,
            report_layers=report_layers,
            map_extent=map_extent,
            map_crs=map_crs,
        )
        if fmt == "pdf":
            ok, result = ReportGenerator.generate_pdf(
                path, projet_info, site_info, self.decision_result,
                analysis_display, **kwargs
            )
        else:
            ok, result = ReportGenerator.generate_word(
                path, projet_info, site_info, self.decision_result,
                analysis_display, **kwargs
            )

        if ok:
            self.lbl_rapport.setText(f"Rapport enregistré : {os.path.basename(path)}")
        else:
            self.lbl_rapport.setText(f"Erreur : {result}")

    def _update_sidebar_title(self):
        
        try:
            dlg = self._dialog_ref
            if dlg and hasattr(dlg, 'list_projets') and hasattr(dlg, '_projets'):
                idx = dlg._projets.index(self)
                item = dlg.list_projets.item(idx)
                if item:
                    item.setText(self.get_titre())
        except (ValueError, AttributeError):
            pass

    def get_titre(self):
        pid  = self.edit_id.text().strip() or self.projet_id
        pnom = self.edit_nom.text().strip() or self.projet_nom
        return f"{pid} — {pnom}"


# =============================================================================
# DIALOGUE PRINCIPAL
# =============================================================================

class VecteurDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface    = iface
        self._projets = []
        self._compteur = 0

        self.setWindowTitle("GeoAssist — Aide à la décision")
        self.setMinimumSize(960, 660)
        self.resize(1000, 700)

        self._build_ui()
        self._apply_style()
        self._ajouter_projet()

    def _build_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        main.addWidget(self._build_title_bar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_content(), 1)

        main.addLayout(body, 1)
        main.addWidget(self._build_status_bar())

    def _build_title_bar(self):
        bar = QFrame(); bar.setObjectName("titleBar"); bar.setFixedHeight(44)
        lay = QHBoxLayout(bar); lay.setContentsMargins(14, 0, 14, 0)
        icon = QLabel("GA")
        icon.setFont(QFont("Arial", 11, QFont.Bold))
        icon.setObjectName("pluginIcon")
        icon.setFixedWidth(28)
        icon.setAlignment(Qt.AlignCenter)
        title = QLabel("GeoAssist Aménagement"); title.setObjectName("pluginTitle")
        title.setFont(QFont("Arial", 13, QFont.Bold))
        sub = QLabel("Analyse territoriale et aide à la décision"); sub.setObjectName("pluginSubtitle")
        lay.addWidget(icon); lay.addWidget(title); lay.addWidget(sub); lay.addStretch()
        
        return bar

    def _build_sidebar(self):
        sidebar = QFrame(); sidebar.setObjectName("sidebar"); sidebar.setFixedWidth(200)
        lay = QVBoxLayout(sidebar); lay.setContentsMargins(0, 10, 0, 10); lay.setSpacing(4)

        header = QHBoxLayout(); header.setContentsMargins(10, 0, 10, 0)
        lbl = QLabel("PROJETS"); lbl.setObjectName("sidebarSectionLabel")
        header.addWidget(lbl)
        header.addStretch()
        self.btn_add = QPushButton("+")
        self.btn_add.setObjectName("btnAdd")
        self.btn_add.setFixedSize(32, 32)
        self.btn_add.setToolTip("Ajouter un nouveau projet")
        self.btn_add.clicked.connect(self._ajouter_projet)
        header.addWidget(self.btn_add)
        lay.addLayout(header)
        lay.addSpacing(6)

        self.list_projets = QListWidget()
        self.list_projets.setObjectName("projetList")
        self.list_projets.currentRowChanged.connect(self._changer_projet)
        lay.addWidget(self.list_projets, 1)

        btn_del = QPushButton("Supprimer ce projet")
        btn_del.setObjectName("btnDelete")
        btn_del.clicked.connect(self._supprimer_projet)
        lay.addWidget(btn_del)
        return sidebar

    def _build_content(self):
        self.stack_projets = QStackedWidget()
        return self.stack_projets

    def _build_status_bar(self):
        bar = QFrame(); bar.setObjectName("statusBar"); bar.setFixedHeight(26)
        lay = QHBoxLayout(bar); lay.setContentsMargins(12, 0, 12, 0)
        self.status_label = QLabel("Prêt"); self.status_label.setObjectName("statusText")
        lay.addWidget(self.status_label); lay.addStretch()
        lay.addWidget(QLabel("CRS : WGS84"))
        return bar

    def _ajouter_projet(self):
        self._compteur += 1
        pid  = generate_unique_id()
        pnom = f"Nouveau projet {self._compteur}"

        widget = ProjetWidget(pid, pnom, self.iface, dialog_ref=self, parent=self)
        self._projets.append(widget)
        self.stack_projets.addWidget(widget)

        item = QListWidgetItem(widget.get_titre())
        self.list_projets.addItem(item)
        self.list_projets.setCurrentRow(self.list_projets.count() - 1)
        self.status_label.setText(f"Projet '{pid}' ajouté.")

    def _supprimer_projet(self):
        idx = self.list_projets.currentRow()
        if idx < 0 or len(self._projets) <= 1:
            QMessageBox.information(self, "Info", "Vous devez garder au moins un projet.")
            return
        rep = QMessageBox.question(
            self, "Supprimer",
            f"Supprimer le projet '{self._projets[idx].get_titre()}' ?",
            QMessageBox.Yes | QMessageBox.No
        )
        if rep != QMessageBox.Yes:
            return
        
        try:
            _USED_IDS.discard(self._projets[idx].edit_id.text())
        except Exception:
            pass
        widget = self._projets.pop(idx)
        self.stack_projets.removeWidget(widget)
        widget.deleteLater()
        self.list_projets.takeItem(idx)

    def _changer_projet(self, idx):
        if 0 <= idx < len(self._projets):
            self.stack_projets.setCurrentIndex(idx)

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog { background: #f7f7f7; color: #333333; }
            #titleBar { background: #ffffff; border-bottom: 1px solid #dddddd; }
            #pluginIcon {
                color: #333333; background: #eeeeee;
                border: 1px solid #cccccc; border-radius: 4px;
            }
            #pluginTitle { color: #222222; }
            #pluginSubtitle { color: #777777; font-size: 11px; }
            #versionTag {
                color: #666666; background: #eeeeee;
                border: 1px solid #dddddd; border-radius: 4px;
                padding: 2px 8px; font-size: 10px;
            }
            #sidebar { background: #ffffff; border-right: 1px solid #dddddd; }
            #sidebarSectionLabel {
                color: #888888; font-size: 10px; font-weight: bold; letter-spacing: 1px;
            }
            #projetList {
                background: transparent; border: none; color: #444444; font-size: 12px;
            }
            #projetList::item { padding: 8px 10px; border-radius: 2px; }
            #projetList::item:selected {
                background: #eeeeee; color: #222222;
                border-left: 3px solid #555555;
            }
            #projetList::item:hover { background: #f0f0f0; }
            QPushButton {
                background: #ffffff; color: #333333;
                border: 1px solid #bbbbbb; border-radius: 4px;
                padding: 6px 14px; font-size: 11px;
            }
            QPushButton:hover { background: #f0f0f0; }
            QPushButton:pressed { background: #e8e8e8; }
            QPushButton#btnAdd {
                background: #333333; color: #ffffff;
                border: 1px solid #333333; border-radius: 4px;
                padding: 0px; margin: 0px;
                min-width: 32px; max-width: 32px;
                min-height: 32px; max-height: 32px;
                font-size: 20px; font-weight: bold;
            }
            QPushButton#btnAdd:hover { background: #555555; border-color: #555555; }
            #btnDelete {
                background: #ffffff; color: #555555;
                border: 1px solid #cccccc; border-radius: 4px;
                padding: 5px; font-size: 11px; margin: 4px 8px;
            }
            #btnDelete:hover { background: #f0f0f0; }
            QGroupBox {
                font-weight: bold; font-size: 11px; color: #333333;
                border: 1px solid #dddddd; border-radius: 4px;
                margin-top: 8px; padding-top: 6px; background: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 10px; padding: 0 6px; background: transparent;
            }
            QPushButton#btnRunAnalysis {
                background: #333333; color: #ffffff;
                font-size: 12px; font-weight: bold; border: none;
            }
            QPushButton#btnRunAnalysis:hover { background: #555555; }
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
            QLineEdit, QComboBox, QDoubleSpinBox {
                border: 1px solid #cccccc; border-radius: 4px;
                padding: 5px 8px; font-size: 11px; background: #ffffff; color: #333333;
            }
            QLineEdit:focus, QComboBox:focus { border-color: #888888; }
            QLineEdit:read-only { background: #f5f5f5; color: #666666; }
            QTableWidget {
                border: 1px solid #dddddd; border-radius: 4px;
                font-size: 11px; gridline-color: #eeeeee; background: #ffffff;
            }
            QTableWidget::item:selected { background: #eeeeee; color: #222222; }
            QHeaderView::section {
                background: #f0f0f0; color: #333333;
                padding: 5px; font-size: 10px; border: none;
                border-bottom: 1px solid #dddddd; font-weight: bold;
            }
            #statusBar { background: #ffffff; border-top: 1px solid #dddddd; }
            #statusText { color: #777777; font-size: 10px; }
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { width: 6px; background: transparent; }
            QScrollBar::handle:vertical { background: #cccccc; border-radius: 3px; }
            #zoneStatut { font-size: 11px; padding: 4px; color: #555555; }
            #bufferStatut { font-size: 10px; padding: 2px; color: #666666; }
            QLabel { color: #444444; }
        """)


# =============================================================================
# CLASSE PRINCIPALE DU PLUGIN
# =============================================================================

class VecteurPlugin:

    def __init__(self, iface):
        self.iface  = iface
        self.dialog = None
        self.action = None

    def initGui(self):
        self.action = QAction(
            QIcon(), "Analyse vectorielle — Plan d'aménagement",
            self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("GeoDecision Aménagement", self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("GeoDecision Aménagement", self.action)
            self.iface.removeToolBarIcon(self.action)

    def run(self):
        if not self.dialog:
            self.dialog = VecteurDialog(self.iface, self.iface.mainWindow())
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
