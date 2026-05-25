from __future__ import annotations

import json
import gzip
import math
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
NAV_DATA = WORKSPACE_ROOT / "nav_map" / "web" / "data"
LOCAL_LAYER_GZ = ROOT / "data_gz" / "layers"
DB_PATH = ROOT / "routing" / "ieum_graph.sqlite"
NON_ELEVATOR_STATION_CONNECTOR_PENALTY = 650.0


def haversine_m(left: list[float], right: list[float]) -> float:
    lon1, lat1 = map(math.radians, left)
    lon2, lat2 = map(math.radians, right)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(value))


class PointGrid:
    def __init__(self, items: list[tuple[str, list[float]]], cell: float = 0.0006) -> None:
        self.cell = cell
        self.grid: dict[tuple[int, int], list[tuple[str, list[float]]]] = {}
        for item_id, coord in items:
            key = self.cell_key(coord)
            self.grid.setdefault(key, []).append((item_id, coord))

    def cell_key(self, coord: list[float]) -> tuple[int, int]:
        return (math.floor(coord[0] / self.cell), math.floor(coord[1] / self.cell))

    def nearby_ids(self, coord: list[float], radius_m: float) -> set[str]:
        cx, cy = self.cell_key(coord)
        found: set[str] = set()
        for gx in range(cx - 2, cx + 3):
            for gy in range(cy - 2, cy + 3):
                for item_id, item_coord in self.grid.get((gx, gy), []):
                    if haversine_m(coord, item_coord) <= radius_m:
                        found.add(item_id)
        return found


def read_features(path: Path) -> list[dict[str, Any]]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")).get("features", [])
    gz_path = LOCAL_LAYER_GZ / f"{path.name}.gz"
    if gz_path.exists():
        with gzip.open(gz_path, "rt", encoding="utf-8") as handle:
            return json.load(handle).get("features", [])
    return []


