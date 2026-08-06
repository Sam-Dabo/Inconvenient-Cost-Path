"""
Worst Cost Path
================
A routing engine that finds the most annoying legal route between two
points: prefers right turns, rewards climbing onto bigger roads, and
stays close to traffic signals, all within 1.4x the distance of the
actual shortest path. Built on QGIS's graph engine with a hand-rolled
Dijkstra and a Lagrangian relaxation solver for the budget constraint.

Run the whole thing in the QGIS Python console.
"""

import math
import heapq

from qgis.analysis import (
    QgsVectorLayerDirector,
    QgsNetworkDistanceStrategy,
    QgsGraphBuilder,
)

from qgis.core import (
    Qgis,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsSpatialIndex,
)

# The raster identify-format enum moved between QGIS versions.
try:
    _IDENTIFY_FORMAT_VALUE = Qgis.RasterIdentifyFormat.Value
except AttributeError:
    from qgis.core import QgsRaster
    _IDENTIFY_FORMAT_VALUE = QgsRaster.IdentifyFormatValue


# ============================================================
# Config
# ============================================================

ROAD_LAYER_NAME = "roads_7855"
RASTER_LAYER_NAME = "signals_cost_filled_7855"

# VicMap CLASS_CODE: 0-3 are the main network (freeway -> sub-arterial),
# 4-6 feed into it (collector, access major/minor). 7+ (tracks, ferries,
# paper roads) don't count either way.
ROAD_TYPE_FIELD = "CLASS_CODE"
AUX_ROAD_VALUES = {4, 5, 6}
MAIN_ROAD_VALUES = {0, 1, 2, 3}

# Only these classes are actually drivable roads. 7-9 (tracks), 13 (paper
# road - not a real feature), 14 (ferry) get excluded from the routing
# graph entirely, not just from the bonus scoring.
ROUTABLE_CLASS_CODES = AUX_ROAD_VALUES | MAIN_ROAD_VALUES

DEFAULT_TURN_COSTS = {"right": 0, "straight": 20, "left": 10, "u_turn": 5}

RIGHT_TURN_BONUS = 50.0
AUX_TO_MAIN_BONUS = 100.0

BUDGET_MULTIPLIER = 1.4

START_LL = (144.825301, -37.719809)
END_LL = (144.904959, -37.674658)


# ============================================================
# Graph building
# ============================================================

def transform_point(lon, lat, source_epsg, target_epsg):
    source = QgsCoordinateReferenceSystem(source_epsg)
    target = QgsCoordinateReferenceSystem(target_epsg)
    transform = QgsCoordinateTransform(source, target, QgsProject.instance())
    return transform.transform(QgsPointXY(lon, lat))


def build_graph(road_layer, start_point, end_point):
    director = QgsVectorLayerDirector(
        road_layer, -1, "", "", "", QgsVectorLayerDirector.DirectionBoth
    )
    director.addStrategy(QgsNetworkDistanceStrategy())

    builder = QgsGraphBuilder(road_layer.crs(), False, 0.0)
    tied_points = director.makeGraph(builder, [start_point, end_point])

    return builder.graph(), tied_points


def find_vertex(graph, point):
    """Nearest graph vertex to a point - used to snap start/end onto the network."""
    closest, min_dist = None, float("inf")
    for i in range(graph.vertexCount()):
        dist = graph.vertex(i).point().distance(point)
        if dist < min_dist:
            min_dist, closest = dist, i
    return closest


# ============================================================
# Raster cost surface
# ============================================================

def sample_raster(point, raster_layer, band=1, failed_points=None):
    """Sample one raster value at a point. None if outside extent / nodata."""
    result = raster_layer.dataProvider().identify(point, _IDENTIFY_FORMAT_VALUE)

    value = result.results().get(band) if result.isValid() else None

    if value is None:
        if failed_points is not None:
            failed_points.append(point)
        return None

    return float(value)


