from __future__ import annotations

import heapq
import gzip
import json
import math
import os
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import route_instructions


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
NAV_DATA = WORKSPACE_ROOT / "nav_map" / "web" / "data"
LOCAL_LAYER_GZ = ROOT / "data_gz" / "layers"
DB_PATH = ROOT / "routing" / "ieum_graph.sqlite"
ENV_PATH = ROOT / ".env"
RESULTS_DIR = ROOT / "routing" / "results"
TRANSFER_PENALTY = 700.0
ONE_STATION_WALK_M = 900.0
LONG_WALK_PENALTY_PER_M = 0.7
WALK_BUCKET_M = 250.0
MAX_WALK_BUCKET = 16
WALK_LIKE_EDGE_TYPES = {
    "walk",
    "braille_walk",
    "crosswalk",
    "crosswalk_connector",
    "facility_connector",
    "subway_connector",
}


@dataclass(frozen=True)
class Location:
    label: str
    lon: float
    lat: float
    source: str


def haversine_m(left: list[float] | tuple[float, float], right: list[float] | tuple[float, float]) -> float:
    import math

    lon1, lat1 = map(math.radians, left)
    lon2, lat2 = map(math.radians, right)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(value))


class PointGrid:
    def __init__(self, items: list[tuple[str, list[float]]], cell: float = 0.0005) -> None:
        import math

        self.cell = cell
        self.grid: dict[tuple[int, int], list[tuple[str, list[float]]]] = {}
        for item_id, coord in items:
            key = (math.floor(coord[0] / cell), math.floor(coord[1] / cell))
            self.grid.setdefault(key, []).append((item_id, coord))

    def nearby_ids(self, coord: list[float], radius_m: float = 30) -> set[str]:
        import math

        cx = math.floor(coord[0] / self.cell)
        cy = math.floor(coord[1] / self.cell)
        found: set[str] = set()
        for gx in range(cx - 2, cx + 3):
            for gy in range(cy - 2, cy + 3):
                for item_id, item_coord in self.grid.get((gx, gy), []):
                    if haversine_m(coord, item_coord) <= radius_m:
                        found.add(item_id)
        return found


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if not line or line.strip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    values.update({key: value for key, value in os.environ.items() if key.startswith("KAKAO_")})
    return values


def read_geojson_features(path: Path) -> list[dict[str, Any]]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")).get("features", [])
    gz_path = LOCAL_LAYER_GZ / f"{path.name}.gz"
    if gz_path.exists():
        with gzip.open(gz_path, "rt", encoding="utf-8") as handle:
            return json.load(handle).get("features", [])
    return []


