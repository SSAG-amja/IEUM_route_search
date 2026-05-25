from __future__ import annotations

import json
import gzip
import math
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
SUBWAY_CATALOG = WORKSPACE_ROOT / "subway_station_catalog" / "web" / "data" / "merged_station_accessibility_catalog.json"
LOCAL_SUBWAY_CATALOG_GZ = ROOT / "data_gz" / "source" / "subway_station_catalog" / "merged_station_accessibility_catalog.json.gz"


INSTRUCTION_TEMPLATES: dict[str, list[str]] = {
    "route_start": [
        "현재 위치에서 안내를 시작합니다.",
        "보도 구간에서는 GPS 위치와 진행 방향을 기준으로 안내합니다.",
        "차량 소리, 보행자 흐름, 공사 구간 등 주변 상황을 함께 확인하세요.",
    ],
    "walk_with_braille": [
        "점자블록을 따라 {distance_m}미터 이동하세요.",
        "이 구간은 경로 주변에 점자블록 데이터가 확인됩니다. 점자블록을 우선 기준으로 이동하세요.",
    ],
    "walk": [
        "보행로를 따라 {distance_m}미터 이동하세요.",
        "이 구간은 경로에 반영된 점자블록 정보가 부족합니다. 보행로 경계와 주변 소리를 확인하며 이동하세요.",
    ],
    "crosswalk": [
        "횡단보도를 {distance_m}미터 건너세요.",
        "음향신호기가 있으면 음향 안내와 보행 신호를 확인한 뒤 건너세요.",
        "음향신호기 정보가 없으면 차량 흐름과 보행 신호를 다시 확인하세요.",
    ],
    "subway_entry": [
        "{station_name}역 접근 구간입니다.",
        "역 내부에서는 GPS가 불안정할 수 있으므로 엘리베이터와 역사 이동동선 안내를 우선합니다.",
    ],
    "subway_ride": [
        "{line_name}을 이용해 {from_station}에서 {to_station}까지 {edge_count}개 구간 이동하세요.",
        "승차 전 승강장과 열차 사이 간격을 확인하세요.",
    ],
    "transfer": [
        "{station_name}역에서 환승합니다.",
        "환승 구간은 역사 내부 이동동선과 엘리베이터 정보를 우선 사용합니다.",
    ],
    "subway_internal": [
        "{station_name}역 내부 이동: {step}",
    ],
    "facility_connector": [
        "엘리베이터 또는 접근성 시설 연결 구간을 {distance_m}미터 이동하세요.",
    ],
    "destination": [
        "목적지 주변에 도착했습니다.",
        "마지막 보행 구간에서는 건물 출입구와 차량 진입로를 확인하세요.",
    ],
    "reroute": [
        "경로에서 벗어났습니다. 현재 위치 기준으로 경로를 다시 계산합니다.",
    ],
    "gps": [
        "현재 위치를 확인했습니다.",
        "진행 방향이 경로와 어긋납니다. 방향을 조정하세요.",
    ],
}


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().replace(" ", "")
    text = re.sub(r"\([^)]*\)", "", text)
    if text.endswith("역"):
        text = text[:-1]
    aliases = {
        "잠실": "잠실",
        "총신대입구": "이수",
        "총신대입구이수": "이수",
    }
    return aliases.get(text, text)