def _interpolate(p1, p2, t):
    return QgsPointXY(p1.x() + (p2.x() - p1.x()) * t, p1.y() + (p2.y() - p1.y()) * t)


def edge_raster_cost(graph, edge, edge_id, raster_layer, cache, samples_per_edge=3, failed_points=None):
    """Length x average raster value along the edge. Cached per edge_id."""
    if edge_id in cache:
        return cache[edge_id]

    p1 = graph.vertex(edge.fromVertex()).point()
    p2 = graph.vertex(edge.toVertex()).point()
    length = p1.distance(p2)

    values = []
    for i in range(samples_per_edge):
        t = i / (samples_per_edge - 1) if samples_per_edge > 1 else 0.0
        value = sample_raster(_interpolate(p1, p2, t), raster_layer, failed_points=failed_points)
        if value is not None:
            values.append(value)

    # No coverage here - fall back to plain length rather than breaking the search.
    cost = length * (sum(values) / len(values)) if values else length
    cache[edge_id] = cost

    return cost


def edge_cost(edge_id, graph, raster_layer, cache, failed_points=None):
    edge = graph.edge(edge_id)
    return edge_raster_cost(graph, edge, edge_id, raster_layer, cache, failed_points=failed_points)


def assert_matching_crs(road_layer, raster_layer):
    """Hard stop on CRS mismatch rather than silently reprojecting."""
    road_crs, raster_crs = road_layer.crs().authid(), raster_layer.crs().authid()
    if road_crs != raster_crs:
        raise ValueError(f"CRS mismatch: roads are {road_crs}, raster is {raster_crs}.")
    print(f"CRS check OK: both layers in {road_crs}")


# ============================================================
# Turns
# ============================================================

def bearing(p1, p2):
    """Compass bearing in degrees, 0 = north."""
    return math.degrees(math.atan2(p2.x() - p1.x(), p2.y() - p1.y())) % 360


def turn_angle(bearing_in, bearing_out):
    """Signed turn angle in (-180, 180]. Positive = right, ~180 = U-turn."""
    return (bearing_out - bearing_in + 180) % 360 - 180


def classify_turn(angle, straight_tolerance=20.0, u_turn_tolerance=20.0):
    abs_angle = abs(angle)
    if abs_angle <= straight_tolerance:
        return "straight"
    if abs_angle >= 180 - u_turn_tolerance:
        return "u_turn"
    return "right" if angle > 0 else "left"


def turn_cost(prev_edge_id, next_edge_id, graph, turn_costs=None):
    if prev_edge_id is None:
        return 0

    turn_costs = turn_costs or DEFAULT_TURN_COSTS
    prev_edge, next_edge = graph.edge(prev_edge_id), graph.edge(next_edge_id)

    angle = turn_angle(
        bearing(graph.vertex(prev_edge.fromVertex()).point(), graph.vertex(prev_edge.toVertex()).point()),
        bearing(graph.vertex(next_edge.fromVertex()).point(), graph.vertex(next_edge.toVertex()).point()),
    )
    return turn_costs.get(classify_turn(angle), 0)


