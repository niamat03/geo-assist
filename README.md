# GeoAssist

Plugin QGIS d'analyse géospatiale intégrée pour l'aide à la décision en aménagement du territoire, combinant analyse vectorielle et télédétection satellitaire dans une interface unique (deux onglets).

## Modules

### GeoAssist Aménagement (onglet "Dérogation")

Module d'analyse vectorielle multi-projets dédié à l'évaluation de projets de dérogation. Il permet de définir une zone d'étude, de calculer des intersections avec des couches de référence, d'évaluer l'éligibilité d'un projet via un moteur de décision multicritère, puis de générer automatiquement un rapport (PDF / Word).

> Les données, sources et critères précis utilisés par ce module sont confidentiels et ne sont pas détaillés publiquement dans ce dépôt.

### GeoAssist Sentinel (onglet "Télédétection")

Module de télédétection pour le téléchargement, la préparation et l'analyse d'images Sentinel-1 et Sentinel-2 :

- Recherche et téléchargement de produits via le [Copernicus Data Space Ecosystem (CDSE)](https://dataspace.copernicus.eu/) (authentification par email/mot de passe saisis dans l'interface, jamais stockés).
- Définition de zone d'intérêt (AOI) par géocodage (Nominatim), saisie manuelle de bbox, ou dessin interactif sur la carte.
- Extraction et composition multibande à partir de produits `.SAFE` (Sentinel-2 : R10m/R20m ; Sentinel-1 : VV/VH reprojeté).
- Calcul d'indices thématiques : NDVI, NDWI, NDBI, ratio VV/VH, RVI, etc.
- Historique des opérations (téléchargement / extraction / calcul d'indice) et chargement automatique des résultats dans QGIS.

## Architecture

```
geo_assist/
├── Geo_Assist.py              # Point d'entrée du plugin (classFactory, initGui, run)
├── Geo_Assist_dialog.py       # Fenêtre principale (QTabWidget : Dérogation / Télédétection)
├── metadata.txt                # Métadonnées du plugin QGIS
├── modules/
│   ├── derogation/vecteur.py   # Module d'analyse vectorielle multi-projets
│   └── sentinel/sentinel_dialog.py  # Module de télédétection Sentinel-1/2
├── i18n/                        # Fichiers de traduction
├── test/                        # Tests unitaires (unittest, environnement QGIS)
└── help/                        # Documentation Sphinx
```

L'interface est construite entièrement en PyQt5 (pas de fichier `.ui`). Chaque module est un widget autonome intégré comme onglet de la fenêtre principale.

## Technologies

Python · PyQt5 · PyQGIS · GDAL · NumPy · ReportLab · python-docx

## Installation

1. Copier le dossier `geo_assist` dans le répertoire des extensions QGIS.
2. Activer le plugin depuis le Gestionnaire des extensions.
3. Redémarrer QGIS si nécessaire.

## Configuration

Le plugin ne nécessite aucune variable d'environnement pour fonctionner (voir [.env.example](.env.example)) : les identifiants Copernicus (module Sentinel) sont saisis directement dans l'interface et ne sont jamais persistés.

## Développement

- El Qasemy Niamat

**Encadrement :** Pr. YAZIDI ALAOUI Otmane
