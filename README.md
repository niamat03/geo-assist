# GeoAssist

Plugin QGIS d'analyse géospatiale intégrée pour l'aide à la décision en aménagement du territoire, combinant analyse vectorielle et télédétection satellitaire dans une interface unique (deux onglets).

## Sommaire

- [Modules](#modules)
- [Architecture du dépôt](#architecture-du-dépôt)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [Utilisation](#utilisation)
- [Configuration](#configuration)
- [Tests](#tests)
- [Internationalisation](#internationalisation)
- [Technologies](#technologies)
- [Développement](#développement)

## Modules

### GeoAssist Aménagement (onglet "Dérogation")

Module d'analyse vectorielle multi-projets dédié à l'évaluation de projets de dérogation. Il permet de :

- gérer plusieurs projets en parallèle (un onglet latéral par projet) ;
- définir une zone d'étude (saisie de coordonnées, clic sur la carte, ou import d'une couche existante) et générer automatiquement un centroïde et une zone tampon ;
- charger les couches de référence nécessaires à l'analyse (depuis le projet QGIS actif ou par import de fichier) ;
- calculer les surfaces et pourcentages d'intersection entre la zone d'étude et ces couches ;
- évaluer l'éligibilité du projet via un moteur de décision multicritère ;
- générer automatiquement un rapport de synthèse (PDF via ReportLab, ou Word via python-docx), incluant carte, légende et tableaux de résultats.

> Les données, sources et critères précis utilisés par ce module sont confidentiels et ne sont pas détaillés publiquement dans ce dépôt.

### GeoAssist Sentinel (onglet "Télédétection")

Module de télédétection pour le téléchargement, la préparation et l'analyse d'images Sentinel-1 et Sentinel-2 :

- **Recherche et téléchargement** de produits via le [Copernicus Data Space Ecosystem (CDSE)](https://dataspace.copernicus.eu/) (authentification par email/mot de passe saisis dans l'interface, jamais stockés sur disque) ; file d'attente de téléchargement ("panier") et cache local.
- **Définition de zone d'intérêt (AOI)** par géocodage (Nominatim/OpenStreetMap), saisie manuelle de bbox, ou dessin interactif d'un rectangle sur la carte intégrée.
- **Extraction et composition multibande** à partir de produits `.SAFE` : Sentinel-2 (bandes 10 m / 20 m / multibande complet) et Sentinel-1 (VV/VH, reprojection automatique en EPSG:4326).
- **Calcul d'indices thématiques** : NDVI, NDWI, NDBI, ratio VV/VH, RVI, etc. — traitement par blocs pour maîtriser la consommation mémoire sur les grandes images.
- **Historique** de toutes les opérations (téléchargement / extraction / calcul d'indice) et chargement automatique des résultats dans le projet QGIS.

## Architecture du dépôt

```
geo_assist/
├── Geo_Assist.py                    # Point d'entrée du plugin (classFactory, initGui, run)
├── Geo_Assist_dialog.py             # Fenêtre principale (QTabWidget : Dérogation / Télédétection)
├── metadata.txt                     # Métadonnées du plugin QGIS
├── resources.qrc / resources.py     # Ressources Qt (icônes)
├── modules/
│   ├── derogation/vecteur.py        # Module d'analyse vectorielle multi-projets
│   └── sentinel/sentinel_dialog.py  # Module de télédétection Sentinel-1/2
├── i18n/                            # Fichiers de traduction (.ts)
├── test/                            # Tests unitaires (unittest, environnement QGIS)
└── help/                            # Documentation Sphinx (source)
```

L'interface est construite entièrement en PyQt5 (pas de fichier `.ui` pour les modules) : chaque module est un widget autonome intégré comme onglet de la fenêtre principale `GeoAssistDialog`.

## Prérequis

- QGIS ≥ 3.0 (PyQGIS, PyQt5, GDAL fournis avec QGIS)
- Python 3
- [ReportLab](https://pypi.org/project/reportlab/) (génération PDF, optionnel — dégradation gracieuse si absent)
- [python-docx](https://pypi.org/project/python-docx/) (génération Word, optionnel — dégradation gracieuse si absent)
- NumPy (fourni avec l'environnement QGIS)

## Installation

1. Copier le dossier `geo_assist` dans le répertoire des extensions QGIS (`~/.local/share/QGIS/QGIS3/profiles/default/python/plugins` sous Linux, `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins` sous Windows).
2. Installer les dépendances Python optionnelles dans l'environnement Python de QGIS si besoin (`pip install reportlab python-docx`).
3. Activer le plugin depuis le Gestionnaire des extensions de QGIS.
4. Redémarrer QGIS si nécessaire.

## Utilisation

1. Lancer GeoAssist depuis la barre d'outils ou le menu **Extensions**.
2. Onglet **Dérogation** : créer un projet, définir la zone d'étude, charger les couches de référence, lancer l'analyse, puis exporter le rapport.
3. Onglet **Télédétection** : se connecter au Copernicus Data Space Ecosystem, définir l'AOI, rechercher et télécharger les produits Sentinel voulus, puis extraire les bandes et calculer les indices sur l'image obtenue.

## Configuration

Le plugin ne nécessite aucune variable d'environnement pour fonctionner (voir [.env.example](.env.example)) : les identifiants Copernicus (module Sentinel) sont saisis directement dans l'interface et ne sont jamais persistés sur disque. L'historique des opérations du module Sentinel est stocké localement dans `geo_assist/sentinelvision_history.json` (fichier local, non versionné).

## Tests

Le dossier `test/` contient des tests unitaires basés sur `unittest`, à exécuter dans un environnement disposant de PyQGIS (voir `test/qgis_interface.py` pour le mock d'interface QGIS utilisé en dehors de l'application).

## Internationalisation

Les chaînes de l'interface passent par `QCoreApplication.translate` ; les fichiers de traduction (`.ts`) se trouvent dans `i18n/` et peuvent être mis à jour avec les scripts `scripts/update-strings.sh` et `scripts/compile-strings.sh`.

## Technologies

Python · PyQt5 · PyQGIS · GDAL · NumPy · ReportLab · python-docx

## Développement

- El Qasemy Niamat