@lru_cache(maxsize=1)
def subway_catalog_by_name() -> dict[str, dict[str, Any]]:
    if SUBWAY_CATALOG.exists():
        payload = json.loads(SUBWAY_CATALOG.read_text(encoding="utf-8"))
    elif LOCAL_SUBWAY_CATALOG_GZ.exists():
        with gzip.open(LOCAL_SUBWAY_CATALOG_GZ, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        return {}
    by_name: dict[str, dict[str, Any]] = {}
    for station in payload.get("stations", []):
        by_name[normalize_name(station.get("station_name"))] = station
    return by_name


def station_name_from_node(node: dict[str, Any] | None, node_id: str | None = None) -> str:
    if node and node.get("station_name"):
        return str(node["station_name"])
    if node_id and node_id.startswith("subway_station:"):
        return node_id.split(":", 2)[-1]
    return ""


def compact_step_text(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"^\d+\)\s*", "", text)


def movement_steps(station_name: str, prefer: str = "elevator", limit: int = 5) -> list[str]:
    station = subway_catalog_by_name().get(normalize_name(station_name))
    if not station:
        return []
    field = "elevator_movements" if prefer == "elevator" else "station_movements"
    records = station.get(field) or []
    if not records and prefer == "elevator":
        records = station.get("station_movements") or []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = str(record.get("mvPathMgNo") or record.get("nextStinCd") or "0")
        grouped[key].append(record)
    if not grouped:
        return []
    selected = sorted(grouped.values(), key=len, reverse=True)[0]
    selected = sorted(selected, key=lambda item: int(item.get("mvTpOrdr") or item.get("exitMvTpOrdr") or 0))
    steps = []
    seen: set[str] = set()
    for item in selected:
        if not item.get("mvContDtl"):
            continue
        text = compact_step_text(item.get("mvContDtl"))
        if text in seen:
            continue
        seen.add(text)
        steps.append(text)
    return steps[:limit]


def instruction(item_type: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"type": item_type, "text": text, **extra}


def group_consecutive(features: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_key: tuple[str, str | None, bool] | None = None
    for feature in features:
        props = feature.get("properties") or {}
        edge_type = str(props.get("edge_type") or "unknown")
        has_braille_context = bool(int(props.get("near_braille_count") or 0) or props.get("has_braille"))
        key = (edge_type, str(props.get("line_code") or "") or None, has_braille_context if edge_type == "walk" else False)
        if current and key != current_key:
            groups.append(current)
            current = []
        current.append(feature)
        current_key = key
    if current:
        groups.append(current)
    return groups


def distance_of(features: list[dict[str, Any]]) -> float:
    return round(sum(float((feature.get("properties") or {}).get("length_m") or 0) for feature in features), 1)


def has_near_braille(features: list[dict[str, Any]]) -> bool:
    return any(int((feature.get("properties") or {}).get("near_braille_count") or 0) > 0 for feature in features)


def haversine_m(left: list[float], right: list[float]) -> float:
    lon1, lat1 = map(math.radians, left)
    lon2, lat2 = map(math.radians, right)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(value))


def bearing_deg(left: list[float], right: list[float]) -> float:
    lon1, lat1 = map(math.radians, left)
    lon2, lat2 = map(math.radians, right)
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def turn_text(previous: float, current: float) -> str:
    diff = (current - previous + 540) % 360 - 180
    if diff <= -135 or diff >= 135:
        return "뒤쪽으로 방향을 크게 전환해"
    if diff <= -45:
        return "좌회전해"
    if diff >= 45:
        return "우회전해"
    return "직진으로"


def feature_coordinates(features: list[dict[str, Any]]) -> list[list[float]]:
    coords: list[list[float]] = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        line = geometry.get("coordinates") or []
        if not line:
            continue
        if coords and line and coords[-1] == line[0]:
            coords.extend(line[1:])
        else:
            coords.extend(line)
    return coords


def walk_segments(features: list[dict[str, Any]], min_segment_m: float = 35, turn_threshold_deg: float = 45) -> list[dict[str, Any]]:
    coords = feature_coordinates(features)
    if len(coords) < 2:
        return [{"direction": "직진으로", "distance_m": distance_of(features)}]

    segments: list[dict[str, Any]] = []
    current_distance = 0.0
    current_bearing: float | None = None
    current_direction = "직진으로"

    for left, right in zip(coords, coords[1:]):
        step_distance = haversine_m(left, right)
        if step_distance <= 0:
            continue
        next_bearing = bearing_deg(left, right)
        if current_bearing is None:
            current_bearing = next_bearing
            current_distance = step_distance
            continue
        diff = abs((next_bearing - current_bearing + 540) % 360 - 180)
        if diff >= turn_threshold_deg and current_distance >= min_segment_m:
            segments.append({"direction": current_direction, "distance_m": round(current_distance, 1)})
            current_direction = turn_text(current_bearing, next_bearing)
            current_distance = step_distance
        else:
            current_distance += step_distance
        current_bearing = next_bearing

    if current_distance:
        segments.append({"direction": current_direction, "distance_m": round(current_distance, 1)})
    return segments or [{"direction": "직진으로", "distance_m": distance_of(features)}]


def append_walk_instructions(instructions: list[dict[str, Any]], group: list[dict[str, Any]]) -> None:
    follows_braille = has_near_braille(group)
    item_type = "walk_with_braille" if follows_braille else "walk"
    base = "점자블록을 따라" if follows_braille else "보행로를 따라"
    suffix = "" if follows_braille else " 이 구간은 경로에 반영된 점자블록 정보가 부족합니다."
    for idx, segment in enumerate(walk_segments(group)):
        direction = segment["direction"]
        distance = segment["distance_m"]
        if idx == 0 and direction == "직진으로":
            text = f"{base} 약 {distance}미터 이동하세요.{suffix}"
        else:
            text = f"{direction} {base} 약 {distance}미터 이동하세요.{suffix}"
        instructions.append(instruction(item_type, text, distance_m=distance, direction=direction))


def generate_instructions(route_geojson: dict[str, Any]) -> list[dict[str, Any]]:
    props = route_geojson.get("properties") or {}
    features = route_geojson.get("features") or []
    instructions: list[dict[str, Any]] = [
        instruction("route_start", "현재 위치에서 안내를 시작합니다."),
        instruction("route_start", "보도 구간에서는 GPS 위치와 진행 방향을 기준으로 안내합니다."),
    ]

    last_subway_line: str | None = None
    for group in group_consecutive(features):
        first_props = group[0].get("properties") or {}
        last_props = group[-1].get("properties") or {}
        edge_type = first_props.get("edge_type")
        dist = distance_of(group)
        if dist < 1 and edge_type not in {"subway_ride"}:
            continue

        if edge_type == "walk":
            append_walk_instructions(instructions, group)
            continue

        if edge_type == "crosswalk":
            has_audible = any((f.get("properties") or {}).get("has_audible_signal") for f in group)
            has_signal = any((f.get("properties") or {}).get("has_ped_signal") for f in group)
            if has_audible:
                text = f"음향신호기가 있는 횡단보도를 약 {dist}미터 건너세요. 음향 안내와 보행 신호를 확인하세요."
            elif has_signal:
                text = f"보행신호가 있는 횡단보도를 약 {dist}미터 건너세요. 음향신호기 정보는 확인되지 않습니다."
            else:
                text = f"횡단보도를 약 {dist}미터 건너세요. 신호와 음향신호기 정보가 부족하므로 차량 흐름을 확인하세요."
            instructions.append(instruction("crosswalk", text, distance_m=dist))
            continue

        if edge_type == "subway_connector":
            from_name = station_name_from_node(first_props.get("route_from_node"), first_props.get("route_from_node_id"))
            to_name = station_name_from_node(last_props.get("route_to_node"), last_props.get("route_to_node_id"))
            station_name = from_name or to_name
            if station_name:
                instructions.append(
                    instruction(
                        "subway_entry",
                        f"{station_name}역 접근 구간입니다. 역 내부에서는 엘리베이터와 역사 이동동선 안내를 우선합니다.",
                        station_name=station_name,
                    )
                )
                for step in movement_steps(station_name, "elevator", limit=4):
                    instructions.append(instruction("subway_internal", f"{station_name}역 내부 이동: {step}", station_name=station_name))
            else:
                instructions.append(instruction("subway_connector", f"지하철역 연결 구간을 약 {dist}미터 이동하세요.", distance_m=dist))
            continue

        if edge_type == "facility_connector":
            instructions.append(
                instruction("facility_connector", f"엘리베이터 또는 접근성 시설 연결 구간을 약 {dist}미터 이동하세요.", distance_m=dist)
            )
            continue

        if edge_type == "subway_ride":
            line_code = str(first_props.get("line_code") or "")
            line_name = str(first_props.get("line_name") or f"{line_code}호선")
            from_name = station_name_from_node(first_props.get("route_from_node"), first_props.get("route_from_node_id"))
            to_name = station_name_from_node(last_props.get("route_to_node"), last_props.get("route_to_node_id"))
            if last_subway_line and line_code != last_subway_line:
                instructions.append(instruction("transfer", f"{from_name}역에서 {last_subway_line}호선에서 {line_name}으로 환승합니다.", station_name=from_name))
                for step in movement_steps(from_name, "station", limit=4):
                    instructions.append(instruction("subway_internal", f"{from_name}역 환승 이동: {step}", station_name=from_name))
            instructions.append(
                instruction(
                    "subway_ride",
                    f"{line_name}을 이용해 {from_name}역에서 {to_name}역까지 {len(group)}개 구간 이동하세요.",
                    line_code=line_code,
                    from_station=from_name,
                    to_station=to_name,
                    segment_count=len(group),
                    distance_m=dist,
                )
            )
            last_subway_line = line_code
            continue

        if edge_type == "crosswalk_connector":
            instructions.append(instruction("move", f"횡단보도 접근 연결부를 약 {dist}미터 이동하세요.", distance_m=dist))
        else:
            instructions.append(instruction("move", f"{edge_type} 구간을 약 {dist}미터 이동하세요.", distance_m=dist))

    end_label = (props.get("end") or {}).get("label") or "목적지"
    instructions.append(instruction("destination", f"{end_label} 주변에 도착했습니다. 안내를 종료합니다."))
    return instructions


def template_payload() -> dict[str, Any]:
    return {
        "generated_for": "IEUM route voice guidance",
        "templates": INSTRUCTION_TEMPLATES,
        "subway_internal_data_source": str(SUBWAY_CATALOG),
    }
