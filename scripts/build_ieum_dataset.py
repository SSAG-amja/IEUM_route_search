from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parents[1]
OUT_DATA = OUT / "data"

NAV_DATA = ROOT / "nav_map" / "web" / "data"
SUBWAY_DATA = ROOT / "subway_station_catalog" / "web" / "data"


def read_geojson(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def coord_key(coord: list[float] | tuple[float, float], precision: int = 7) -> str:
    return f"{float(coord[0]):.{precision}f},{float(coord[1]):.{precision}f}"


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().replace(" ", "")
    while "(" in text and ")" in text:
        start = text.find("(")
        end = text.find(")", start)
        if end < start:
            break
        text = text[:start] + text[end + 1 :]
    if text.endswith("역"):
        text = text[:-1]
    aliases = {
        "총신대입구": "이수",
        "총신대입구이수": "이수",
        "서울": "서울역",
        "자양": "뚝섬유원지",
    }
    return aliases.get(text, text)


def exact_name_key(value: Any) -> str:
    text = str(value or "").strip().replace(" ", "")
    while "(" in text and ")" in text:
        start = text.find("(")
        end = text.find(")", start)
        if end < start:
            break
        text = text[:start] + text[end + 1 :]
    if text.endswith("역"):
        text = text[:-1]
    return text


def haversine_m(left: list[float] | tuple[float, float], right: list[float] | tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, left)
    lon2, lat2 = map(math.radians, right)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(value))


class SpatialIndex:
    def __init__(self, items: list[tuple[str, list[float]]], cell: float = 0.002) -> None:
        self.cell = cell
        self.grid: dict[tuple[int, int], list[tuple[str, list[float]]]] = defaultdict(list)
        for item_id, coord in items:
            self.grid[self._cell(coord)].append((item_id, coord))

    def _cell(self, coord: list[float]) -> tuple[int, int]:
        return (math.floor(coord[0] / self.cell), math.floor(coord[1] / self.cell))

    def nearest(self, coord: list[float], max_radius_m: float = 250) -> tuple[str, list[float], float] | None:
        cx, cy = self._cell(coord)
        best: tuple[str, list[float], float] | None = None
        for radius in range(0, 4):
            for gx in range(cx - radius, cx + radius + 1):
                for gy in range(cy - radius, cy + radius + 1):
                    for item_id, item_coord in self.grid.get((gx, gy), []):
                        distance = haversine_m(coord, item_coord)
                        if best is None or distance < best[2]:
                            best = (item_id, item_coord, distance)
            if best and best[2] <= max_radius_m:
                return best
        return best if best and best[2] <= max_radius_m else None


def feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def point_feature(node_id: str, coord: list[float], props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coord},
        "properties": {"node_id": node_id, **props},
    }


def line_feature(edge_id: str, coords: list[list[float]], props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {"edge_id": edge_id, **props},
    }


def length_of_line(coords: list[list[float]]) -> float:
    return sum(haversine_m(left, right) for left, right in zip(coords, coords[1:]))


def as_linestring_coords(geometry: dict[str, Any]) -> list[list[float]]:
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "LineString":
        return coords
    if geometry.get("type") == "MultiLineString" and coords:
        return [point for line in coords for point in line]
    if coords and isinstance(coords[0], list) and coords[0] and isinstance(coords[0][0], list):
        return [point for line in coords for point in line]
    return coords


def visual_weight(base_m: float, flags: dict[str, Any]) -> float:
    weight = base_m
    if flags.get("has_braille"):
        weight *= 0.70
    if flags.get("has_audible_signal"):
        weight *= 0.75
    if flags.get("is_crosswalk") and not flags.get("has_ped_signal"):
        weight *= 1.80
    if flags.get("is_crosswalk") and not flags.get("has_audible_signal"):
        weight *= 1.30
    if flags.get("is_subway_internal"):
        weight *= 0.85
    if flags.get("is_subway_ride"):
        weight *= 0.50
    if flags.get("data_confidence") == "low":
        weight *= 1.25
    return round(weight, 3)


