from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..schemas import RouteCreateRequest, RouteResponse
from ..services import RoutingService


router = APIRouter(prefix="/api/v1/routes", tags=["routes"])


def service_from(request: Request) -> RoutingService:
    return request.app.state.routing_service


@router.post("", response_model=RouteResponse)
def create_route(payload: RouteCreateRequest, request: Request) -> RouteResponse:
    try:
        return service_from(request).create_route(payload)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
