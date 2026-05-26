from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..services import RoutingService, load_dataset, route_instructions


router = APIRouter(tags=["support"])


@router.get("/api/v1/datasets/{name}")
def dataset(name: str) -> dict[str, Any]:
    try:
        return load_dataset(name)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=f"unknown dataset: {name}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"dataset not found: {name}") from exc


@router.get("/api/v1/instruction-templates")
def templates() -> dict[str, Any]:
    return route_instructions.template_payload()


# Compatibility endpoints keep the standalone browser demo useful during migration.
@router.get("/api/route")
def legacy_route(start: str, end: str, request: Request) -> dict[str, Any]:
    service: RoutingService = request.app.state.routing_service
    try:
        return service.create_legacy_route(start, end)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/dataset")
def legacy_dataset(name: str) -> dict[str, Any]:
    return dataset(name)


@router.get("/api/instruction-templates")
def legacy_templates() -> dict[str, Any]:
    return templates()