def line_points(geometry: dict[str, Any]) -> list[list[float]]:
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "MultiLineString":
        coords = [point for line in coords for point in line]
    if not coords:
        return []
    points = [coords[0], coords[-1]]
    if len(coords) > 2:
        points.append(coords[len(coords) // 2])
    for left, right in zip(coords, coords[1:]):
        points.append([(left[0] + right[0]) / 2, (left[1] + right[1]) / 2])
    return points


def first_id_value(props: dict[str, Any]) -> Any:
    for key, value in props.items():
        if str(key).endswith(" ID"):
            return value
    return None


def build_context_indexes() -> dict[str, PointGrid]:
    braille_items = []
    for feature in read_features(NAV_DATA / "braille_network_links.geojson"):
        props = feature.get("properties") or {}
        item_id = f"braille:{props.get('braille_link_id')}"
        for point in line_points(feature.get("geometry") or {}):
            braille_items.append((item_id, point))

    crosswalk_items = []
    for feature in read_features(NAV_DATA / "crosswalk_links_enriched.geojson"):
        props = feature.get("properties") or {}
        item_id = f"crosswalk:{first_id_value(props)}"
        for point in line_points(feature.get("geometry") or {}):
            crosswalk_items.append((item_id, point))

    audible_items = []
    for feature in read_features(NAV_DATA / "audible_signal_points.geojson"):
        props = feature.get("properties") or {}
        coord = (feature.get("geometry") or {}).get("coordinates")
        if coord:
            audible_items.append((f"audible:{props.get('MGRNU')}", coord))

    return {
        "braille": PointGrid(braille_items),
        "crosswalk": PointGrid(crosswalk_items),
        "audible": PointGrid(audible_items),
    }


def accessibility_weight(
    length_m: float,
    edge_type: str,
    has_braille: int,
    has_audible_signal: int,
    has_ped_signal: int,
    has_elevator: int,
    near_braille_count: int,
    near_crosswalk_count: int,
    near_audible_signal_count: int,
    station_has_elevator: int,
    station_has_indoor_route: int,
    data_confidence: str | None,
) -> float:
    weight = length_m
    if edge_type == "subway_ride":
        weight *= 0.50
    elif edge_type == "subway_connector":
        if has_elevator:
            weight *= 0.20
        elif station_has_elevator:
            weight = weight * 1.80 + NON_ELEVATOR_STATION_CONNECTOR_PENALTY
        else:
            weight *= 1.15
        if station_has_indoor_route:
            weight *= 0.85
    elif edge_type == "facility_connector" and has_elevator:
        weight *= 0.30
    elif edge_type == "braille_walk" or has_braille or near_braille_count:
        weight *= 0.68
    elif edge_type == "walk" and near_audible_signal_count:
        weight *= 0.82

    if edge_type == "crosswalk":
        if has_ped_signal:
            weight *= 0.85
        else:
            weight *= 1.80
        if has_audible_signal:
            weight *= 0.70
        else:
            weight *= 1.30

    if edge_type == "walk" and near_crosswalk_count and not near_audible_signal_count:
        weight *= 1.08
    if data_confidence == "low":
        weight *= 1.25
    return round(weight, 3)


def ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
    columns = {
        "near_braille_count": "INTEGER NOT NULL DEFAULT 0",
        "near_crosswalk_count": "INTEGER NOT NULL DEFAULT 0",
        "near_audible_signal_count": "INTEGER NOT NULL DEFAULT 0",
        "accessibility_enriched": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE edges ADD COLUMN {name} {definition}")


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "있음", "유", "o"}


def subway_station_flags(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    flags: dict[str, dict[str, int]] = {}
    rows = conn.execute(
        "SELECT node_id, raw_properties FROM nodes WHERE node_type = 'subway_station'"
    ).fetchall()
    for node_id, raw_properties in rows:
        props = json.loads(raw_properties)
        flags[node_id] = {
            "has_elevator": int(truthy(props.get("has_elevator"))),
            "has_indoor_route": int(truthy(props.get("has_indoor_route"))),
        }
    return flags


def edge_station_flags(
    station_flags: dict[str, dict[str, int]],
    from_node_id: str,
    to_node_id: str,
) -> tuple[int, int]:
    flags = []
    for node_id in (from_node_id, to_node_id):
        if node_id in station_flags:
            flags.append(station_flags[node_id])
    if not flags:
        return 0, 0
    return (
        int(any(flag["has_elevator"] for flag in flags)),
        int(any(flag["has_indoor_route"] for flag in flags)),
    )


def main() -> int:
    indexes = build_context_indexes()
    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_columns(conn)
        station_flags = subway_station_flags(conn)
        rows = conn.execute(
            """
            SELECT edge_id, edge_type, length_m, has_braille, has_audible_signal,
                   has_ped_signal, has_elevator, data_confidence, geometry,
                   from_node_id, to_node_id
            FROM edges
            """
        ).fetchall()
        updates = []
        for idx, row in enumerate(rows, start=1):
            (
                edge_id,
                edge_type,
                length_m,
                has_braille,
                has_audible,
                has_ped,
                has_elevator,
                confidence,
                geometry_text,
                from_node_id,
                to_node_id,
            ) = row
            station_has_elevator, station_has_indoor_route = edge_station_flags(
                station_flags,
                str(from_node_id),
                str(to_node_id),
            )
            near_braille = near_crosswalk = near_audible = 0
            if edge_type in {"walk", "crosswalk", "facility_connector", "subway_connector"}:
                geometry = json.loads(geometry_text)
                points = line_points(geometry)
                braille_ids: set[str] = set()
                crosswalk_ids: set[str] = set()
                audible_ids: set[str] = set()
                for point in points:
                    braille_ids.update(indexes["braille"].nearby_ids(point, 25))
                    crosswalk_ids.update(indexes["crosswalk"].nearby_ids(point, 20))
                    audible_ids.update(indexes["audible"].nearby_ids(point, 35))
                near_braille = len(braille_ids)
                near_crosswalk = len(crosswalk_ids)
                near_audible = len(audible_ids)

            new_weight = accessibility_weight(
                float(length_m or 0),
                str(edge_type or ""),
                int(has_braille or 0),
                int(has_audible or 0),
                int(has_ped or 0),
                int(has_elevator or 0),
                near_braille,
                near_crosswalk,
                near_audible,
                station_has_elevator,
                station_has_indoor_route,
                confidence,
            )
            updates.append((new_weight, near_braille, near_crosswalk, near_audible, edge_id))
            if idx % 50000 == 0:
                print(f"processed {idx}/{len(rows)}")

        conn.executemany(
            """
            UPDATE edges
            SET visual_impairment_weight = ?,
                near_braille_count = ?,
                near_crosswalk_count = ?,
                near_audible_signal_count = ?,
                accessibility_enriched = 1
            WHERE edge_id = ?
            """,
            updates,
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("accessibility_enriched", "true"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("subway_elevator_global_priority", "true"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            ("subway_indoor_route_weighting", "true"),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"enriched {len(updates)} edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
