from __future__ import annotations

import gzip
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
DATA = ROOT / "data"
OUT = ROOT / "data_gz"
SOURCE = OUT / "source"

FILES = [
    DATA / "ieum_route_graph_nodes.geojson",
    DATA / "ieum_route_graph_edges.geojson",
    DATA / "dataset_manifest.json",
    DATA / "ieum_accessibility_rules.json",
    DATA / "route_test_cases.json",
]


def gzip_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, gzip.open(target, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)


def main() -> int:
    written = []
    for source in FILES:
        if not source.exists():
            continue
        target = OUT / f"{source.name}.gz"
        gzip_file(source, target)
        written.append(
            {
                "file": str(target.relative_to(ROOT)),
                "source_bytes": source.stat().st_size,
                "gzip_bytes": target.stat().st_size,
            }
        )
    for item in written:
        ratio = item["gzip_bytes"] / item["source_bytes"] if item["source_bytes"] else 0
        print(f"{item['file']}: {item['source_bytes']} -> {item['gzip_bytes']} ({ratio:.2%})")
    source_dirs = {
        "bus_station_catalog": WORKSPACE_ROOT / "bus_station_catalog" / "data_gz",
    }
    copied = []
    for name, source_dir in source_dirs.items():
        if not source_dir.exists():
            continue
        target_dir = SOURCE / name
        target_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_dir.glob("*.gz")):
            target = target_dir / source.name
            shutil.copy2(source, target)
            copied.append(str(target.relative_to(ROOT)))
    for item in copied:
        print(f"synced source {item}")
    print(f"compressed {len(written)} files")
    if copied:
        print(f"synced {len(copied)} source files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