def dijkstra_turn_aware(graph, start_vertex, end_vertex, raster_layer, cache=None, turn_costs=None, failed_points=None):
    """State is (vertex, incoming_edge) since turn cost depends on how you arrived."""
    cache = cache if cache is not None else {}
    start_state = (start_vertex, None)

    queue = [(0, start_state)]
    distances = {start_state: 0}
    parents = {}
    visited = set()
    goal_state = None

    while queue:
        current_cost, current_state = heapq.heappop(queue)
        if current_state in visited:
            continue
        visited.add(current_state)

        current_vertex, incoming_edge = current_state
        if current_vertex == end_vertex:
            goal_state = current_state
            break

        for edge_id in graph.vertex(current_vertex).outgoingEdges():
            edge = graph.edge(edge_id)

            # Don't immediately backtrack down the edge just arrived on.
            if incoming_edge is not None:
                prev_edge = graph.edge(incoming_edge)
                if edge_id == incoming_edge or (
                    edge.fromVertex() == prev_edge.toVertex() and edge.toVertex() == prev_edge.fromVertex()
                ):
                    continue

            new_cost = (
                current_cost
                + edge_cost(edge_id, graph, raster_layer, cache, failed_points=failed_points)
                + turn_cost(incoming_edge, edge_id, graph, turn_costs)
            )

            next_state = (edge.toVertex(), edge_id)
            if next_state not in distances or new_cost < distances[next_state]:
                distances[next_state] = new_cost
                parents[next_state] = (current_state, edge_id)
                heapq.heappush(queue, (new_cost, next_state))

    if goal_state is None:
        raise RuntimeError("No route found.")

    path, current = [], goal_state
    while current != start_state:
        current, edge_id = parents[current]
        path.append(edge_id)
    path.reverse()

    return path, cache


def summarize_turns(path_edges, graph):
    counts = {"right": 0, "straight": 0, "left": 0, "u_turn": 0}
    for i in range(1, len(path_edges)):
        prev_edge, next_edge = graph.edge(path_edges[i - 1]), graph.edge(path_edges[i])
        angle = turn_angle(
            bearing(graph.vertex(prev_edge.fromVertex()).point(), graph.vertex(prev_edge.toVertex()).point()),
            bearing(graph.vertex(next_edge.fromVertex()).point(), graph.vertex(next_edge.toVertex()).point()),
        )
        counts[classify_turn(angle)] += 1
    return counts


# ============================================================
# Budget-constrained score maximization
# ============================================================
# Goal: maximize total score (raster + turn/road-type bonuses) along the
# route, subject to distance <= BUDGET_MULTIPLIER x shortest path.
#
# There's no direct way to maximize score under a distance cap with plain
# Dijkstra, so we search over a trade-off weight lambda:
#   edge_weight = length - lambda * (score + bonus)
# Higher lambda favors higher-scoring routes at the cost of distance.
# Binary search finds the lambda whose path best fills the budget.

def edge_length(graph, edge):
    return graph.vertex(edge.fromVertex()).point().distance(graph.vertex(edge.toVertex()).point())


def plain_shortest_distance(graph, start_vertex, end_vertex):
    """Plain Dijkstra on length only - gives the baseline for the budget."""
    queue = [(0.0, start_vertex)]
    distances = {start_vertex: 0.0}
    parents = {}
    visited = set()

    while queue:
        current_cost, current_vertex = heapq.heappop(queue)
        if current_vertex in visited:
            continue
        visited.add(current_vertex)
        if current_vertex == end_vertex:
            break

        for edge_id in graph.vertex(current_vertex).outgoingEdges():
            edge = graph.edge(edge_id)
            new_cost = current_cost + edge_length(graph, edge)
            next_vertex = edge.toVertex()
            if next_vertex not in distances or new_cost < distances[next_vertex]:
                distances[next_vertex] = new_cost
                parents[next_vertex] = (current_vertex, edge_id)
                heapq.heappush(queue, (new_cost, next_vertex))

    if end_vertex not in distances:
        raise RuntimeError("No path found for the baseline shortest-distance run.")

    path, current = [], end_vertex
    while current != start_vertex:
        current, edge_id = parents[current]
        path.append(edge_id)
    path.reverse()

    return path, distances[end_vertex]


# ---- Road type + turn/type bonus -----------------------------------------

def build_edge_road_type_cache(graph, road_layer, road_type_field):
    """Maps each edge to its nearest road feature's type. One-time pass, cache it."""
    spatial_index = QgsSpatialIndex(road_layer.getFeatures())
    cache = {}

    for edge_id in range(graph.edgeCount()):
        edge = graph.edge(edge_id)
        p1 = graph.vertex(edge.fromVertex()).point()
        p2 = graph.vertex(edge.toVertex()).point()
        midpoint = QgsPointXY((p1.x() + p2.x()) / 2, (p1.y() + p2.y()) / 2)

        nearest = spatial_index.nearestNeighbor(midpoint, 1)
        cache[edge_id] = road_layer.getFeature(nearest[0]).attribute(road_type_field) if nearest else None

    return cache