def main() -> int:
    OUT_DATA.mkdir(parents=True, exist_ok=True)

    walk_nodes = read_geojson(NAV_DATA / "walk_nodes.geojson")["features"]
    walk_edges = read_geojson(NAV_DATA / "walk_network.geojson")["features"]
    braille_nodes = read_geojson(NAV_DATA / "braille_network_nodes.geojson")["features"]
    braille_edges = read_geojson(NAV_DATA / "braille_network_links.geojson")["features"]
    crosswalk_edges = read_geojson(NAV_DATA / "crosswalk_links_enriched.geojson")["features"]
    audible_points = read_geojson(NAV_DATA / "audible_signal_points.geojson")["features"]
    nav_subway_elevators = read_geojson(NAV_DATA / "subway_elevators.geojson")["features"]
    subway_points = read_geojson(SUBWAY_DATA / "merged_station_points.geojson")["features"]
    subway_segments = read_geojson(SUBWAY_DATA / "line_segments_display.geojson")["features"]

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_coords: dict[str, list[float]] = {}

    def add_node(node_id: str, coord: list[float], props: dict[str, Any]) -> None:
        if node_id in node_coords:
            return
        node_coords[node_id] = coord
        nodes.append(point_feature(node_id, coord, props))

    def add_edge(edge_id: str, from_id: str, to_id: str, coords: list[list[float]], props: dict[str, Any]) -> None:
        length_m = props.get("length_m")
        if length_m is None:
            length_m = length_of_line(coords)
        flags = {
            "has_braille": props.get("has_braille") is True,
            "has_audible_signal": props.get("has_audible_signal") is True,
            "has_ped_signal": props.get("has_ped_signal") is True,
            "is_crosswalk": props.get("edge_type") == "crosswalk",
            "is_subway_internal": props.get("edge_type") == "subway_connector",
            "is_subway_ride": props.get("edge_type") == "subway_ride",
            "data_confidence": props.get("data_confidence"),
        }
        edges.append(
            line_feature(
                edge_id,
                coords,
                {
                    "from_node_id": from_id,
                    "to_node_id": to_id,
                    "length_m": round(float(length_m), 3),
                    "visual_impairment_weight": visual_weight(float(length_m), flags),
                    **props,
                },
            )
        )

    for feature in walk_nodes:
        props = feature.get("properties") or {}
        coord = feature["geometry"]["coordinates"]
        node_id = f"walk:{props.get('노드 ID')}"
        add_node(
            node_id,
            coord,
            {
                "node_type": "walk_node",
                "source": "nav_map.walk_nodes",
                "district": props.get("시군구명"),
                "dong": props.get("읍면동명"),
                "raw_node_id": props.get("노드 ID"),
            },
        )

    for idx, feature in enumerate(walk_edges, start=1):
        props = feature.get("properties") or {}
        coords = as_linestring_coords(feature["geometry"])
        from_id = f"walk_geom:{coord_key(coords[0])}"
        to_id = f"walk_geom:{coord_key(coords[-1])}"
        add_node(from_id, coords[0], {"node_type": "walk_geometry_endpoint", "source": "nav_map.walk_network"})
        add_node(to_id, coords[-1], {"node_type": "walk_geometry_endpoint", "source": "nav_map.walk_network"})
        length_m = float(props.get("링크 길이") or length_of_line(coords))
        add_edge(
            f"walk:{props.get('링크 ID') or idx}",
            from_id,
            to_id,
            coords,
            {
                "edge_type": "walk",
                "source": "nav_map.walk_network",
                "length_m": length_m,
                "district": props.get("시군구명"),
                "dong": props.get("읍면동명"),
                "is_crosswalk": bool(props.get("횡단보도")),
                "data_confidence": "medium",
            },
        )

    walk_route_index = SpatialIndex(
        [
            (node["properties"]["node_id"], node["geometry"]["coordinates"])
            for node in nodes
            if node["properties"]["node_type"] == "walk_geometry_endpoint"
        ]
    )

    braille_index_items: list[tuple[str, list[float]]] = []
    for feature in braille_nodes:
        props = feature.get("properties") or {}
        coord = feature["geometry"]["coordinates"]
        node_id = f"braille:{props.get('braille_node_id')}"
        braille_index_items.append((node_id, coord))
        add_node(
            node_id,
            coord,
            {
                "node_type": "braille_node",
                "source": "nav_map.braille_network_nodes",
                "raw_node_id": props.get("braille_node_id"),
                "degree": props.get("degree"),
            },
        )

    for feature in braille_edges:
        props = feature.get("properties") or {}
        coords = as_linestring_coords(feature["geometry"])
        from_id = f"braille:{props.get('from_braille_node_id')}"
        to_id = f"braille:{props.get('to_braille_node_id')}"
        add_edge(
            f"braille:{props.get('braille_link_id')}",
            from_id,
            to_id,
            coords,
            {
                "edge_type": "braille_walk",
                "source": "nav_map.braille_network_links",
                "has_braille": bool(props.get("has_braille")),
                "braille_link_score": props.get("braille_link_score"),
                "braille_link_confidence": props.get("braille_link_confidence"),
                "data_confidence": props.get("braille_link_confidence") or "medium",
            },
        )

    for idx, feature in enumerate(crosswalk_edges, start=1):
        props = feature.get("properties") or {}
        coords = as_linestring_coords(feature["geometry"])
        from_id = f"crosswalk_endpoint:{coord_key(coords[0])}"
        to_id = f"crosswalk_endpoint:{coord_key(coords[-1])}"
        add_node(from_id, coords[0], {"node_type": "crosswalk_endpoint", "source": "nav_map.crosswalk_links_enriched"})
        add_node(to_id, coords[-1], {"node_type": "crosswalk_endpoint", "source": "nav_map.crosswalk_links_enriched"})
        for endpoint_id, endpoint_coord in [(from_id, coords[0]), (to_id, coords[-1])]:
            nearest = walk_route_index.nearest(endpoint_coord, max_radius_m=30)
            if nearest:
                add_edge(
                    f"connector:crosswalk_walk:{endpoint_id}",
                    endpoint_id,
                    nearest[0],
                    [endpoint_coord, nearest[1]],
                    {
                        "edge_type": "crosswalk_connector",
                        "source": "generated.nearest_walk_geometry_endpoint",
                        "length_m": nearest[2],
                        "data_confidence": "medium",
                    },
                )
        has_audible = str(props.get("음향신호기설치여부") or "").strip() in {"Y", "1", "있음", "유"}
        has_ped_signal = str(props.get("보행등유무") or "").strip() in {"Y", "1", "있음", "유"}
        length_m = float(str(props.get("링크 길이") or length_of_line(coords)).replace(",", ""))
        add_edge(
            f"crosswalk:{props.get('링크 ID') or idx}",
            from_id,
            to_id,
            coords,
            {
                "edge_type": "crosswalk",
                "source": "nav_map.crosswalk_links_enriched",
                "length_m": length_m,
                "has_audible_signal": has_audible,
                "has_ped_signal": has_ped_signal,
                "crosswalk_type": props.get("횡단보도종류"),
                "data_confidence": "high",
            },
        )

    for idx, feature in enumerate(audible_points, start=1):
        props = feature.get("properties") or {}
        coord = feature["geometry"]["coordinates"]
        node_id = f"audible:{props.get('MGRNU') or idx}"
        add_node(
            node_id,
            coord,
            {
                "node_type": "audible_signal",
                "source": "nav_map.audible_signal_points",
                "manager_id": props.get("MGRNU"),
                "status_code": props.get("STAT_CDE"),
            },
        )
        nearest = walk_route_index.nearest(coord, max_radius_m=80)
        if nearest:
            add_edge(
                f"connector:audible_walk:{node_id}",
                node_id,
                nearest[0],
                [coord, nearest[1]],
                {
                    "edge_type": "facility_connector",
                    "source": "generated.nearest_walk_geometry_endpoint",
                    "length_m": nearest[2],
                    "has_audible_signal": True,
                    "data_confidence": "medium",
                },
            )

    subway_by_name: dict[str, list[tuple[str, list[float]]]] = defaultdict(list)
    for idx, feature in enumerate(subway_points, start=1):
        props = feature.get("properties") or {}
        coord = feature["geometry"]["coordinates"]
        exact_key = exact_name_key(props.get("station_name"))
        name_key = normalize_name(props.get("station_name"))
        node_id = f"subway_station:{idx:04d}:{exact_key}"
        subway_by_name[exact_key].append((node_id, coord))
        if name_key != exact_key:
            subway_by_name[name_key].append((node_id, coord))
        add_node(
            node_id,
            coord,
            {
                "node_type": "subway_station",
                "source": "subway_station_catalog.merged_station_points",
                "station_name": props.get("station_name"),
                "line_codes": props.get("line_codes"),
                "has_elevator": props.get("has_elevator"),
                "has_indoor_route": props.get("has_indoor_route"),
                "has_disabled_toilet": props.get("has_disabled_toilet"),
                "has_screen_door": props.get("has_screen_door"),
            },
        )
        nearest = walk_route_index.nearest(coord, max_radius_m=200)
        if nearest:
            add_edge(
                f"connector:subway_walk:{node_id}",
                node_id,
                nearest[0],
                [coord, nearest[1]],
                {
                    "edge_type": "subway_connector",
                    "source": "generated.nearest_walk_geometry_endpoint",
                    "length_m": nearest[2],
                    "data_confidence": "medium",
                },
            )

    for idx, feature in enumerate(nav_subway_elevators, start=1):
        props = feature.get("properties") or {}
        coord = feature["geometry"]["coordinates"]
        station_name = props.get("지하철역명")
        exact_key = exact_name_key(station_name)
        name_key = normalize_name(station_name)
        node_id = f"subway_elevator:{props.get('노드 ID') or idx}"
        add_node(
            node_id,
            coord,
            {
                "node_type": "subway_elevator",
                "source": "nav_map.subway_elevators",
                "station_name": station_name,
                "station_name_key": name_key,
                "district": props.get("시군구명"),
                "dong": props.get("읍면동명"),
                "raw_node_id": props.get("노드 ID"),
            },
        )
        nearest = walk_route_index.nearest(coord, max_radius_m=100)
        if nearest:
            add_edge(
                f"connector:elevator_walk:{node_id}",
                node_id,
                nearest[0],
                [coord, nearest[1]],
                {
                    "edge_type": "facility_connector",
                    "source": "generated.nearest_walk_geometry_endpoint",
                    "length_m": nearest[2],
                    "has_elevator": True,
                    "data_confidence": "high",
                },
            )
        station_matches = subway_by_name.get(exact_key) or subway_by_name.get(name_key) or []
        for station_id, station_coord in station_matches:
            distance = haversine_m(coord, station_coord)
            if distance <= 700:
                add_edge(
                    f"connector:station_elevator:{station_id}:{node_id}",
                    station_id,
                    node_id,
                    [station_coord, coord],
                    {
                        "edge_type": "subway_connector",
                        "source": "generated.station_to_nav_map_elevator",
                        "length_m": distance,
                        "has_elevator": True,
                        "data_confidence": "high" if distance <= 250 else "medium",
                    },
                )

    for idx, feature in enumerate(subway_segments, start=1):
        props = feature.get("properties") or {}
        coords = as_linestring_coords(feature["geometry"])
        from_key = exact_name_key(props.get("from_station_name"))
        to_key = exact_name_key(props.get("to_station_name"))
        from_matches = subway_by_name.get(from_key) or subway_by_name.get(normalize_name(props.get("from_station_name"))) or []
        to_matches = subway_by_name.get(to_key) or subway_by_name.get(normalize_name(props.get("to_station_name"))) or []
        if not from_matches or not to_matches:
            continue
        from_id = from_matches[0][0]
        to_id = to_matches[0][0]
        if from_id not in node_coords or to_id not in node_coords:
            continue
        add_edge(
            f"subway_ride:{props.get('line_code')}:{idx}",
            from_id,
            to_id,
            coords,
            {
                "edge_type": "subway_ride",
                "source": "subway_station_catalog.line_segments_display",
                "line_code": props.get("line_code"),
                "line_name": props.get("line_name"),
                "line_color": props.get("line_color"),
                "data_confidence": "high",
            },
        )

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_counts_by_type": dict(sorted(_count_by(nodes, "node_type").items())),
        "edge_counts_by_type": dict(sorted(_count_by(edges, "edge_type").items())),
        "sources": {
            "nav_map": str(NAV_DATA),
            "subway_station_catalog": str(SUBWAY_DATA),
        },
    }

    write_json(OUT_DATA / "ieum_route_graph_nodes.geojson", feature_collection(nodes))
    write_json(OUT_DATA / "ieum_route_graph_edges.geojson", feature_collection(edges))
    write_json(OUT_DATA / "dataset_manifest.json", summary)
    write_json(
        OUT_DATA / "ieum_accessibility_rules.json",
        {
            "profile": "visual_impairment_default",
            "base_cost": "edge length in meters",
            "rules": [
                {"condition": "has_braille", "multiplier": 0.70},
                {"condition": "has_audible_signal", "multiplier": 0.75},
                {"condition": "crosswalk without pedestrian signal", "multiplier": 1.80},
                {"condition": "crosswalk without audible signal", "multiplier": 1.30},
                {"condition": "subway internal connector", "multiplier": 0.85},
                {"condition": "subway ride", "multiplier": 0.50},
                {"condition": "low confidence data", "multiplier": 1.25},
            ],
        },
    )
    write_json(
        OUT_DATA / "route_test_cases.json",
        {
            "test_cases": [
                {
                    "id": "sample_city_hall_to_gangnam",
                    "start": {"label": "시청역", "lon": 126.977088, "lat": 37.565715},
                    "end": {"label": "강남역", "lon": 127.027621, "lat": 37.497952},
                    "expected": "walk/subway graph should snap both points and return a route candidate",
                },
                {
                    "id": "sample_hongdae_to_myeongdong",
                    "start": {"label": "홍대입구역", "lon": 126.923708, "lat": 37.55679},
                    "end": {"label": "명동역", "lon": 126.986325, "lat": 37.560989},
                    "expected": "route can use subway ride edges plus accessible connectors",
                },
            ]
        },
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _count_by(features: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for feature in features:
        counts[str((feature.get("properties") or {}).get(key) or "unknown")] += 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
