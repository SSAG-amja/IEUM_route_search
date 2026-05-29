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
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import route_instructions


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
NAV_DATA = WORKSPACE_ROOT / "nav_map" / "web" / "data"
LOCAL_LAYER_GZ = ROOT / "data_gz" / "layers"
SOURCE_GZ = ROOT / "data_gz" / "source"
SUBWAY_CATALOG_GZ = SOURCE_GZ / "subway_station_catalog"
TRANSPORT_ACCESSIBILITY_GZ = SOURCE_GZ / "transport_accessibility_catalog"
TRANSIT_CONGESTION_GZ = SOURCE_GZ / "transit_congestion_catalog"
DB_PATH = Path(os.environ.get("IEUM_ROUTE_DB_PATH", str(ROOT / "routing" / "ieum_graph.sqlite"))).expanduser()
ENV_PATH = ROOT / ".env"
RESULTS_DIR = ROOT / "routing" / "results"
TRANSFER_PENALTY = 700.0
ONE_STATION_WALK_M = 900.0
LONG_WALK_PENALTY_PER_M = 0.7
WALK_BUCKET_M = 250.0
MAX_WALK_BUCKET = 16
STATION_CANDIDATE_LIMIT = 5
STATION_CANDIDATE_RADIUS_M = 1800.0
DIRECT_WALK_LIMIT_M = 900.0
EDGE_SNAP_RADIUS_M = 120.0
SUBWAY_LINE_CONGESTION_WEIGHT = 0.12
SUBWAY_STATION_CONGESTION_WEIGHT = 0.08
SEOUL_TZ = timezone(timedelta(hours=9), name="Asia/Seoul")
try:
    SEOUL_TZ = ZoneInfo("Asia/Seoul")
except ZoneInfoNotFoundError:
    pass
WALK_LIKE_EDGE_TYPES = {
    "walk",
    "braille_walk",
    "crosswalk",
    "crosswalk_connector",
    "facility_connector",
    "subway_connector",
}
ROUTABLE_SNAP_EDGE_TYPES = {
    "walk",
    "braille_walk",
    "crosswalk",
    "crosswalk_connector",
    "facility_connector",
    "subway_connector",
}

Edge = tuple[str, float, float, str, str, str | None]
AdjacencyMap = dict[str, list[Edge]]
VirtualAdjacencyMap = dict[str, tuple[Edge, ...]]


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