def classify_road_type(value, aux_values, main_values):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "other"
    if value in aux_values:
        return "auxiliary"
    if value in main_values:
        return "main"
    return "other"


def transition_bonus(prev_edge_id, next_edge_id, graph, edge_road_type_cache, right_turn_bonus=RIGHT_TURN_BONUS, aux_to_main_bonus=AUX_TO_MAIN_BONUS):
    """Bonus for right turns, plus for climbing from an auxiliary road onto a main one."""
    if prev_edge_id is None:
        return 0.0

    prev_edge, next_edge = graph.edge(prev_edge_id), graph.edge(next_edge_id)
    bonus = 0.0

    angle = turn_angle(
        bearing(graph.vertex(prev_edge.fromVertex()).point(), graph.vertex(prev_edge.toVertex()).point()),
        bearing(graph.vertex(next_edge.fromVertex()).point(), graph.vertex(next_edge.toVertex()).point()),
    )
    if classify_turn(angle) == "right":
        bonus += right_turn_bonus

    prev_type = classify_road_type(edge_road_type_cache.get(prev_edge_id), AUX_ROAD_VALUES, MAIN_ROAD_VALUES)
    next_type = classify_road_type(edge_road_type_cache.get(next_edge_id), AUX_ROAD_VALUES, MAIN_ROAD_VALUES)
    if prev_type == "auxiliary" and next_type == "main":
        bonus += aux_to_main_bonus

    return bonus


def path_length_and_score(graph, path_edges, raster_layer, score_cache, edge_road_type_cache, right_turn_bonus=RIGHT_TURN_BONUS, aux_to_main_bonus=AUX_TO_MAIN_BONUS):
    total_length, total_score, prev_edge_id = 0.0, 0.0, None

    for edge_id in path_edges:
        edge = graph.edge(edge_id)
        total_length += edge_length(graph, edge)
        total_score += edge_raster_cost(graph, edge, edge_id, raster_layer, score_cache)
        total_score += transition_bonus(prev_edge_id, edge_id, graph, edge_road_type_cache, right_turn_bonus, aux_to_main_bonus)
        prev_edge_id = edge_id

    return total_length, total_score


def lagrangian_weighted_dijkstra(graph, start_vertex, end_vertex, raster_layer, score_cache, edge_road_type_cache, lam, right_turn_bonus=RIGHT_TURN_BONUS, aux_to_main_bonus=AUX_TO_MAIN_BONUS):
    """Single-lambda search, state (vertex, incoming_edge) so bonuses see the transition."""
    start_state = (start_vertex, None)
    queue = [(0.0, start_state)]
    distances = {start_state: 0.0}
    parents = {}
    visited = set()
    goal_state = None

    while queue:
        current_cost, current_state = heapq.heappop(queue)
        if current_state in visited:
            continue
        visited.add(current_state)

        current_vertex, incoming_edge = current_state
        if current_vertex == end_vertex:
            goal_state = current_state
            break

        for edge_id in graph.vertex(current_vertex).outgoingEdges():
            edge = graph.edge(edge_id)

            if incoming_edge is not None:
                prev_edge = graph.edge(incoming_edge)
                if edge_id == incoming_edge or (
                    edge.fromVertex() == prev_edge.toVertex() and edge.toVertex() == prev_edge.fromVertex()
                ):
                    continue

            score = edge_raster_cost(graph, edge, edge_id, raster_layer, score_cache)
            bonus = transition_bonus(incoming_edge, edge_id, graph, edge_road_type_cache, right_turn_bonus, aux_to_main_bonus)
            weight = max(0.0, edge_length(graph, edge) - lam * (score + bonus))

            next_state = (edge.toVertex(), edge_id)
            new_cost = current_cost + weight
            if next_state not in distances or new_cost < distances[next_state]:
                distances[next_state] = new_cost
                parents[next_state] = (current_state, edge_id)
                heapq.heappush(queue, (new_cost, next_state))

    if goal_state is None:
        return None

    path, current = [], goal_state
    while current != start_state:
        current, edge_id = parents[current]
        path.append(edge_id)
    path.reverse()

    return path


