from __future__ import annotations

import re
import os
import subprocess
import logging
from pathlib import Path
from google import genai
from google.genai import types
from dotenv import load_dotenv
import gzip
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from threading import RLock
from typing import Any
from uuid import uuid4
import dotenv

from .schemas import LocationInput, RouteCreateRequest, RouteLeg, RouteResponse, RouteStep

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

ROUTING_PATH = ROOT / "routing"
WORKSPACE_ROOT = ROOT.parent
NAV_DATA = WORKSPACE_ROOT / "nav_map" / "web" / "data"
SUBWAY_DATA = WORKSPACE_ROOT / "subway_station_catalog" / "web" / "data"
LOCAL_LAYER_GZ = ROOT / "data_gz" / "layers"
sys.path.append(str(ROUTING_PATH))

import route_engine  # noqa: E402
import route_instructions  # noqa: E402

logger = logging.getLogger("ieum.api.voice")

load_dotenv()

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
    
gemini_api_key = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=gemini_api_key) if gemini_api_key else None

gemini_config = types.GenerateContentConfig(
    system_instruction=(
        "오타나 발음 오류를 실제 장소명이나 주소 1개로 고쳐 써. "
        "조사와 요청말은 빼고 정답만 답해. "
        "예: 항꾺체육대학교->한국체육대학교, 서울때 학교->서울대학교, 올링픽회광->올림픽회관. "
        "모르면 NONE."
    ),
    temperature=0.0,
    max_output_tokens=20,
)


