from qgis.core import QgsProject, QgsRasterLayer, QgsRasterBandStats
import processing
import math

project = QgsProject.instance()

# ---------- SETTINGS ----------
signals_name = "traffic_light_clip"
boundary_name = "BoundingBox"

ref_latitude = -37.8
metres_per_degree_lat = 111320

def metres_to_degrees(m):
    return m / metres_per_degree_lat

cell_size = metres_to_degrees(5)          # ~5m in degrees
search_radius = metres_to_degrees(500)    # ~250m in degrees

signals = project.mapLayersByName(signals_name)[0]
boundary = project.mapLayersByName(boundary_name)[0]

print(f"Cell size (deg): {cell_size:.8f}")
print(f"Search radius (deg): {search_radius:.8f}")

# ---------- 1. KERNEL DENSITY ESTIMATION ----------
kde_result = processing.run("qgis:heatmapkerneldensityestimation", {
    'INPUT': signals,
    'RADIUS': search_radius,
    'PIXEL_SIZE': cell_size,
    'KERNEL': 0,
    'OUTPUT': 'TEMPORARY_OUTPUT'
})['OUTPUT']

kde_layer = QgsRasterLayer(kde_result, "kde_check")
stats = kde_layer.dataProvider().bandStatistics(1, QgsRasterBandStats.All, kde_layer.extent(), 0)
print(f"KDE output range: min={stats.minimumValue}, max={stats.maximumValue}")
print(f"KDE extent: {kde_layer.extent().toString()}")

# ---------- 2. RECLASSIFY INTO 5 SCORE BANDS ----------
vmin, vmax = stats.minimumValue, stats.maximumValue
span = vmax - vmin

band_edges = [vmin, vmin + span*0.05, vmin + span*0.15, vmin + span*0.35, vmin + span*0.60, vmax]
scores = [50, 30, 15, 7, 3]

flat_table = []
for i in range(5):
    flat_table.extend([band_edges[i], band_edges[i+1], scores[i]])

signals_cost_result = processing.run("native:reclassifybytable", {
    'INPUT_RASTER': kde_result, 'RASTER_BAND': 1,
    'TABLE': flat_table,
    'NO_DATA': 0, 'RANGE_BOUNDARIES': 3, 'NODATA_FOR_MISSING': True,
    'DATA_TYPE': 5, 'OUTPUT': 'TEMPORARY_OUTPUT'
})['OUTPUT']

signals_cost_layer = QgsRasterLayer(signals_cost_result, "signals_cost_kde")
project.addMapLayer(signals_cost_layer)

final_stats = signals_cost_layer.dataProvider().bandStatistics(1, QgsRasterBandStats.All, signals_cost_layer.extent(), 0)
print("Signals cost (KDE-based) added. Min/Max:", final_stats.minimumValue, final_stats.maximumValue)
print("Final extent:", signals_cost_layer.extent().toString())

background_score = 0   # score for areas with no nearby signals

boundary = project.mapLayersByName(boundary_name)[0]
extent = boundary.extent()
extent_str = f"{extent.xMinimum()},{extent.xMaximum()},{extent.yMinimum()},{extent.yMaximum()} [{boundary.crs().authid()}]"

# ---------- 1. CREATE A CONSTANT BACKGROUND RASTER COVERING THE FULL BOUNDARY ----------
background = processing.run("native:createconstantrasterlayer", {
    'EXTENT': extent_str,
    'TARGET_CRS': boundary.crs(),
    'PIXEL_SIZE': cell_size,   # same cell_size (in degrees) used for the KDE
    'NUMBER': background_score,
    'OUTPUT_TYPE': 5,          # Float32
    'OUTPUT': 'TEMPORARY_OUTPUT'
})['OUTPUT']

background_layer = QgsRasterLayer(background, "background_check")
print("Background valid:", background_layer.isValid())
print("Background extent:", background_layer.extent().toString())

# ---------- 2. MERGE BACKGROUND + SIGNALS KDE COST (KDE values take priority where they exist) ----------
signals_cost_layer = project.mapLayersByName("signals_cost_kde")[0]

merged = processing.run("gdal:merge", {
    'INPUT': [background, signals_cost_layer.source()],  # order matters: last input wins on overlap
    'PCT': False,
    'SEPARATE': False,
    'NODATA_INPUT': 0,     # treat 0/NoData in the KDE layer as "no override" - background shows through
    'NODATA_OUTPUT': None,
    'OUTPUT': 'TEMPORARY_OUTPUT'
})['OUTPUT']

merged_layer = QgsRasterLayer(merged, "signals_cost_filled")
project.addMapLayer(merged_layer)

stats = merged_layer.dataProvider().bandStatistics(1, QgsRasterBandStats.All, merged_layer.extent(), 0)
print("Filled cost surface added. Min/Max:", stats.minimumValue, stats.maximumValue)
print("Final extent:", merged_layer.extent().toString())