def best_score_path_within_budget(graph, start_vertex, end_vertex, raster_layer, edge_road_type_cache,
                                   budget_multiplier=BUDGET_MULTIPLIER, lambda_iterations=25,
                                   right_turn_bonus=RIGHT_TURN_BONUS, aux_to_main_bonus=AUX_TO_MAIN_BONUS):

    score_cache = {}

    shortest_path, shortest_distance = plain_shortest_distance(graph, start_vertex, end_vertex)
    max_distance = shortest_distance * budget_multiplier
    print(f"Shortest-distance baseline: {shortest_distance:.1f} m ({len(shortest_path)} edges)")
    print(f"Distance budget ({budget_multiplier}x): {max_distance:.1f} m")

    # Find a lambda upper bound by doubling until we bust the budget.
    lambda_max = 1.0
    for _ in range(20):
        trial = lagrangian_weighted_dijkstra(graph, start_vertex, end_vertex, raster_layer, score_cache, edge_road_type_cache, lambda_max, right_turn_bonus, aux_to_main_bonus)
        if trial is None:
            break
        trial_length, _ = path_length_and_score(graph, trial, raster_layer, score_cache, edge_road_type_cache, right_turn_bonus, aux_to_main_bonus)
        if trial_length > max_distance:
            break
        lambda_max *= 2

    # Binary search for the best feasible lambda.
    low, high = 0.0, lambda_max
    best_path = shortest_path
    best_length, best_score = path_length_and_score(graph, shortest_path, raster_layer, score_cache, edge_road_type_cache, right_turn_bonus, aux_to_main_bonus)

    for _ in range(lambda_iterations):
        mid = (low + high) / 2
        candidate = lagrangian_weighted_dijkstra(graph, start_vertex, end_vertex, raster_layer, score_cache, edge_road_type_cache, mid, right_turn_bonus, aux_to_main_bonus)
        if candidate is None:
            high = mid
            continue

        candidate_length, candidate_score = path_length_and_score(graph, candidate, raster_layer, score_cache, edge_road_type_cache, right_turn_bonus, aux_to_main_bonus)
        if candidate_length <= max_distance:
            if candidate_score > best_score:
                best_path, best_length, best_score = candidate, candidate_length, candidate_score
            low = mid
        else:
            high = mid

    print(f"Best path found: length={best_length:.1f} m (budget {max_distance:.1f} m), score={best_score:.1f}")

    return {
        "best_path": best_path,
        "best_length": best_length,
        "best_score": best_score,
        "shortest_path": shortest_path,
        "shortest_distance": shortest_distance,
        "max_distance": max_distance,
    }


# ============================================================
# Map output
# ============================================================

def points_to_layer(points, crs, name="raster_sample_failures"):
    layer = QgsVectorLayer("Point?crs=" + crs, name, "memory")
    features = [QgsFeature() for _ in points]
    for feat, point in zip(features, points):
        feat.setGeometry(QgsGeometry.fromPointXY(point))
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


def edges_to_layer(graph, path_edges, crs, name="route_output"):
    layer = QgsVectorLayer("LineString?crs=" + crs, name, "memory")
    features = []
    for edge_id in path_edges:
        edge = graph.edge(edge_id)
        geom = QgsGeometry.fromPolylineXY([
            graph.vertex(edge.fromVertex()).point(),
            graph.vertex(edge.toVertex()).point(),
        ])
        feat = QgsFeature()
        feat.setGeometry(geom)
        features.append(feat)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


