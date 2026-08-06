# Inconvenient-Cost-Path
A constrained shortest-path routing, inverted: instead of minimizing cost, this maximizes a custom "inconvenience score" (turn penalties, road-hierarchy transitions, proximity to traffic signals) within a bounded detour budget. Built on QGIS's graph engine with a hand-rolled Lagrangian relaxation solver. Entirely impractical, technically sound.

Made this with the intention of creating a really irritating route to get my partner to drive to our favourite coffee shop.

Requirements
QGIS 3.34 (LTR) or similar, with the Python console
A road network layer with VicMap-style attributes (specifically a CLASS_CODE field — see Road classification below)
A point layer of traffic signal locations
A polygon layer defining your study area boundary

Pipeline

Run these four scripts in order, in the QGIS Python console. Each one depends on layers the previous script created.

#1 Traffic_Clipping.py 

Clips your traffic signal points down to the study area boundary.

Setting	Meaning
TRAFFIC_LAYER_NAME	your raw traffic signal point layer
BOUNDARY_LAYER_NAME	your study area polygon

#2 TraficLightScore.py

Builds the cost surface: a kernel density estimate of signal proximity, reclassified into 5 score bands (closer to signals = higher score), then filled out to the full study area with a flat background value so there are no gaps.

Setting	Meaning
signals_name	should match the output of step 1
boundary_name	your study area polygon
cell_size / search_radius	in metres, converted to degrees internally

#3 Road_clipping.py

Clips your full road network down to the cost surface's extent, so routing never wanders into areas with no cost data.

Setting	Meaning
ROAD_LAYER_NAME	your raw road network layer
RASTER_LAYER_NAME	output of step 2

#4 7855_Reprojection.py

Reprojects both the clipped roads and the cost raster to a metres-based CRS (EPSG:7855, GDA2020 / MGA Zone 55). Required, not optional: distance and raster score get combined directly in the routing cost function, and mixing degree-scale lengths with metre-scale raster values breaks the search.

Setting	Meaning
ROAD_LAYER_NAME	output of step 3
RASTER_LAYER_NAME	output of step 2
OUTPUT_FOLDER	where the reprojected files get written - real files

#5  WCP_V2.py

The actual router. Builds the network graph, then computes and draws three routes for comparison:

Output layer	What it is
route_min_cost	turn-aware minimum-cost route (sane, signal-avoiding reference)
route_shortest_distance	the true shortest path, ignoring score entirely
route_worst_raster_only	worst-cost using signal-proximity score alone, no turn/road-type bonuses
worst_cost_route	the full worst-cost route: signal score + right-turn bonus + auxiliary→main road bonus

Configuration:
START_LL = (144.825301, -37.719809)   # currently hardcoded - see note below
END_LL = (144.904959, -37.674658)

BUDGET_MULTIPLIER = 1.4       # max detour = this x shortest-path distance
RIGHT_TURN_BONUS = 50.0
AUX_TO_MAIN_BONUS = 100.0
DEFAULT_TURN_COSTS = {"right": 0, "straight": 20, "left": 10, "u_turn": 5}

START_LL / END_LL are currently fixed coordinates. These are slated to become user-supplied input (e.g. via a QGIS point tool click, or a simple prompt) rather than hardcoded lat/lon — until then, edit these two lines directly for each route you want to test.

Classification of roads:
Using Vic Map data 9https://www.land.vic.gov.au/maps-and-spatial/spatial-data/vicmap-catalogue), the roads have been found from CLASS_CODE field:

Code	Class	Bucket
0	Freeway	main
1	Highway	main
2	Arterial	main
3	Sub-arterial	main
4	Collector	auxiliary
5	Access Major	auxiliary
6	Access Minor	auxiliary
7–9, 13, 14	Tracks / paper roads / ferries	excluded from routing entirely


Funny Exampels: https://github.com/Sam-Dabo/Inconvenient-Cost-Path/blob/4018910a5f9ae05f7ec5935fe8cf17ddf985e5f6/Zoom.png