class VoiceService:
    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None

    _leading_noise_pattern = re.compile(r"^(?:음|아|어|저기)\s+")
    _request_suffixes = (
        "안내해줘",
        "안내해주세요",
        "가고 싶어",
        "가고싶어",
        "가줘",
        "가주세요",
        "어디야",
        "알려줘",
        "알려주세요",
        "말해줘",
        "말해주세요",
    )
    _soft_suffixes = ("쪽으로", "근처로", "방면으로")
    _road_suffixes = ("로", "길", "대로", "번길")
    _address_tokens = (
        "시",
        "군",
        "구",
        "읍",
    )

    def open(self) -> None:
        ensure_runtime_db()
        self.conn = sqlite3.connect(route_engine.DB_PATH, check_same_thread=False)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
        self.conn = None

    async def extract_destination(self, text: str) -> str:
        if not text.strip() or self.conn is None:
            return ""
        raw = text.strip()
        logger.info("destination.stage1.start query=%s", raw)
        resolved = self._resolve_candidate(raw)
        logger.info("destination.stage1.end query=%s result=%s", raw, resolved or "")
        if resolved:
            return resolved
        candidate = self._secondary_candidate(raw)
        if candidate and candidate != raw:
            logger.info("destination.stage2.start query=%s", candidate)
            resolved = self._resolve_candidate(candidate)
            logger.info("destination.stage2.end query=%s result=%s", candidate, resolved or "")
            if resolved:
                return resolved
        gemini_seed = candidate or self._normalize_for_search(raw)
        logger.info("destination.stage3.start query=%s", gemini_seed)
        gemini_candidate = await self._extract_with_gemini(gemini_seed)
        if gemini_candidate:
            resolved = self._resolve_candidate(gemini_candidate)
            logger.info("destination.stage3.end query=%s result=%s", gemini_candidate, resolved or "")
            if resolved:
                return resolved
        else:
            logger.info("destination.stage3.end query=%s result=", gemini_seed)
        return ""

    def _normalize_for_search(self, text: str) -> str:
        normalized = re.sub(r"[,.!?]+", " ", text)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        while True:
            updated = self._leading_noise_pattern.sub("", normalized)
            updated = updated.strip()
            if updated == normalized:
                break
            normalized = updated
        for suffix in self._request_suffixes:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)].rstrip()
                break
        for suffix in self._soft_suffixes:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)].rstrip()
                break
        normalized = re.sub(r"(\d[\d-]*)(?:으로|로|까지)$", r"\1", normalized)
        return normalized

    def _secondary_candidate(self, raw: str) -> str:
        primary = self._normalize_for_search(raw)
        stripped = self._strip_particle_candidate(primary)
        candidate = stripped or primary
        if candidate and not self._looks_like_address_input(candidate) and " " in candidate:
            compact = self._compact_no_space_candidate(candidate)
            if compact:
                return compact
        return candidate

    def _compact_no_space_candidate(self, text: str) -> str:
        if " " not in text:
            return text
        if self._looks_like_address_input(text):
            return text
        return text.replace(" ", "")

    def _strip_particle_candidate(self, text: str) -> str:
        if not text:
            return text
        for particle in ("으로", "까지"):
            if text.endswith(particle):
                return text[: -len(particle)].rstrip()
        tokens = text.split()
        if len(tokens) >= 2 and self._looks_like_address_phrase(tokens):
            return text
        if text.endswith("로"):
            return text[:-1].rstrip()
        return text

    def _looks_like_address_phrase(self, tokens: list[str]) -> bool:
        if any(char.isdigit() for char in " ".join(tokens)):
            return True
        if len(tokens) < 2:
            return False
        return self._looks_like_address_token(tokens[-1]) and any(
            token.endswith(self._address_tokens) for token in tokens[:-1]
        )

    def _looks_like_address_token(self, token: str) -> bool:
        if any(char.isdigit() for char in token):
            return True
        if token.endswith(self._road_suffixes):
            return True
        return token.endswith(self._address_tokens)

    def _resolve_candidate(self, candidate: str) -> str:
        if not candidate or self.conn is None:
            return ""
        try:
            kakao = self._search_kakao(candidate)
            if kakao:
                if self._looks_like_address_input(candidate) and not self._is_address_like_result(kakao.label):
                    return ""
                return kakao.label
        except Exception:
            pass
        fallback = route_engine.fallback_station_location(self.conn, candidate)
        if fallback:
            return fallback.label
        return ""

    async def _extract_with_gemini(self, text: str) -> str:
        if not text or gemini_client is None:
            return ""
        try:
            logger.info("destination.gemini.request query=%s", text)
            response = await gemini_client.aio.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=text,
                config=gemini_config,
            )
            logger.info("destination.gemini.response query=%s result=%s", text, response.text or "")
        except Exception as exc:
            logger.warning("destination.gemini.error query=%s detail=%s", text, exc)
            return ""
        extracted = (response.text or "").strip()
        if not extracted or extracted.lower() == "none":
            return ""
        normalized = self._normalize_for_search(extracted)
        if not self._is_valid_gemini_candidate(text, normalized):
            return ""
        return normalized

    def _looks_like_address_input(self, text: str) -> bool:
        compact = text.replace(" ", "")
        if any(char.isdigit() for char in compact):
            return True
        tokens = text.split()
        if len(tokens) >= 2 and self._looks_like_address_phrase(tokens):
            return True
        return any(token.endswith(self._road_suffixes) for token in tokens)

    def _is_address_like_result(self, label: str) -> bool:
        compact = label.replace(" ", "")
        if any(char.isdigit() for char in compact):
            return True
        tokens = label.split()
        if len(tokens) >= 2 and self._looks_like_address_phrase(tokens):
            return True
        return any(token.endswith(self._road_suffixes) for token in tokens)

    def _is_valid_gemini_candidate(self, source_text: str, candidate: str) -> bool:
        if not candidate:
            return False
        if len(candidate.replace(" ", "")) < 2:
            return False
        if any(char.isdigit() for char in source_text) and not any(char.isdigit() for char in candidate):
            return False
        return True

    def _search_kakao(self, candidate: str):
        if self._looks_like_address_input(candidate):
            address = self._kakao_address_search(candidate)
            if address:
                return address
            return None
        return route_engine.kakao_keyword_search(candidate)

    def _kakao_address_search(self, query: str):
        api_key = route_engine.load_env().get("KAKAO_REST_API_KEY")
        if not api_key:
            return None
        params = urllib.parse.urlencode({"query": query, "size": 1})
        request = urllib.request.Request(
            f"https://dapi.kakao.com/v2/local/search/address.json?{params}",
            headers={"Authorization": f"KakaoAK {api_key}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        docs = payload.get("documents") or []
        if not docs:
            return None
        first = docs[0]
        label = first.get("address_name") or first.get("road_address", {}).get("address_name") or query
        return route_engine.Location(
            label=label,
            lon=float(first["x"]),
            lat=float(first["y"]),
            source="kakao.address",
        )

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