# ============================================================
# Run
# ============================================================

road_layers = QgsProject.instance().mapLayersByName(ROAD_LAYER_NAME)
if not road_layers:
    raise RuntimeError(f"No layer named '{ROAD_LAYER_NAME}' found.")
road_layer = road_layers[0]

raster_layers = QgsProject.instance().mapLayersByName(RASTER_LAYER_NAME)
if not raster_layers:
    raise RuntimeError(f"No layer named '{RASTER_LAYER_NAME}' found.")
raster_layer = raster_layers[0]
if not raster_layer.isValid():
    raise RuntimeError(f"'{RASTER_LAYER_NAME}' is not a valid raster.")

assert_matching_crs(road_layer, raster_layer)

# Restrict to drivable road classes only - see ROUTABLE_CLASS_CODES.
# setSubsetString() filters at the provider level, so it affects graph
# building, the road-type spatial index, AND what's rendered in the
# Layers panel. Call road_layer.setSubsetString("") to clear it later.
codes = ",".join(str(c) for c in sorted(ROUTABLE_CLASS_CODES))
road_layer.setSubsetString(f"{ROAD_TYPE_FIELD} IN ({codes})")
print(f"Road layer filtered to classes [{codes}]: {road_layer.featureCount()} features remain")

start = transform_point(*START_LL, "EPSG:4326", road_layer.crs().authid())
end = transform_point(*END_LL, "EPSG:4326", road_layer.crs().authid())

graph, tied = build_graph(road_layer, start, end)
print(f"Graph: {graph.vertexCount()} vertices, {graph.edgeCount()} edges")

start_vertex = find_vertex(graph, start)
end_vertex = find_vertex(graph, end)

# Reference route: minimum cost, turn-aware.
failed_points = []
path_edges, cache = dijkstra_turn_aware(
    graph, start_vertex, end_vertex, raster_layer, turn_costs=DEFAULT_TURN_COSTS, failed_points=failed_points
)
print(f"Min-cost route: {len(path_edges)} edges, turns {summarize_turns(path_edges, graph)}")
if failed_points:
    points_to_layer(failed_points, road_layer.crs().authid())
    print(f"{len(failed_points)} raster samples had no coverage - see raster_sample_failures layer")
edges_to_layer(graph, path_edges, road_layer.crs().authid(), name="route_min_cost")

# The actual point of this project: worst-cost routing.
edge_road_type_cache = build_edge_road_type_cache(graph, road_layer, ROAD_TYPE_FIELD)

# 1. Plain shortest distance - the sane baseline everything else is compared against.
shortest_path, shortest_distance = plain_shortest_distance(graph, start_vertex, end_vertex)
edges_to_layer(graph, shortest_path, road_layer.crs().authid(), name="route_shortest_distance")
print(f"route_shortest_distance: {len(shortest_path)} edges, {shortest_distance:.1f} m")

# 2. Worst-cost route on raster score alone, no turn or road-type bonuses.
raster_only_result = best_score_path_within_budget(
    graph, start_vertex, end_vertex, raster_layer, edge_road_type_cache,
    right_turn_bonus=0.0, aux_to_main_bonus=0.0,
)
edges_to_layer(graph, raster_only_result["best_path"], road_layer.crs().authid(), name="route_worst_raster_only")
print(f"route_worst_raster_only: {len(raster_only_result['best_path'])} edges, "
      f"length={raster_only_result['best_length']:.1f} m, score={raster_only_result['best_score']:.1f}")

# 3. Full worst-cost route: raster score + right-turn bonus + road-type bonus.
result = best_score_path_within_budget(graph, start_vertex, end_vertex, raster_layer, edge_road_type_cache)
edges_to_layer(graph, result["best_path"], road_layer.crs().authid(), name="worst_cost_route")
print(f"worst_cost_route: {len(result['best_path'])} edges, "
      f"length={result['best_length']:.1f} m, score={result['best_score']:.1f}")