"""
Reproject road network + raster cost surface to EPSG:7855 (GDA2020 / MGA Zone 55).

Run in the QGIS console. This is now required, not optional, for Stage 5's
budget search - mixing degree-scale lengths with metre-scale raster values
breaks the Lagrangian weighting.
"""

import processing

from qgis.core import QgsProject

ROAD_LAYER_NAME = "roads_clipped_to_raster"
RASTER_LAYER_NAME = "signals_cost_filled"
TARGET_CRS = "EPSG:7855"

ROAD_OUTPUT_NAME = "roads_7855"
RASTER_OUTPUT_NAME = "signals_cost_filled_7855"


road_layers = QgsProject.instance().mapLayersByName(ROAD_LAYER_NAME)
raster_layers = QgsProject.instance().mapLayersByName(RASTER_LAYER_NAME)

if not road_layers:
    raise RuntimeError(f"No layer named '{ROAD_LAYER_NAME}' found.")
if not raster_layers:
    raise RuntimeError(f"No layer named '{RASTER_LAYER_NAME}' found.")

road_layer_src = road_layers[0]
raster_layer_src = raster_layers[0]


# --- Reproject roads (vector) ---
road_result = processing.run(
    "native:reprojectlayer",
    {
        "INPUT": road_layer_src,
        "TARGET_CRS": TARGET_CRS,
        "OUTPUT": "memory:" + ROAD_OUTPUT_NAME,
    },
)
road_layer_7855 = road_result["OUTPUT"]
road_layer_7855.setName(ROAD_OUTPUT_NAME)
QgsProject.instance().addMapLayer(road_layer_7855)

print(f"Roads reprojected: '{ROAD_OUTPUT_NAME}' ({road_layer_7855.featureCount()} features, {TARGET_CRS})")


# --- Reproject raster ---
raster_result = processing.run(
    "gdal:warpreproject",
    {
        "INPUT": raster_layer_src,
        "SOURCE_CRS": raster_layer_src.crs(),
        "TARGET_CRS": TARGET_CRS,
        "RESAMPLING": 0,  # nearest neighbour - preserves raw values, no smoothing/blending across cells
        "OUTPUT": "TEMPORARY_OUTPUT",
    },
)
raster_layer_7855_path = raster_result["OUTPUT"]

from qgis.core import QgsRasterLayer
raster_layer_7855 = QgsRasterLayer(raster_layer_7855_path, RASTER_OUTPUT_NAME)

if not raster_layer_7855.isValid():
    raise RuntimeError("Reprojected raster failed to load.")

QgsProject.instance().addMapLayer(raster_layer_7855)

print(f"Raster reprojected: '{RASTER_OUTPUT_NAME}' ({TARGET_CRS})")
print(f"\nUpdate road_router.py to use:")
print(f"  ROAD_LAYER_NAME = '{ROAD_OUTPUT_NAME}'")
print(f"  raster layer name = '{RASTER_OUTPUT_NAME}'")