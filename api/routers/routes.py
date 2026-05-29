from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ..schemas import RouteCreateRequest, RouteResponse
from ..services import RoutingService


router = APIRouter(prefix="/api/v1/routes", tags=["routes"])
logger = logging.getLogger("ieum.api.routes")


def service_from(request: Request) -> RoutingService:
    return request.app.state.routing_service


@router.post("", response_model=RouteResponse)
def create_route(payload: RouteCreateRequest, request: Request) -> RouteResponse:
    try:
        logger.info("routes.create payload=%s", payload.model_dump())
        return service_from(request).create_route(payload)
    except (RuntimeError, ValueError) as exc:
        logger.warning("routes.create failed detail=%s payload=%s", str(exc), payload.model_dump())
        raise HTTPException(status_code=422, detail=str(exc)) from exc
