"""Look up ECO_NAME and COLOR for the RESOLVE Ecoregions IDs present in
data/resolve_ecoregions_2017_cog.tif, using the canonical Ecoregions2017
shapefile published by https://ecoregions.appspot.com/.

Outputs JSON ready to paste into dataset_label_mapping.json under the
"resolve_ecoregions" key.
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

SHAPEFILE_URL = "https://storage.googleapis.com/teow2016/Ecoregions2017.zip"

ECO_IDS_IN_KENYA = [8, 9, 12, 25, 42, 43, 50, 51, 54, 55, 57, 61, 69, 76, 78, 79, 112]


def download_shapefile(dest_zip: Path) -> None:
    if dest_zip.exists() and dest_zip.stat().st_size > 100_000_000:
        print(f"Cached: {dest_zip} ({dest_zip.stat().st_size / 1e6:.1f} MB)")
        return
    print(f"Downloading {SHAPEFILE_URL} -> {dest_zip} ...")
    urllib.request.urlretrieve(SHAPEFILE_URL, dest_zip)
    print(f"Downloaded {dest_zip.stat().st_size / 1e6:.1f} MB")


def extract_shapefile(zip_path: Path, extract_dir: Path) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    shp = next(extract_dir.rglob("*.shp"))
    print(f"Shapefile: {shp}")
    return shp


def main() -> int:
    cache_dir = Path(tempfile.gettempdir()) / "resolve_ecoregions_2017"
    cache_dir.mkdir(exist_ok=True)
    zip_path = cache_dir / "Ecoregions2017.zip"
    extract_dir = cache_dir / "extracted"
    extract_dir.mkdir(exist_ok=True)

    download_shapefile(zip_path)
    shp_path = extract_shapefile(zip_path, extract_dir)

    try:
        import pyogrio
        df = pyogrio.read_dataframe(shp_path, read_geometry=False)
    except ImportError:
        import geopandas as gpd
        df = gpd.read_file(shp_path, ignore_geometry=True)

    print(f"Loaded {len(df)} ecoregion rows.")
    print(f"Columns: {list(df.columns)}")

    df["ECO_ID"] = df["ECO_ID"].astype(int)
    subset = df[df["ECO_ID"].isin(ECO_IDS_IN_KENYA)].drop_duplicates("ECO_ID").sort_values("ECO_ID")

    missing = sorted(set(ECO_IDS_IN_KENYA) - set(subset["ECO_ID"].tolist()))
    if missing:
        print(f"WARNING: missing ECO_IDs: {missing}", file=sys.stderr)

    classes = [{"value": 0, "name": "No Data", "color": "#000000"}]
    for _, row in subset.iterrows():
        classes.append({
            "value": int(row["ECO_ID"]),
            "name": str(row["ECO_NAME"]),
            "color": str(row["COLOR"]).upper(),
            "_biome": str(row["BIOME_NAME"]),
        })

    print("\n=== Pretty table ===")
    print(f"{'ECO_ID':>6}  {'COLOR':<8}  {'BIOME_NAME':<55}  ECO_NAME")
    for c in classes[1:]:
        print(f"{c['value']:>6}  {c['color']:<8}  {c['_biome']:<55}  {c['name']}")

    print("\n=== JSON snippet for dataset_label_mapping.json ===")
    out_classes = [{k: v for k, v in c.items() if not k.startswith("_")} for c in classes]
    block = {
        "resolve_ecoregions": {
            "name": "RESOLVE Ecoregions",
            "description": "RESOLVE Ecoregions 2017 — terrestrial ecoregions of the world (846 globally, 17 present in Kenya)",
            "source": "RESOLVE / Dinerstein et al. 2017 (BioScience)",
            "resolution": 250,
            "years_available": "2017",
            "classes": out_classes,
        }
    }
    print(json.dumps(block, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
