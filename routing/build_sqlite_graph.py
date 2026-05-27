from __future__ import annotations

import os
import json
import gzip
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA_GZ = ROOT / "data_gz"
ROUTING = ROOT / "routing"
DB_PATH = Path(os.environ.get("IEUM_ROUTE_DB_PATH", str(ROUTING / "ieum_graph.sqlite"))).expanduser()


def read_geojson(path: Path) -> list[dict[str, Any]]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["features"]
    gz_path = DATA_GZ / f"{path.name}.gz"
    if gz_path.exists():
        with gzip.open(gz_path, "rt", encoding="utf-8") as handle:
            return json.load(handle)["features"]
    raise FileNotFoundError(f"missing dataset: {path} or {gz_path}")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;

        DROP TABLE IF EXISTS nodes;
        DROP TABLE IF EXISTS edges;
        DROP TABLE IF EXISTS metadata;

        CREATE TABLE nodes (
            node_id TEXT PRIMARY KEY,
            node_type TEXT NOT NULL,
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            source TEXT,
            station_name TEXT,
            raw_properties TEXT NOT NULL
        );

        CREATE TABLE edges (
            edge_id TEXT PRIMARY KEY,
            source_edge_id TEXT,
            edge_type TEXT NOT NULL,
            from_node_id TEXT NOT NULL,
            to_node_id TEXT NOT NULL,
            length_m REAL NOT NULL,
            visual_impairment_weight REAL NOT NULL,
            source TEXT,
            line_code TEXT,
            has_braille INTEGER NOT NULL DEFAULT 0,
            has_audible_signal INTEGER NOT NULL DEFAULT 0,
            has_ped_signal INTEGER NOT NULL DEFAULT 0,
            has_elevator INTEGER NOT NULL DEFAULT 0,
            data_confidence TEXT,
            geometry TEXT NOT NULL,
            raw_properties TEXT NOT NULL
        );

        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX idx_nodes_type ON nodes(node_type);
        CREATE INDEX idx_nodes_lon_lat ON nodes(lon, lat);
        CREATE INDEX idx_edges_from ON edges(from_node_id);
        CREATE INDEX idx_edges_to ON edges(to_node_id);
        CREATE INDEX idx_edges_type ON edges(edge_type);
        CREATE INDEX idx_edges_line ON edges(line_code);
        """
    )


def bool_int(value: Any) -> int:
    return 1 if value is True or str(value).lower() in {"true", "1", "y", "yes"} else 0


def main() -> int:
    ROUTING.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    nodes = read_geojson(DATA / "ieum_route_graph_nodes.geojson")
    edges = read_geojson(DATA / "ieum_route_graph_edges.geojson")

    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)

        conn.executemany(
            """
            INSERT INTO nodes (
                node_id, node_type, lon, lat, source, station_name, raw_properties
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    props["node_id"],
                    props.get("node_type") or "unknown",
                    float(feature["geometry"]["coordinates"][0]),
                    float(feature["geometry"]["coordinates"][1]),
                    props.get("source"),
                    props.get("station_name"),
                    json.dumps(props, ensure_ascii=False, sort_keys=True),
                )
                for feature in nodes
                for props in [feature.get("properties") or {}]
            ],
        )

        conn.executemany(
            """
            INSERT INTO edges (
                edge_id, source_edge_id, edge_type, from_node_id, to_node_id, length_m,
                visual_impairment_weight, source, line_code, has_braille,
                has_audible_signal, has_ped_signal, has_elevator, data_confidence,
                geometry, raw_properties
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"edge:{idx:07d}",
                    props.get("edge_id"),
                    props.get("edge_type") or "unknown",
                    props["from_node_id"],
                    props["to_node_id"],
                    float(props.get("length_m") or 0),
                    float(props.get("visual_impairment_weight") or props.get("length_m") or 0),
                    props.get("source"),
                    props.get("line_code"),
                    bool_int(props.get("has_braille")),
                    bool_int(props.get("has_audible_signal")),
                    bool_int(props.get("has_ped_signal")),
                    bool_int(props.get("has_elevator")),
                    props.get("data_confidence"),
                    json.dumps(feature.get("geometry") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(props, ensure_ascii=False, sort_keys=True),
                )
                for idx, feature in enumerate(edges, start=1)
                for props in [feature.get("properties") or {}]
            ],
        )

        conn.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            [
                ("node_count", str(len(nodes))),
                ("edge_count", str(len(edges))),
                ("build_source", "ieum_route_graph_nodes.geojson + ieum_route_graph_edges.geojson"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    print(f"created {DB_PATH}")
    print(f"nodes={len(nodes)} edges={len(edges)} bytes={DB_PATH.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