def read_gzip_json(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compact_station_key(value: Any) -> str:
    text = str(value or "").strip().replace(" ", "")
    while "(" in text and ")" in text:
        start = text.find("(")
        end = text.find(")", start)
        if end < start:
            break
        text = text[:start] + text[end + 1 :]
    return text[:-1] if text.endswith("역") else text


def compact_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return str(int(text))
    return text


@lru_cache(maxsize=1)
def subway_station_code_index() -> dict[str, set[str]]:
    path = SUBWAY_CATALOG_GZ / "station_line_nodes.json.gz"
    if not path.exists():
        return {}
    index: dict[str, set[str]] = {}
    for item in read_gzip_json(path):
        key = compact_station_key(item.get("station_name_key") or item.get("station_name") or item.get("api_station_name"))
        if not key:
            continue
        codes = index.setdefault(key, set())
        for field in ("api_station_code", "station_code", "source_station_code"):
            code = compact_code(item.get(field))
            if code:
                codes.add(code)
    return index


@lru_cache(maxsize=1)
def subway_congestion_index() -> dict[str, Any]:
    path = TRANSIT_CONGESTION_GZ / "transit_congestion_subway_summary.json.gz"
    if not path.exists():
        return {
            "available": False,
            "scenarios": {},
            "scenario_count": 0,
        }
    payload = read_gzip_json(path)
    scenarios: dict[str, dict[str, Any]] = {}
    for scenario in payload.get("scenarios") or []:
        scenario_id = str(scenario.get("scenario_id") or "")
        if not scenario_id:
            continue
        line_scores: dict[str, float] = {}
        station_scores: dict[str, float] = {}
        for item in scenario.get("route_congestion") or []:
            key = compact_code(item.get("id"))
            line_scores[key] = max(line_scores.get(key, 0.0), safe_float(item.get("congestion_score")))
        for bucket in ("boarding_stop_congestion", "alighting_stop_congestion"):
            for item in scenario.get(bucket) or []:
                key = compact_code(item.get("id"))
                station_scores[key] = max(station_scores.get(key, 0.0), safe_float(item.get("congestion_score")))
        scenarios[scenario_id] = {
            "label": scenario.get("label"),
            "mode": scenario.get("mode"),
            "line_scores": line_scores,
            "station_scores": station_scores,
        }
    return {
        "available": True,
        "scenarios": scenarios,
        "scenario_count": len(payload.get("scenarios") or []),
        "source": str(path.relative_to(ROOT)),
    }


def current_subway_congestion_scenario_id(now: datetime | None = None) -> str | None:
    seoul_now = now.astimezone(SEOUL_TZ) if now else datetime.now(SEOUL_TZ)
    weekday = seoul_now.weekday()
    hour = seoul_now.hour
    if weekday < 5 and 7 <= hour < 10:
        return "weekday_morning_subway"
    if weekday >= 5 and 13 <= hour < 18:
        return "weekend_afternoon_subway"
    return None


def active_subway_congestion() -> dict[str, Any]:
    index = subway_congestion_index()
    scenario_id = current_subway_congestion_scenario_id()
    scenario = (index.get("scenarios") or {}).get(scenario_id or "")
    return {
        "available": bool(index.get("available")),
        "active_scenario_id": scenario_id,
        "active_scenario": scenario,
        "source": index.get("source"),
        "scenario_count": index.get("scenario_count", 0),
    }


@lru_cache(maxsize=1)
def subway_accessibility_index() -> dict[str, Any]:
    roads_path = TRANSPORT_ACCESSIBILITY_GZ / "subway_accessibility_roads.json.gz"
    districts_path = TRANSPORT_ACCESSIBILITY_GZ / "subway_accessibility_districts.json.gz"
    if not roads_path.exists():
        return {"available": False, "road_scores": {}, "district_count": 0, "road_count": 0}
    roads_payload = read_gzip_json(roads_path)
    roads = roads_payload.get("roads") or []
    road_scores = {
        str(item.get("road_name") or ""): item
        for item in roads
        if item.get("road_name")
    }
    district_count = 0
    if districts_path.exists():
        district_count = len(read_gzip_json(districts_path).get("districts") or [])
    return {
        "available": True,
        "road_scores": road_scores,
        "road_count": len(roads),
        "district_count": district_count,
        "source": str(roads_path.relative_to(ROOT)),
    }


def station_congestion_score(station_name: Any) -> float:
    congestion = active_subway_congestion()
    scenario = congestion.get("active_scenario") or {}
    station_scores: dict[str, float] = scenario.get("station_scores") or {}
    codes = subway_station_code_index().get(compact_station_key(station_name), set())
    if not codes:
        return 0.0
    return max((station_scores.get(code, 0.0) for code in codes), default=0.0)


def line_congestion_score(line_code: Any) -> float:
    congestion = active_subway_congestion()
    scenario = congestion.get("active_scenario") or {}
    line_scores: dict[str, float] = scenario.get("line_scores") or {}
    key = compact_code(line_code)
    candidates = [key]
    if key.isdigit():
        line_no = int(key)
        if 5 <= line_no <= 8:
            candidates.append(str(200 + line_no))
        elif line_no == 1:
            candidates.extend(str(100 + offset) for offset in range(1, 15))
        elif line_no == 3:
            candidates.extend(str(300 + offset) for offset in range(1, 30))
        elif line_no == 4:
            candidates.extend(str(400 + offset) for offset in range(1, 20))
    return max((line_scores.get(candidate, 0.0) for candidate in candidates), default=0.0)


def route_accessibility_context(start: Location, end: Location) -> dict[str, Any]:
    index = subway_accessibility_index()
    context = {
        "available": bool(index.get("available")),
        "source": index.get("source"),
        "road_count": index.get("road_count", 0),
        "district_count": index.get("district_count", 0),
        "matched_roads": [],
    }
    if not index.get("available"):
        return context
    label_text = f"{start.label} {end.label}"
    matched = []
    for road_name, item in (index.get("road_scores") or {}).items():
        if road_name and road_name in label_text:
            matched.append(
                {
                    "road_name": road_name,
                    "cty_nm": item.get("cty_nm"),
                    "access_score_avg": item.get("access_score_avg"),
                    "access_dist_avg_m": item.get("access_dist_avg_m"),
                    "access_dist_min_m": item.get("access_dist_min_m"),
                }
            )
    context["matched_roads"] = sorted(
        matched,
        key=lambda item: safe_float(item.get("access_score_avg")),
        reverse=True,
    )[:4]
    return context


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


def nearby_subway_stations(
    conn: sqlite3.Connection,
    lon: float,
    lat: float,
    limit: int = STATION_CANDIDATE_LIMIT,
    radius_m: float = STATION_CANDIDATE_RADIUS_M,
) -> list[dict[str, Any]]:
    radius_deg = radius_m / 111000
    rows = conn.execute(
        """
        SELECT node_id, node_type, lon, lat, station_name,
               ((lon - ?) * (lon - ?) + (lat - ?) * (lat - ?)) AS d2
        FROM nodes
        WHERE node_type = 'subway_station'
          AND lon BETWEEN ? AND ?
          AND lat BETWEEN ? AND ?
        ORDER BY d2
        LIMIT ?
        """,
        (
            lon,
            lon,
            lat,
            lat,
            lon - radius_deg,
            lon + radius_deg,
            lat - radius_deg,
            lat + radius_deg,
            limit * 4,
        ),
    ).fetchall()
    stations = [
        {
            "node_id": row[0],
            "node_type": row[1],
            "lon": row[2],
            "lat": row[3],
            "station_name": row[4],
            "distance_m": haversine_m((lon, lat), (row[2], row[3])),
        }
        for row in rows
    ]
    stations = [station for station in stations if station["distance_m"] <= radius_m]
    return sorted(stations, key=lambda station: station["distance_m"])[:limit]


def load_adjacency(conn: sqlite3.Connection) -> AdjacencyMap:
    adjacency: AdjacencyMap = {}
    for edge_id, from_id, to_id, weight, length_m, edge_type, line_code in conn.execute(
        "SELECT edge_id, from_node_id, to_node_id, visual_impairment_weight, length_m, edge_type, line_code FROM edges"
    ):
        cost = float(weight)
        length = float(length_m or 0)
        adjacency.setdefault(from_id, []).append((to_id, cost, length, edge_id, str(edge_type or ""), str(line_code) if line_code else None))
        adjacency.setdefault(to_id, []).append((from_id, cost, length, edge_id, str(edge_type or ""), str(line_code) if line_code else None))
    return adjacency


def iter_neighbors(
    adjacency: AdjacencyMap,
    node_id: str,
    virtual_adjacency: VirtualAdjacencyMap | None = None,
):
    for edge in adjacency.get(node_id, []):
        yield edge
    if virtual_adjacency:
        for edge in virtual_adjacency.get(node_id, ()):
            yield edge


def line_length_m(coords: list[list[float]]) -> float:
    return sum(haversine_m(left, right) for left, right in zip(coords, coords[1:]))


def project_point_to_segment(
    point: tuple[float, float],
    left: list[float],
    right: list[float],
) -> tuple[list[float], float]:
    lon, lat = point
    lon1, lat1 = left
    lon2, lat2 = right
    scale = math.cos(math.radians(lat))
    px = lon * scale
    py = lat
    ax = lon1 * scale
    ay = lat1
    bx = lon2 * scale
    by = lat2
    dx = bx - ax
    dy = by - ay
    denom = dx * dx + dy * dy
    if denom == 0:
        projected = [lon1, lat1]
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
        projected = [lon1 + (lon2 - lon1) * t, lat1 + (lat2 - lat1) * t]
    return projected, haversine_m(point, projected)


def nearest_point_on_line(point: tuple[float, float], coords: list[list[float]]) -> tuple[list[float], int, float]:
    best_point = coords[0]
    best_segment = 0
    best_distance = float("inf")
    for idx, (left, right) in enumerate(zip(coords, coords[1:])):
        projected, distance = project_point_to_segment(point, left, right)
        if distance < best_distance:
            best_point = projected
            best_segment = idx
            best_distance = distance
    return best_point, best_segment, best_distance


def split_line_at_projection(coords: list[list[float]], projected: list[float], segment_idx: int) -> tuple[list[list[float]], list[list[float]]]:
    before = coords[: segment_idx + 1]
    if before[-1] != projected:
        before.append(projected)
    after = [projected]
    if coords[segment_idx + 1] != projected:
        after.extend(coords[segment_idx + 1 :])
    else:
        after.extend(coords[segment_idx + 2 :])
    return before, after


def nearest_routable_edge_snap(
    conn: sqlite3.Connection,
    lon: float,
    lat: float,
    radius_m: float = EDGE_SNAP_RADIUS_M,
) -> dict[str, Any] | None:
    radius_deg = radius_m / 111000
    placeholders = ",".join("?" for _ in ROUTABLE_SNAP_EDGE_TYPES)
    rows = conn.execute(
        f"""
        SELECT edge_id, from_node_id, to_node_id, edge_type, length_m,
               visual_impairment_weight, line_code, geometry, raw_properties,
               near_braille_count, near_crosswalk_count, near_audible_signal_count,
               accessibility_enriched
        FROM edges
        WHERE edge_type IN ({placeholders})
          AND (
            from_node_id IN (
              SELECT node_id FROM nodes
              WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?
            )
            OR to_node_id IN (
              SELECT node_id FROM nodes
              WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?
            )
          )
        """,
        (
            *ROUTABLE_SNAP_EDGE_TYPES,
            lon - radius_deg,
            lon + radius_deg,
            lat - radius_deg,
            lat + radius_deg,
            lon - radius_deg,
            lon + radius_deg,
            lat - radius_deg,
            lat + radius_deg,
        ),
    ).fetchall()
    best: dict[str, Any] | None = None
    for row in rows:
        geometry = json.loads(row[7])
        coords = geometry.get("coordinates") or []
        if geometry.get("type") != "LineString" or len(coords) < 2:
            continue
        projected, segment_idx, distance = nearest_point_on_line((lon, lat), coords)
        if distance > radius_m:
            continue
        if best is None or distance < best["snap_distance_m"]:
            best = {
                "edge_id": row[0],
                "from_node_id": row[1],
                "to_node_id": row[2],
                "edge_type": row[3],
                "length_m": float(row[4] or 0),
                "visual_impairment_weight": float(row[5] or 0),
                "line_code": str(row[6]) if row[6] else None,
                "geometry": geometry,
                "raw_properties": row[8],
                "near_braille_count": int(row[9] or 0),
                "near_crosswalk_count": int(row[10] or 0),
                "near_audible_signal_count": int(row[11] or 0),
                "accessibility_enriched": bool(row[12]),
                "projected": projected,
                "segment_idx": segment_idx,
                "snap_distance_m": distance,
            }
    return best


def virtual_edge_feature(
    edge_id: str,
    base_edge: dict[str, Any],
    from_id: str,
    to_id: str,
    coords: list[list[float]],
) -> dict[str, Any]:
    length = line_length_m(coords)
    base_length = max(float(base_edge.get("length_m") or 0), 1.0)
    base_weight = float(base_edge.get("visual_impairment_weight") or base_length)
    props = json.loads(base_edge.get("raw_properties") or "{}")
    props.update(
        {
            "edge_id": edge_id,
            "source_edge_id": base_edge["edge_id"],
            "edge_type": base_edge["edge_type"],
            "from_node_id": from_id,
            "to_node_id": to_id,
            "source": "generated.edge_projection_snap",
            "length_m": length,
            "visual_impairment_weight": base_weight * (length / base_length),
            "line_code": base_edge.get("line_code"),
            "near_braille_count": base_edge.get("near_braille_count", 0),
            "near_crosswalk_count": base_edge.get("near_crosswalk_count", 0),
            "near_audible_signal_count": base_edge.get("near_audible_signal_count", 0),
            "accessibility_enriched": base_edge.get("accessibility_enriched", False),
            "route_from_node_id": from_id,
            "route_to_node_id": to_id,
            "data_confidence": "medium",
            "snap_source": "nearest_walk_edge_projection",
        }
    )
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": props,
    }


