from __future__ import annotations

import re
import time
import os
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile
import edge_tts
from pywhispercpp.model import Model
from google import genai
from google.genai import types
from dotenv import load_dotenv

import gzip
import json
import sqlite3
import subprocess
import sys
from threading import RLock
from typing import Any
from uuid import uuid4

from .schemas import LocationInput, RouteCreateRequest, RouteLeg, RouteResponse, RouteStep

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
ROUTING_PATH = ROOT / "routing"
WORKSPACE_ROOT = ROOT.parent
NAV_DATA = WORKSPACE_ROOT / "nav_map" / "web" / "data"
SUBWAY_DATA = WORKSPACE_ROOT / "subway_station_catalog" / "web" / "data"
LOCAL_LAYER_GZ = ROOT / "data_gz" / "layers"
sys.path.append(str(ROUTING_PATH))

import route_engine  # noqa: E402
import route_instructions  # noqa: E402


DATASET_FILES = {
    "braille": (NAV_DATA / "braille_network_links.geojson", LOCAL_LAYER_GZ / "braille_network_links.geojson.gz"),
    "crosswalk": (NAV_DATA / "crosswalk_links_enriched.geojson", LOCAL_LAYER_GZ / "crosswalk_links_enriched.geojson.gz"),
    "audible": (NAV_DATA / "audible_signal_points.geojson", LOCAL_LAYER_GZ / "audible_signal_points.geojson.gz"),
    "subway_elevator": (NAV_DATA / "subway_elevators.geojson", LOCAL_LAYER_GZ / "subway_elevators.geojson.gz"),
    "subway_station": (SUBWAY_DATA / "merged_station_points.geojson", LOCAL_LAYER_GZ / "merged_station_points.geojson.gz"),
    "subway_line": (SUBWAY_DATA / "line_segments_display.geojson", LOCAL_LAYER_GZ / "line_segments_display.geojson.gz"),
}


def ensure_runtime_db() -> None:
    if route_engine.DB_PATH.exists():
        return
    subprocess.run([sys.executable, str(ROUTING_PATH / "build_sqlite_graph.py")], cwd=str(ROOT), check=True)
    subprocess.run([sys.executable, str(ROUTING_PATH / "enrich_sqlite_accessibility.py")], cwd=str(ROOT), check=True)


def _leg_type(edge_type: str, passed_subway: bool, has_subway_ahead: bool) -> str:
    if edge_type == "subway_ride":
        return "subway_ride"
    if edge_type == "subway_connector":
        return "station_exit" if passed_subway and not has_subway_ahead else "station_entry"
    return "outdoor_walk"


def _accessibility_tags(features: list[dict[str, Any]]) -> list[str]:
    tags: set[str] = set()
    for feature in features:
        props = feature.get("properties") or {}
        if props.get("has_braille") or int(props.get("near_braille_count") or 0):
            tags.add("braille")
        if props.get("has_audible_signal") or int(props.get("near_audible_signal_count") or 0):
            tags.add("audible_signal")
        if props.get("has_elevator"):
            tags.add("elevator")
        if props.get("edge_type") == "crosswalk":
            tags.add("crosswalk")
    return sorted(tags)


def build_legs(features: list[dict[str, Any]]) -> list[RouteLeg]:
    legs: list[RouteLeg] = []
    passed_subway = False
    for index, feature in enumerate(features):
        edge_type = str((feature.get("properties") or {}).get("edge_type") or "unknown")
        has_subway_ahead = any(
            (remaining.get("properties") or {}).get("edge_type") == "subway_ride"
            for remaining in features[index + 1 :]
        )
        current_type = _leg_type(edge_type, passed_subway, has_subway_ahead)
        if edge_type == "subway_ride":
            passed_subway = True
        if legs and legs[-1].type == current_type:
            existing = legs[-1]
            appended = existing.geometry["features"] + [feature]
            legs[-1] = RouteLeg(
                type=existing.type,
                distance_m=round(existing.distance_m + float((feature.get("properties") or {}).get("length_m") or 0), 1),
                edge_count=existing.edge_count + 1,
                geometry={"type": "FeatureCollection", "features": appended},
                accessibility=_accessibility_tags(appended),
            )
            continue
        legs.append(
            RouteLeg(
                type=current_type,
                distance_m=round(float((feature.get("properties") or {}).get("length_m") or 0), 1),
                edge_count=1,
                geometry={"type": "FeatureCollection", "features": [feature]},
                accessibility=_accessibility_tags([feature]),
            )
        )
    return legs


