from __future__ import annotations

import json
import gzip
import os
import sqlite3
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "routing" / "web"
WORKSPACE_ROOT = ROOT.parent
NAV_DATA = WORKSPACE_ROOT / "nav_map" / "web" / "data"
SUBWAY_DATA = WORKSPACE_ROOT / "subway_station_catalog" / "web" / "data"
LOCAL_LAYER_GZ = ROOT / "data_gz" / "layers"
sys.path.append(str(ROOT / "routing"))

import route_engine  # noqa: E402
import route_instructions  # noqa: E402


def ensure_runtime_db() -> None:
    if route_engine.DB_PATH.exists():
        return
    print("routing/ieum_graph.sqlite not found; building from data_gz...")
    subprocess.run([sys.executable, str(ROOT / "routing" / "build_sqlite_graph.py")], cwd=str(ROOT), check=True)
    subprocess.run([sys.executable, str(ROOT / "routing" / "enrich_sqlite_accessibility.py")], cwd=str(ROOT), check=True)


class RouteApp:
    def __init__(self) -> None:
        ensure_runtime_db()
        self.conn = sqlite3.connect(route_engine.DB_PATH, check_same_thread=False)
        print("loading route graph adjacency...")
        self.adjacency = route_engine.load_adjacency(self.conn)
        print(f"loaded adjacency nodes={len(self.adjacency)}")

    def route(self, start: str, end: str) -> dict:
        return route_engine.build_route_geojson(self.conn, start, end, self.adjacency)


APP = RouteApp()

DATASET_FILES = {
    "braille": (NAV_DATA / "braille_network_links.geojson", LOCAL_LAYER_GZ / "braille_network_links.geojson.gz"),
    "crosswalk": (NAV_DATA / "crosswalk_links_enriched.geojson", LOCAL_LAYER_GZ / "crosswalk_links_enriched.geojson.gz"),
    "audible": (NAV_DATA / "audible_signal_points.geojson", LOCAL_LAYER_GZ / "audible_signal_points.geojson.gz"),
    "subway_elevator": (NAV_DATA / "subway_elevators.geojson", LOCAL_LAYER_GZ / "subway_elevators.geojson.gz"),
    "subway_station": (SUBWAY_DATA / "merged_station_points.geojson", LOCAL_LAYER_GZ / "merged_station_points.geojson.gz"),
    "subway_line": (SUBWAY_DATA / "line_segments_display.geojson", LOCAL_LAYER_GZ / "line_segments_display.geojson.gz"),
}


def existing_dataset_path(candidates: tuple[Path, Path]) -> Path | None:
    for path in candidates:
        if path.exists():
            return path
    return None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file_json(self, path: Path | None) -> None:
        if path is None:
            self.send_json(404, {"error": "dataset not found"})
            return
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as handle:
                body = handle.read()
        else:
            body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/geo+json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/dataset":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0].strip()
            path = DATASET_FILES.get(name)
            if not path:
                self.send_json(400, {"error": f"unknown dataset: {name}"})
                return
            self.send_file_json(existing_dataset_path(path))
            return
        if parsed.path == "/api/instruction-templates":
            self.send_json(200, route_instructions.template_payload())
            return
        if parsed.path != "/api/route":
            return super().do_GET()
        params = parse_qs(parsed.query)
        start = (params.get("start") or [""])[0].strip()
        end = (params.get("end") or [""])[0].strip()
        if not start or not end:
            self.send_json(400, {"error": "start and end are required"})
            return
        try:
            route = APP.route(start, end)
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})
            return
        self.send_json(200, route)


def main() -> int:
    port = int(os.environ.get("IEUM_ROUTE_PORT", "8020"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"IEUM route demo: http://localhost:{port}/")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