def add_virtual_snap_node(
    virtual_adjacency: dict[str, list[Edge]],
    virtual_edges: dict[str, dict[str, Any]],
    conn: sqlite3.Connection,
    location: Location,
    node_prefix: str,
) -> dict[str, Any]:
    snap = nearest_routable_edge_snap(conn, location.lon, location.lat)
    if snap is None:
        return nearest_node(conn, location.lon, location.lat)
    node_id = f"virtual:{node_prefix}"
    projected = snap["projected"]
    left_coords, right_coords = split_line_at_projection(snap["geometry"]["coordinates"], projected, snap["segment_idx"])
    pieces = [
        (f"virtual:{node_prefix}:left", snap["from_node_id"], node_id, left_coords),
        (f"virtual:{node_prefix}:right", node_id, snap["to_node_id"], right_coords),
    ]
    for edge_id, from_id, to_id, coords in pieces:
        feature = virtual_edge_feature(edge_id, snap, from_id, to_id, coords)
        props = feature["properties"]
        cost = float(props["visual_impairment_weight"])
        length = float(props["length_m"])
        edge_type = str(props["edge_type"])
        line_code = str(props["line_code"]) if props.get("line_code") else None
        virtual_edges[edge_id] = feature
        virtual_adjacency.setdefault(from_id, []).append((to_id, cost, length, edge_id, edge_type, line_code))
        virtual_adjacency.setdefault(to_id, []).append((from_id, cost, length, edge_id, edge_type, line_code))
    return {
        "node_id": node_id,
        "node_type": "virtual_edge_projection",
        "lon": projected[0],
        "lat": projected[1],
        "station_name": None,
        "snap_edge_id": snap["edge_id"],
        "snap_distance_m": round(float(snap["snap_distance_m"]), 1),
    }