class RoutingService:
    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None
        self.adjacency: dict[str, list[Any]] | None = None
        self.lock = RLock()

    def open(self) -> None:
        ensure_runtime_db()
        self.conn = sqlite3.connect(route_engine.DB_PATH, check_same_thread=False)
        self.adjacency = route_engine.load_adjacency(self.conn)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.conn = None
        self.adjacency = None

    def create_route(self, request: RouteCreateRequest) -> RouteResponse:
        if request.profile != "visual_impairment_default":
            raise ValueError(f"unsupported profile: {request.profile}")
        if self.conn is None or self.adjacency is None:
            raise RuntimeError("routing service is not ready")
        with self.lock:
            route = route_engine.build_route_geojson(
                self.conn,
                request.origin.engine_query(),
                request.destination.engine_query(),
                self.adjacency,
            )
        steps = [RouteStep.model_validate(item) for item in route.get("instructions") or []]
        features = route.get("features") or []
        return RouteResponse(
            route_id=f"route_{uuid4().hex}",
            profile=request.profile,
            summary=route.get("properties") or {},
            geometry={"type": "FeatureCollection", "features": features},
            instructions=steps,
            legs=build_legs(features),
        )

    def create_legacy_route(self, start: str, end: str) -> dict[str, Any]:
        request = RouteCreateRequest(
            origin=LocationInput(query=start),
            destination=LocationInput(query=end),
        )
        response = self.create_route(request)
        return {
            "type": "FeatureCollection",
            "properties": response.summary,
            "instructions": [step.model_dump(exclude_none=True) for step in response.instructions],
            "features": response.geometry["features"],
        }
    
# 1. 새 방식의 클라이언트 초기화
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# 2. 시스템 프롬프트 및 설정 객체 생성
gemini_config = types.GenerateContentConfig(
    system_instruction=(
        "사용자의 텍스트에서 가고자 하는 '최종 목적지'의 명칭만 정확하게 추출해. "
        "조사(로, 으로, 까지 등), 서술어(가줘, 안내해 등), 수식어는 절대 포함하지 마. "
        "오직 장소 이름만 단답형으로 출력해. (예: '서울역으로 가줘' -> '서울역') "
        "목적지가 명확하지 않거나 없으면 'None'이라고 출력해."
        "실제로 존재하는 장소를 반환해야해 장소가 정확하지 않더라도 실제로 네이버나 카카오 지도에서 검색 가능한 이름을 반환해."
    ),
    temperature=0.0,
    max_output_tokens=20,
)

class VoiceService:
    def __init__(self) -> None:
        self.whisper_model: Model | None = None

    def open(self) -> None:
        self.whisper_model = Model(
            "small",
            n_threads=max(1, os.cpu_count() or 1),
            print_realtime=False,
            print_progress=False,
        )

    def transcribe(
        self,
        audio_data: bytes,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        if not self.whisper_model:
            raise RuntimeError("voice service is not ready")
        suffix = ".webm"
        if filename:
            suffix = Path(filename).suffix or suffix
        elif content_type == "audio/wav":
            suffix = ".wav"

        with NamedTemporaryFile(suffix=suffix) as temp:
            temp.write(audio_data)
            temp.flush()
            try:
                segments = self.whisper_model.transcribe(temp.name, language="ko")
                return " ".join(segment.text.strip() for segment in segments).strip()
            except Exception as exc:
                message = str(exc)
                if "FFMPEG is not installed or not in PATH" in message:
                    raise ValueError("audio decode failed: ffmpeg is required for this audio format") from exc
                raise ValueError("audio decode failed") from exc

    # 3. 비동기 호출 방식 변경 (gemini_model.generate_content_async -> client.aio.models.generate_content)
    async def extract_destination(self, text: str) -> str:
        if not text.strip():
            return ""
        try:
            response = await gemini_client.aio.models.generate_content(
                model='gemini-2.5-flash',
                contents=text,
                config=gemini_config
            )
            extracted = response.text.strip()
            return "" if extracted.lower() == "none" or not extracted else extracted
        except Exception as exc:
            print(f"Gemini API Error: {exc}")
            return ""

    def build_prompt(self, destination: str) -> str:
        if not destination:
            return "목적지를 인식하지 못했습니다. 다시 말씀해주세요."
        return f"목적지는 {destination} 입니다. 맞으면 화면을 두번, 틀리면 세번 터치해주세요."

    async def make_tts(self, text: str) -> bytes:
        # (기존 코드와 동일)
        audio = bytearray()
        communicate = edge_tts.Communicate(text, "ko-KR-SunHiNeural")
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
        return bytes(audio)

def load_dataset(name: str) -> dict[str, Any]:
    candidates = DATASET_FILES.get(name)
    if candidates is None:
        raise KeyError(name)
    for path in candidates:
        if not path.exists():
            continue
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(name)
