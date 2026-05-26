from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def main() -> int:
    """Run the FastAPI application through the former server entrypoint."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    port = int(os.environ.get("IEUM_ROUTE_PORT", "8020"))
    host = os.environ.get("IEUM_ROUTE_HOST", "0.0.0.0")
    uvicorn.run("api.main:app", host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