def walk_bucket(walk_m: float) -> int:
    return min(int(math.ceil(walk_m / WALK_BUCKET_M)), MAX_WALK_BUCKET)


def long_walk_penalty(previous_walk_m: float, next_walk_m: float) -> float:
    previous_over = max(0.0, previous_walk_m - ONE_STATION_WALK_M)
    next_over = max(0.0, next_walk_m - ONE_STATION_WALK_M)
    return (next_over - previous_over) * LONG_WALK_PENALTY_PER_M


def station_access_penalty(distance_m: float) -> float:
    return max(0.0, distance_m - ONE_STATION_WALK_M) * LONG_WALK_PENALTY_PER_M


def dijkstra(
    adjacency: AdjacencyMap,
    start_id: str,
    goal_id: str,
    max_visited: int = 2000000,
    allowed_edge_types: set[str] | None = None,
    apply_long_walk_penalty: bool = True,
    apply_transfer_penalty: bool = True,
    virtual_adjacency: VirtualAdjacencyMap | None = None,
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
        for next_id, edge_cost, edge_length_m, edge_id, edge_type, line_code in iter_neighbors(
            adjacency,
            node_id,
            virtual_adjacency,
        ):
            if allowed_edge_types is not None and edge_type not in allowed_edge_types:
                continue
            next_line = line_code if edge_type == "subway_ride" else current_line
            if not apply_long_walk_penalty:
                next_walk_m = 0.0
                next_walk_bucket = 0
            elif edge_type == "subway_ride":
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
                if apply_transfer_penalty and edge_type == "subway_ride" and current_line and line_code and current_line != line_code
                else 0.0
            )
            walk_penalty = (
                long_walk_penalty(current_walk_m, next_walk_m)
                if apply_long_walk_penalty and edge_type in WALK_LIKE_EDGE_TYPES
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


def fetch_edges(
    conn: sqlite3.Connection,
    steps: list[tuple[str, str, str]],
    virtual_edges: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not steps:
        return []
    edge_ids = [edge_id for edge_id, _, _ in steps]
    db_edge_ids = [edge_id for edge_id in edge_ids if not edge_id.startswith("virtual:")]
    rows = []
    if db_edge_ids:
        placeholders = ",".join("?" for _ in db_edge_ids)
        rows = conn.execute(
            f"""
            SELECT edge_id, source_edge_id, edge_type, from_node_id, to_node_id, length_m,
                   visual_impairment_weight, line_code, geometry, raw_properties,
                   near_braille_count, near_crosswalk_count, near_audible_signal_count,
                   accessibility_enriched
            FROM edges
            WHERE edge_id IN ({placeholders})
            """,
            db_edge_ids,
        ).fetchall()
    by_id = {row[0]: row for row in rows}
    node_ids = sorted({node_id for _, route_from, route_to in steps for node_id in (route_from, route_to)})
    db_node_ids = [node_id for node_id in node_ids if not node_id.startswith("virtual:")]
    node_rows = []
    if db_node_ids:
        node_placeholders = ",".join("?" for _ in db_node_ids)
        node_rows = conn.execute(
            f"SELECT node_id, node_type, station_name, lon, lat FROM nodes WHERE node_id IN ({node_placeholders})",
            db_node_ids,
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
        if edge_id.startswith("virtual:"):
            feature = json.loads(json.dumps((virtual_edges or {})[edge_id]))
            props = feature["properties"]
            original_from = props.get("from_node_id")
            original_to = props.get("to_node_id")
            props["route_from_node_id"] = route_from
            props["route_to_node_id"] = route_to
            props["route_from_node"] = nodes_by_id.get(route_from, {})
            props["route_to_node"] = nodes_by_id.get(route_to, {})
            coords = feature["geometry"].get("coordinates") or []
            if original_from == route_to and original_to == route_from and len(coords) > 1:
                feature["geometry"]["coordinates"] = list(reversed(coords))
            result.append(feature)
            continue
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


def subway_step_context(conn: sqlite3.Connection, steps: list[tuple[str, str, str]]) -> dict[str, Any]:
    edge_ids = [edge_id for edge_id, _, _ in steps if not edge_id.startswith("virtual:")]
    if not edge_ids:
        return {
            "line_codes": [],
            "line_congestion_score": 0.0,
            "station_congestion_score": 0.0,
            "congestion_penalty_multiplier": 0.0,
        }
    placeholders = ",".join("?" for _ in edge_ids)
    rows = conn.execute(
        f"""
        SELECT e.edge_id, e.edge_type, e.line_code,
               nf.station_name AS from_station_name,
               nt.station_name AS to_station_name
        FROM edges e
        LEFT JOIN nodes nf ON nf.node_id = e.from_node_id
        LEFT JOIN nodes nt ON nt.node_id = e.to_node_id
        WHERE e.edge_id IN ({placeholders})
        """,
        edge_ids,
    ).fetchall()
    lines = []
    station_names = []
    for _, edge_type, line_code, from_station, to_station in rows:
        if edge_type == "subway_ride" and line_code:
            lines.append(str(line_code))
            if from_station:
                station_names.append(str(from_station))
            if to_station:
                station_names.append(str(to_station))
    line_scores = [line_congestion_score(line) for line in set(lines)]
    station_scores = [station_congestion_score(name) for name in set(station_names)]
    line_score = max(line_scores, default=0.0)
    station_score = max(station_scores, default=0.0)
    multiplier = (line_score * SUBWAY_LINE_CONGESTION_WEIGHT) + (station_score * SUBWAY_STATION_CONGESTION_WEIGHT)
    congestion = active_subway_congestion()
    return {
        "line_codes": sorted(set(lines)),
        "line_congestion_score": round(line_score, 4),
        "station_congestion_score": round(station_score, 4),
        "congestion_penalty_multiplier": round(multiplier, 4),
        "active_scenario_id": congestion.get("active_scenario_id"),
        "active_scenario_label": (congestion.get("active_scenario") or {}).get("label"),
        "data_source": congestion.get("source"),
        "scenario_count": congestion.get("scenario_count", 0),
    }


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
        if edge_type in {"route_start_connector", "route_end_connector"}:
            accessible["walk_count"] += 1
            accessible["walk_length_m"] += length_m
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
        "transport_accessibility_context": route_accessibility_context(start, end),
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


def candidate_subway_route(
    conn: sqlite3.Connection,
    adjacency: AdjacencyMap,
    start: Location,
    end: Location,
    start_node: dict[str, Any],
    end_node: dict[str, Any],
    virtual_adjacency: VirtualAdjacencyMap | None = None,
) -> tuple[float, list[tuple[str, str, str]], dict[str, Any]] | None:
    start_stations = nearby_subway_stations(conn, start.lon, start.lat)
    end_stations = nearby_subway_stations(conn, end.lon, end.lat)
    if not start_stations or not end_stations:
        return None

    best: tuple[float, list[tuple[str, str, str]], dict[str, Any]] | None = None
    walk_edge_types = set(WALK_LIKE_EDGE_TYPES)
    subway_edge_types = {"subway_ride"}

    start_walk_cache: dict[str, tuple[float, list[tuple[str, str, str]]]] = {}
    end_walk_cache: dict[str, tuple[float, list[tuple[str, str, str]]]] = {}
    subway_cache: dict[tuple[str, str], tuple[float, list[tuple[str, str, str]]]] = {}

    for start_station in start_stations:
        start_station_id = start_station["node_id"]
        try:
            start_walk_cache[start_station_id] = dijkstra(
                adjacency,
                start_node["node_id"],
                start_station_id,
                allowed_edge_types=walk_edge_types,
                apply_long_walk_penalty=False,
                apply_transfer_penalty=False,
                virtual_adjacency=virtual_adjacency,
            )
        except RuntimeError:
            continue

        for end_station in end_stations:
            end_station_id = end_station["node_id"]
            if start_station_id == end_station_id:
                continue
            try:
                if end_station_id not in end_walk_cache:
                    end_walk_cache[end_station_id] = dijkstra(
                        adjacency,
                        end_station_id,
                        end_node["node_id"],
                        allowed_edge_types=walk_edge_types,
                        apply_long_walk_penalty=False,
                        apply_transfer_penalty=False,
                        virtual_adjacency=virtual_adjacency,
                    )
                subway_key = (start_station_id, end_station_id)
                if subway_key not in subway_cache:
                    subway_cache[subway_key] = dijkstra(
                        adjacency,
                        start_station_id,
                        end_station_id,
                        allowed_edge_types=subway_edge_types,
                        apply_long_walk_penalty=False,
                        apply_transfer_penalty=True,
                        virtual_adjacency=virtual_adjacency,
                    )
            except RuntimeError:
                continue

            start_cost, start_steps = start_walk_cache[start_station_id]
            subway_cost, subway_steps = subway_cache[(start_station_id, end_station_id)]
            end_cost, end_steps = end_walk_cache[end_station_id]
            access_penalty = station_access_penalty(start_station["distance_m"]) + station_access_penalty(end_station["distance_m"])
            congestion_context = subway_step_context(conn, subway_steps)
            congestion_penalty = subway_cost * safe_float(congestion_context.get("congestion_penalty_multiplier"))
            total_cost = start_cost + subway_cost + end_cost + access_penalty + congestion_penalty
            steps = start_steps + subway_steps + end_steps
            route_info = {
                "routing_strategy": "candidate_station_subway",
                "start_station": start_station,
                "end_station": end_station,
                "cost_breakdown": {
                    "start_walk": round(start_cost, 1),
                    "subway": round(subway_cost, 1),
                    "end_walk": round(end_cost, 1),
                    "station_access_penalty": round(access_penalty, 1),
                    "subway_congestion_penalty": round(congestion_penalty, 1),
                },
                "subway_congestion_context": congestion_context,
            }
            if best is None or total_cost < best[0]:
                best = (total_cost, steps, route_info)

    return best


def build_route_geojson(
    conn: sqlite3.Connection,
    start_query: str,
    end_query: str,
    adjacency: AdjacencyMap | None = None,
) -> dict[str, Any]:
    start = resolve_location_any(conn, start_query)
    end = resolve_location_any(conn, end_query)
    base_graph = adjacency if adjacency is not None else load_adjacency(conn)
    virtual_edges: dict[str, dict[str, Any]] = {}
    virtual_overlay: dict[str, list[Edge]] = {}
    start_node = add_virtual_snap_node(virtual_overlay, virtual_edges, conn, start, "start")
    end_node = add_virtual_snap_node(virtual_overlay, virtual_edges, conn, end, "end")
    virtual_adjacency: VirtualAdjacencyMap = {node_id: tuple(edges) for node_id, edges in virtual_overlay.items()}
    direct_distance_m = haversine_m((start.lon, start.lat), (end.lon, end.lat))
    route_info: dict[str, Any] = {"routing_strategy": "single_graph"}
    if direct_distance_m <= DIRECT_WALK_LIMIT_M:
        cost, steps = dijkstra(
            base_graph,
            start_node["node_id"],
            end_node["node_id"],
            allowed_edge_types=set(WALK_LIKE_EDGE_TYPES),
            apply_long_walk_penalty=False,
            apply_transfer_penalty=False,
            virtual_adjacency=virtual_adjacency,
        )
        route_info["routing_strategy"] = "direct_walk"
    else:
        station_route = candidate_subway_route(
            conn,
            base_graph,
            start,
            end,
            start_node,
            end_node,
            virtual_adjacency=virtual_adjacency,
        )
        if station_route:
            cost, steps, route_info = station_route
        else:
            cost, steps = dijkstra(
                base_graph,
                start_node["node_id"],
                end_node["node_id"],
                virtual_adjacency=virtual_adjacency,
            )
    features = fetch_edges(conn, steps, virtual_edges)
    summary = summarize_route(features, cost, start, end)
    summary["start_snap_node"] = start_node
    summary["end_snap_node"] = end_node
    summary["direct_distance_m"] = round(direct_distance_m, 1)
    summary.update(route_info)
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
