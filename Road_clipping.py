"""
Clip the road network to the raster cost surface's extent.

Run this in the QGIS Python console. Adjust ROAD_LAYER_NAME and
RASTER_LAYER_NAME if they differ from what's already in your project.
"""

import processing

from qgis.core import QgsProject

ROAD_LAYER_NAME = "TR_ROAD"     # <-- set this
RASTER_LAYER_NAME = "signals_cost_filled"
OUTPUT_LAYER_NAME = "roads_clipped_to_raster"


road_layers = QgsProject.instance().mapLayersByName(ROAD_LAYER_NAME)
raster_layers = QgsProject.instance().mapLayersByName(RASTER_LAYER_NAME)

if not road_layers:
    raise RuntimeError(f"No layer named '{ROAD_LAYER_NAME}' found.")

if not raster_layers:
    raise RuntimeError(f"No layer named '{RASTER_LAYER_NAME}' found.")

road_layer = road_layers[0]
raster_layer = raster_layers[0]

if road_layer.crs() != raster_layer.crs():
    raise ValueError(
        f"CRS mismatch: road layer is {road_layer.crs().authid()}, "
        f"raster layer is {raster_layer.crs().authid()}. "
        f"Reproject one to match before clipping."
    )

extent = raster_layer.extent()
extent_str = f"{extent.xMinimum()},{extent.xMaximum()},{extent.yMinimum()},{extent.yMaximum()} [{road_layer.crs().authid()}]"

print("Clipping to extent:", extent_str)

result = processing.run(
    "native:extractbyextent",
    {
        "INPUT": road_layer,
        "EXTENT": extent_str,
        "CLIP": True,   # actually clip road geometries at the boundary,
                        # not just select features that intersect it
        "OUTPUT": "memory:" + OUTPUT_LAYER_NAME,
    },
)

clipped_layer = result["OUTPUT"]
clipped_layer.setName(OUTPUT_LAYER_NAME)

QgsProject.instance().addMapLayer(clipped_layer)

print(f"Done. '{OUTPUT_LAYER_NAME}' added with {clipped_layer.featureCount()} features "
      f"(original had {road_layer.featureCount()}).")
print(f"Set ROAD_LAYER_NAME = '{OUTPUT_LAYER_NAME}' in road_router.py to use this from now on.")