"""
Clip the traffic signals to the raster cost surface's extent.

Run this in the QGIS Python console. Adjust TRAFFIC_LAYER_NAME and
BOUNDARY_LAYER_NAME if they differ from what's already in your project.
"""

import processing
from qgis.core import QgsProject

TRAFFIC_LAYER_NAME = "traffic_light"
BOUNDARY_LAYER_NAME = "BoundingBox"
OUTPUT_LAYER_NAME = "traffic_light_clip"

project = QgsProject.instance()

traffic = project.mapLayersByName(TRAFFIC_LAYER_NAME)[0]
boundary = project.mapLayersByName(BOUNDARY_LAYER_NAME)[0]

if traffic.crs() != boundary.crs():
    raise ValueError(
        f"CRS mismatch: {traffic.crs().authid()} vs {boundary.crs().authid()}"
    )

result = processing.run(
    "native:clip",
    {
        "INPUT": traffic,
        "OVERLAY": boundary,
        "OUTPUT": "memory:" + OUTPUT_LAYER_NAME,
    },
)

clipped = result["OUTPUT"]
clipped.setName(OUTPUT_LAYER_NAME)

project.addMapLayer(clipped)

print(
    f"Done. '{OUTPUT_LAYER_NAME}' contains "
    f"{clipped.featureCount()} of {traffic.featureCount()} traffic lights."
)