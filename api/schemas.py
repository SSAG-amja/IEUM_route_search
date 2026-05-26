from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class Coordinate(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LocationInput(BaseModel):
    query: str | None = None
    coordinate: Coordinate | None = None
    label: str | None = None

    @model_validator(mode="after")
    def validate_location(self) -> "LocationInput":
        if not (self.query and self.query.strip()) and self.coordinate is None:
            raise ValueError("query or coordinate is required")
        return self

    def engine_query(self) -> str:
        if self.coordinate is not None:
            return f"{self.coordinate.longitude},{self.coordinate.latitude}"
        return str(self.query).strip()


class RouteCreateRequest(BaseModel):
    origin: LocationInput
    destination: LocationInput
    profile: str = "visual_impairment_default"


class RouteStep(BaseModel):
    type: str
    text: str
    distance_m: float | None = None
    direction: str | None = None
    station_name: str | None = None
    line_code: str | None = None
    from_station: str | None = None
    to_station: str | None = None
    segment_count: int | None = None


class RouteLeg(BaseModel):
    type: str
    distance_m: float
    edge_count: int
    geometry: dict[str, Any]
    accessibility: list[str]


class RouteResponse(BaseModel):
    route_id: str
    profile: str
    summary: dict[str, Any]
    geometry: dict[str, Any]
    instructions: list[RouteStep]
    legs: list[RouteLeg]

class TextBody(BaseModel):
    text: str

class VoiceAnalysisResponse(BaseModel):
    text: str         # STT로 변환된 원본 텍스트 (화면 표시용)
    destination: str  # Gemini가 추출한 최종 목적지
    prompt: str       # TTS나 화면에 띄울 안내 멘트