def representative_line_points(geometry: dict[str, Any]) -> list[list[float]]:
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "MultiLineString":
        coords = [point for line in coords for point in line]
    if not coords:
        return []
    points = [coords[0], coords[-1]]
    if len(coords) > 2:
        points.append(coords[len(coords) // 2])
    return points


def first_id_value(props: dict[str, Any]) -> Any:
    for key, value in props.items():
        if str(key).endswith(" ID"):
            return value
    return None


@lru_cache(maxsize=1)
def route_context_indexes() -> dict[str, PointGrid]:
    braille_items: list[tuple[str, list[float]]] = []
    for feature in read_geojson_features(NAV_DATA / "braille_network_links.geojson"):
        props = feature.get("properties") or {}
        item_id = f"braille:{props.get('braille_link_id')}"
        for coord in representative_line_points(feature.get("geometry") or {}):
            braille_items.append((item_id, coord))

    crosswalk_items: list[tuple[str, list[float]]] = []
    for feature in read_geojson_features(NAV_DATA / "crosswalk_links_enriched.geojson"):
        props = feature.get("properties") or {}
        item_id = f"crosswalk:{first_id_value(props)}"
        for coord in representative_line_points(feature.get("geometry") or {}):
            crosswalk_items.append((item_id, coord))

    audible_items: list[tuple[str, list[float]]] = []
    for feature in read_geojson_features(NAV_DATA / "audible_signal_points.geojson"):
        props = feature.get("properties") or {}
        coord = (feature.get("geometry") or {}).get("coordinates")
        if coord:
            audible_items.append((f"audible:{props.get('MGRNU')}", coord))

    return {
        "braille": PointGrid(braille_items),
        "crosswalk": PointGrid(crosswalk_items),
        "audible": PointGrid(audible_items),
    }


def route_sample_points(features: list[dict[str, Any]]) -> list[list[float]]:
    samples: list[list[float]] = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        if geometry.get("type") != "LineString" or not coords:
            continue
        samples.extend(coords)
        for left, right in zip(coords, coords[1:]):
            samples.append([(left[0] + right[0]) / 2, (left[1] + right[1]) / 2])
    return samples


def summarize_route_context(features: list[dict[str, Any]], radius_m: float = 30) -> dict[str, Any]:
    indexes = route_context_indexes()
    samples = route_sample_points(features)
    nearby = {"braille": set(), "crosswalk": set(), "audible": set()}
    for coord in samples:
        for key, index in indexes.items():
            nearby[key].update(index.nearby_ids(coord, radius_m))
    return {
        "radius_m": radius_m,
        "nearby_braille_edge_count": len(nearby["braille"]),
        "nearby_crosswalk_count": len(nearby["crosswalk"]),
        "nearby_audible_signal_count": len(nearby["audible"]),
        "meaning": "route corridor context; these features are near the selected route, even if the selected edge itself is a generic walk edge",
    }


def kakao_keyword_search(query: str) -> Location | None:
    api_key = load_env().get("KAKAO_REST_API_KEY")
    if not api_key:
        return None
    params = urllib.parse.urlencode({"query": query, "size": 1})
    request = urllib.request.Request(
        f"https://dapi.kakao.com/v2/local/search/keyword.json?{params}",
        headers={"Authorization": f"KakaoAK {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    docs = payload.get("documents") or []
    if not docs:
        return None
    first = docs[0]
    return Location(
        label=first.get("place_name") or query,
        lon=float(first["x"]),
        lat=float(first["y"]),
        source="kakao.keyword",
    )


def fallback_station_location(conn: sqlite3.Connection, query: str) -> Location | None:
    normalized = query.replace("역", "").replace(" ", "")
    rows = conn.execute(
        "SELECT station_name, lon, lat FROM nodes WHERE node_type = 'subway_station'"
    ).fetchall()
    candidates = []
    for station_name, lon, lat in rows:
        key = str(station_name).replace("역", "").replace(" ", "")
        no_paren = key
        while "(" in no_paren and ")" in no_paren:
            start = no_paren.find("(")
            end = no_paren.find(")", start)
            if end < start:
                break
            no_paren = no_paren[:start] + no_paren[end + 1 :]
        if key == normalized or no_paren == normalized:
            score = 0
        elif key.startswith(normalized) or no_paren.startswith(normalized):
            score = 1
        elif normalized in key or normalized in no_paren:
            score = 2
        else:
            continue
        candidates.append((score, len(no_paren), station_name, lon, lat))
    if not candidates:
        return None
    _, _, station_name, lon, lat = sorted(candidates)[0]
    return Location(label=station_name, lon=float(lon), lat=float(lat), source="sqlite.station_fallback")


def resolve_location(conn: sqlite3.Connection, query: str) -> Location:
    try:
        kakao = kakao_keyword_search(query)
        if kakao:
            return kakao
    except Exception as exc:
        print(f"warning: Kakao lookup failed for {query}: {exc}")
    fallback = fallback_station_location(conn, query)
    if fallback:
        return fallback
    raise RuntimeError(f"location not found: {query}")


def nearest_node(
    conn: sqlite3.Connection,
    lon: float,
    lat: float,
    preferred_types: tuple[str, ...] = ("walk_geometry_endpoint", "subway_station"),
) -> dict[str, Any]:
    for radius in [0.002, 0.005, 0.01, 0.02, 0.05]:
        placeholders = ",".join("?" for _ in preferred_types)
        rows = conn.execute(
            f"""
            SELECT node_id, node_type, lon, lat, station_name,
                   ((lon - ?) * (lon - ?) + (lat - ?) * (lat - ?)) AS d2
            FROM nodes
            WHERE node_type IN ({placeholders})
              AND lon BETWEEN ? AND ?
              AND lat BETWEEN ? AND ?
            ORDER BY d2
            LIMIT 1
            """,
            (
                lon,
                lon,
                lat,
                lat,
                *preferred_types,
                lon - radius,
                lon + radius,
                lat - radius,
                lat + radius,
            ),
        ).fetchall()
        if rows:
            row = rows[0]
            return {
                "node_id": row[0],
                "node_type": row[1],
                "lon": row[2],
                "lat": row[3],
                "station_name": row[4],
            }
    raise RuntimeError("nearest node not found")


def load_adjacency(conn: sqlite3.Connection) -> dict[str, list[tuple[str, float, float, str, str, str | None]]]:
    adjacency: dict[str, list[tuple[str, float, float, str, str, str | None]]] = {}
    for edge_id, from_id, to_id, weight, length_m, edge_type, line_code in conn.execute(
        "SELECT edge_id, from_node_id, to_node_id, visual_impairment_weight, length_m, edge_type, line_code FROM edges"
    ):
        cost = float(weight)
        length = float(length_m or 0)
        adjacency.setdefault(from_id, []).append((to_id, cost, length, edge_id, str(edge_type or ""), str(line_code) if line_code else None))
        adjacency.setdefault(to_id, []).append((from_id, cost, length, edge_id, str(edge_type or ""), str(line_code) if line_code else None))
    return adjacency


def walk_bucket(walk_m: float) -> int:
    return min(int(math.ceil(walk_m / WALK_BUCKET_M)), MAX_WALK_BUCKET)


def long_walk_penalty(previous_walk_m: float, next_walk_m: float) -> float:
    previous_over = max(0.0, previous_walk_m - ONE_STATION_WALK_M)
    next_over = max(0.0, next_walk_m - ONE_STATION_WALK_M)
    return (next_over - previous_over) * LONG_WALK_PENALTY_PER_M


def dijkstra(
    adjacency: dict[str, list[tuple[str, float, float, str, str, str | None]]],
    start_id: str,
    goal_id: str,
    max_visited: int = 350000,
) -> tuple[float, list[tuple[str, str, str]]]:
    start_state = (start_id, None, 0)
    queue: list[tuple[float, str, str | None, int]] = [(0.0, start_id, None, 0)]
    best = {start_state: 0.0}
    previous: dict[tuple[str, str | None, int], tuple[tuple[str, str | None, int], str]] = {}
    goal_state: tuple[str, str | None, int] | None = None
    visited = 0
    while queue:
        cost, node_id, current_line, current_walk_bucket = heapq.heappop(queue)
        state = (node_id, current_line, current_walk_bucket)
        if cost != best.get(state):
            continue
        visited += 1
        if node_id == goal_id:
            goal_state = state
            break
        if visited > max_visited:
            raise RuntimeError(f"route search exceeded visit limit: {max_visited}")
        current_walk_m = current_walk_bucket * WALK_BUCKET_M
        for next_id, edge_cost, edge_length_m, edge_id, edge_type, line_code in adjacency.get(node_id, []):
            next_line = line_code if edge_type == "subway_ride" else current_line
            if edge_type == "subway_ride":
                next_walk_m = 0.0
                next_walk_bucket = 0
            elif edge_type in WALK_LIKE_EDGE_TYPES:
                edge_walk_bucket = walk_bucket(edge_length_m) if edge_length_m > 0 else 0
                next_walk_bucket = min(current_walk_bucket + edge_walk_bucket, MAX_WALK_BUCKET)
                next_walk_m = next_walk_bucket * WALK_BUCKET_M
            else:
                next_walk_m = current_walk_m
                next_walk_bucket = current_walk_bucket
            transfer_cost = (
                TRANSFER_PENALTY
                if edge_type == "subway_ride" and current_line and line_code and current_line != line_code
                else 0.0
            )
            walk_penalty = (
                long_walk_penalty(current_walk_m, next_walk_m)
                if edge_type in WALK_LIKE_EDGE_TYPES
                else 0.0
            )
            new_cost = cost + edge_cost + transfer_cost + walk_penalty
            next_state = (next_id, next_line, next_walk_bucket)
            if new_cost < best.get(next_state, float("inf")):
                best[next_state] = new_cost
                previous[next_state] = (state, edge_id)
                heapq.heappush(queue, (new_cost, next_id, next_line, next_walk_bucket))
    if goal_state is None:
        raise RuntimeError("route not found")

    steps: list[tuple[str, str, str]] = []
    cursor = goal_state
    while cursor != start_state:
        prev, edge_id = previous[cursor]
        steps.append((edge_id, prev[0], cursor[0]))
        cursor = prev
    steps.reverse()
    return best[goal_state], steps


def fetch_edges(conn: sqlite3.Connection, steps: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    if not steps:
        return []
    edge_ids = [edge_id for edge_id, _, _ in steps]
    placeholders = ",".join("?" for _ in edge_ids)
    rows = conn.execute(
        f"""
        SELECT edge_id, source_edge_id, edge_type, from_node_id, to_node_id, length_m,
               visual_impairment_weight, line_code, geometry, raw_properties,
               near_braille_count, near_crosswalk_count, near_audible_signal_count,
               accessibility_enriched
        FROM edges
        WHERE edge_id IN ({placeholders})
        """,
        edge_ids,
    ).fetchall()
    by_id = {row[0]: row for row in rows}
    node_ids = sorted({node_id for _, route_from, route_to in steps for node_id in (route_from, route_to)})
    node_placeholders = ",".join("?" for _ in node_ids)
    node_rows = conn.execute(
        f"SELECT node_id, node_type, station_name, lon, lat FROM nodes WHERE node_id IN ({node_placeholders})",
        node_ids,
    ).fetchall()
    nodes_by_id = {
        row[0]: {
            "node_type": row[1],
            "station_name": row[2],
            "lon": row[3],
            "lat": row[4],
        }
        for row in node_rows
    }
    result = []
    for edge_id, route_from, route_to in steps:
        row = by_id[edge_id]
        props = json.loads(row[9])
        props["edge_id"] = row[0]
        props["source_edge_id"] = row[1]
        props["edge_type"] = row[2]
        props["length_m"] = float(row[5] or 0)
        props["visual_impairment_weight"] = float(row[6] or 0)
        props["line_code"] = row[7]
        props["near_braille_count"] = int(row[10] or 0)
        props["near_crosswalk_count"] = int(row[11] or 0)
        props["near_audible_signal_count"] = int(row[12] or 0)
        props["accessibility_enriched"] = bool(row[13])
        props["route_from_node_id"] = route_from
        props["route_to_node_id"] = route_to
        props["route_from_node"] = nodes_by_id.get(route_from, {})
        props["route_to_node"] = nodes_by_id.get(route_to, {})
        geometry = json.loads(row[8])
        if row[3] == route_to and row[4] == route_from and geometry.get("type") == "LineString":
            geometry["coordinates"] = list(reversed(geometry.get("coordinates") or []))
        result.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": props,
            }
        )
    return result


def summarize_route(features: list[dict[str, Any]], cost: float, start: Location, end: Location) -> dict[str, Any]:
    edge_type_counts: dict[str, int] = {}
    edge_type_lengths: dict[str, float] = {}
    subway_lines: list[str] = []
    last_subway_line: str | None = None
    transfer_count = 0
    total_length = 0.0
    accessible = {
        "braille_edge_count": 0,
        "braille_length_m": 0.0,
        "crosswalk_count": 0,
        "crosswalk_length_m": 0.0,
        "audible_signal_edge_count": 0,
        "audible_signal_length_m": 0.0,
        "near_braille_edge_count": 0,
        "near_braille_length_m": 0.0,
        "near_crosswalk_edge_count": 0,
        "near_crosswalk_length_m": 0.0,
        "near_audible_signal_edge_count": 0,
        "near_audible_signal_length_m": 0.0,
        "ped_signal_edge_count": 0,
        "ped_signal_length_m": 0.0,
        "elevator_connector_count": 0,
        "elevator_connector_length_m": 0.0,
        "subway_ride_count": 0,
        "subway_ride_length_m": 0.0,
        "subway_connector_count": 0,
        "subway_connector_length_m": 0.0,
        "walk_count": 0,
        "walk_length_m": 0.0,
        "low_confidence_count": 0,
        "low_confidence_length_m": 0.0,
    }
    for feature in features:
        props = feature["properties"]
        edge_type = props.get("edge_type") or "unknown"
        length_m = float(props.get("length_m") or 0)
        near_braille = int(props.get("near_braille_count") or 0)
        near_crosswalk = int(props.get("near_crosswalk_count") or 0)
        near_audible = int(props.get("near_audible_signal_count") or 0)
        edge_type_counts[edge_type] = edge_type_counts.get(edge_type, 0) + 1
        edge_type_lengths[edge_type] = edge_type_lengths.get(edge_type, 0.0) + length_m
        total_length += length_m
        if edge_type == "subway_ride" and props.get("line_code"):
            line_code = str(props["line_code"])
            subway_lines.append(line_code)
            if last_subway_line and last_subway_line != line_code:
                transfer_count += 1
            last_subway_line = line_code
        if edge_type in {"walk", "braille_walk"}:
            accessible["walk_count"] += 1
            accessible["walk_length_m"] += length_m
        if edge_type == "braille_walk" or props.get("has_braille") is True:
            accessible["braille_edge_count"] += 1
            accessible["braille_length_m"] += length_m
        if edge_type == "crosswalk":
            accessible["crosswalk_count"] += 1
            accessible["crosswalk_length_m"] += length_m
        if props.get("has_audible_signal") is True:
            accessible["audible_signal_edge_count"] += 1
            accessible["audible_signal_length_m"] += length_m
        if near_braille:
            accessible["near_braille_edge_count"] += 1
            accessible["near_braille_length_m"] += length_m
        if near_crosswalk:
            accessible["near_crosswalk_edge_count"] += 1
            accessible["near_crosswalk_length_m"] += length_m
        if near_audible:
            accessible["near_audible_signal_edge_count"] += 1
            accessible["near_audible_signal_length_m"] += length_m
        if props.get("has_ped_signal") is True:
            accessible["ped_signal_edge_count"] += 1
            accessible["ped_signal_length_m"] += length_m
        if props.get("has_elevator") is True:
            accessible["elevator_connector_count"] += 1
            accessible["elevator_connector_length_m"] += length_m
        if edge_type == "subway_ride":
            accessible["subway_ride_count"] += 1
            accessible["subway_ride_length_m"] += length_m
        if edge_type == "subway_connector":
            accessible["subway_connector_count"] += 1
            accessible["subway_connector_length_m"] += length_m
        if props.get("data_confidence") == "low":
            accessible["low_confidence_count"] += 1
            accessible["low_confidence_length_m"] += length_m
    for key, value in list(accessible.items()):
        if key.endswith("_m"):
            accessible[key] = round(float(value), 1)
    edge_type_lengths = {key: round(value, 1) for key, value in edge_type_lengths.items()}
    walk_or_crosswalk_length = accessible["walk_length_m"] + accessible["crosswalk_length_m"]
    accessible["braille_coverage_ratio"] = (
        round(accessible["braille_length_m"] / walk_or_crosswalk_length, 3)
        if walk_or_crosswalk_length
        else 0
    )
    accessible["audible_crosswalk_ratio"] = (
        round(accessible["audible_signal_edge_count"] / accessible["crosswalk_count"], 3)
        if accessible["crosswalk_count"]
        else 0
    )
    return {
        "start": start.__dict__,
        "end": end.__dict__,
        "edge_count": len(features),
        "total_length_m": round(total_length, 1),
        "total_visual_impairment_cost": round(cost, 1),
        "edge_type_counts": edge_type_counts,
        "edge_type_lengths_m": edge_type_lengths,
        "dataset_coverage": accessible,
        "route_corridor_context": summarize_route_context(features),
        "subway_lines": sorted(set(subway_lines)),
        "transfer_count": transfer_count,
        "uses_subway": bool(subway_lines),
    }


def parse_coordinate_query(query: str) -> Location | None:
    parts = [part.strip() for part in query.split(",")]
    if len(parts) != 2:
        return None
    try:
        first = float(parts[0])
        second = float(parts[1])
    except ValueError:
        return None
    if 120 <= first <= 140 and 30 <= second <= 45:
        lon, lat = first, second
    elif 30 <= first <= 45 and 120 <= second <= 140:
        lat, lon = first, second
    else:
        return None
    return Location(label=query, lon=lon, lat=lat, source="input.coordinate")


def resolve_location_any(conn: sqlite3.Connection, query: str) -> Location:
    parsed = parse_coordinate_query(query)
    if parsed:
        return parsed
    return resolve_location(conn, query)


def build_route_geojson(
    conn: sqlite3.Connection,
    start_query: str,
    end_query: str,
    adjacency: dict[str, list[tuple[str, float, str]]] | None = None,
) -> dict[str, Any]:
    start = resolve_location_any(conn, start_query)
    end = resolve_location_any(conn, end_query)
    start_node = nearest_node(conn, start.lon, start.lat)
    end_node = nearest_node(conn, end.lon, end.lat)
    graph = adjacency if adjacency is not None else load_adjacency(conn)
    cost, steps = dijkstra(graph, start_node["node_id"], end_node["node_id"])
    features = fetch_edges(conn, steps)
    summary = summarize_route(features, cost, start, end)
    summary["start_snap_node"] = start_node
    summary["end_snap_node"] = end_node
    return {
        "type": "FeatureCollection",
        "properties": summary,
        "instructions": route_instructions.generate_instructions({"type": "FeatureCollection", "properties": summary, "features": features}),
        "features": features,
    }


def route_between(start_query: str, end_query: str, output_name: str) -> dict[str, Any]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        route_geojson = build_route_geojson(conn, start_query, end_query)
        summary = route_geojson["properties"]
        route_path = RESULTS_DIR / f"{output_name}.geojson"
        summary_path = RESULTS_DIR / f"{output_name}_summary.json"
        route_path.write_text(json.dumps(route_geojson, ensure_ascii=False, indent=2), encoding="utf-8")
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"summary": summary, "route_path": str(route_path), "summary_path": str(summary_path)}
    finally:
        conn.close()


def main() -> int:
    result = route_between("고덕역", "잠실역", "godeok_to_jamsil")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
