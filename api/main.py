from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .routers import routes, support, voice
from .services import RoutingService, VoiceService



ROOT = Path(__file__).resolve().parents[1]
DEMO_WEB = ROOT / "demo" / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    routing_service = RoutingService()
    routing_service.open()
    app.state.routing_service = routing_service

    voic_service = VoiceService()
    voic_service.open()
    app.state.voice_service = voic_service
    yield
    routing_service.close()


app = FastAPI(
    title="IEUM Accessibility Routing API",
    version="1.0.0",
    lifespan=lifespan,
)

origins = os.environ.get(
    "IEUM_CORS_ORIGINS",
    "http://localhost:8081,http://localhost:19006,http://127.0.0.1:8081,http://127.0.0.1:19006",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in origins if origin.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes.router)
app.include_router(support.router)
app.include_router(voice.router)

if DEMO_WEB.exists():
    app.mount("/demo", StaticFiles(directory=str(DEMO_WEB), html=True), name="demo")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/demo/")


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}
