# Dataset

## 1 Overview

This work draws on six land cover products. Five of them are existing global
datasets that are used as independent input sources for a consensus-labeling
step: **Google Dynamic World**, **ESRI Land Use / Land Cover (LULC)**,
**ESA WorldCover**, the **GLAD Global Land Cover and Land Use (GLCLU)**
dataset, and **GLC-FCS30D**. The sixth product, **Sunstone Kenya LULC**, is
the output produced in this work. It is derived by combining the five global
products through a unanimous-agreement filter, using AlphaEarth foundation
model embeddings as input features to a deep-learning-based classification
pipeline.

## 2 Summary of datasets

| Dataset | Resolution | Temporal coverage | Sensor basis | Reference |
|---|---|---|---|---|
| Sunstone Kenya LULC | 10 m | 2017–2025 | AlphaEarth Embeddings | This work |
| Google Dynamic World | 10 m | 2015–present | Sentinel-2 | Brown et al., 2022 |
| ESRI LULC | 10 m | 2017–2024 | Sentinel-2 | Karra et al., 2021 |
| ESA WorldCover | 10 m | 2020–2021 | Sentinel-1/2 | Zanaga et al., 2022 |
| GLAD GLCLU | 30 m | 2000–2020 | Landsat | Potapov et al., 2022 |
| GLC-FCS30D | 30 m | 1985–2022 | Landsat | Zhang et al., 2024 |

## 3 Per-dataset descriptions

**Sunstone Kenya LULC** (this work) is a 10 m annual land cover product
covering Kenya from 2017 to 2025. It uses AlphaEarth foundation model
embeddings as pixel-level features and is trained on a consensus label set
drawn from the five global products listed below. Its class taxonomy comprises
ten harmonized classes: Trees, Cropland, Water, Built Area, Bare Ground,
Flooded Vegetation, Mangroves, Shrubland, Grassland, and Snow/Ice.

**Google Dynamic World** (Brown et al., 2022) is a near-real-time, 10 m
global land cover product derived from Sentinel-2 imagery using a convolutional
neural network. It uses a nine-class taxonomy (water, trees, grass, flooded
vegetation, crops, shrub and scrub, built area, bare ground, snow/ice) and is
updated with each new Sentinel-2 acquisition.

**ESRI LULC** (Karra et al., 2021) provides annual 10 m land cover maps using
a convolutional neural network trained on billions of labeled Sentinel-2
pixels. It uses a nine-class taxonomy in which grassland, shrubland, and
sparse vegetation are collapsed into a single "Rangeland" class, which limits
its utility for distinguishing semi-arid vegetation types.

**ESA WorldCover** (Zanaga et al., 2022) fuses Sentinel-1 synthetic aperture
radar and Sentinel-2 optical data at 10 m resolution. Its twelve-class
taxonomy separates mangroves, herbaceous wetland, and moss/lichen from other
vegetation types, offering finer thematic detail than the other 10 m
products. Maps are available for 2020 and 2021.

**GLAD GLCLU** (Potapov et al., 2022) is a 30 m Landsat-based product with a
highly detailed taxonomy of 113 classes organized by vegetation structural
type and canopy height. It is produced using bagged decision tree ensembles
trained on manually collected reference data, applied in a three-stage
regional hierarchy. For this work, the 113 classes are aggregated into the
ten harmonized categories.

**GLC-FCS30D** (Zhang et al., 2024) is a 30 m Landsat-based product spanning
1985–2022 at roughly five-year intervals. It combines the Continuous Change
Detection (CCD) algorithm for time-series change detection with a
local-adaptive Random Forest classifier applied within 5° × 5° geographic
tiles. Its taxonomy distinguishes forest types, shrubland, grassland,
wetland, and mangrove as separate categories.
