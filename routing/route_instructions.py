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
        "안내를 시작합니다.",
        "주변 상황에 유의하세요.",
    ],
    "walk_with_braille": [
        "점자블록을 따라 {distance_m}미터 이동하세요.",
    ],
    "walk": [
        "보행로를 따라 {distance_m}미터 이동하세요.",
        "이 구간은 점자블록 정보가 부족합니다. 보행로 경계와 주변소리를 확인하며 이동하세요.",
    ],
    "crosswalk": [
        "{distance_m}미터 횡단보도를 건너세요.",
        "횡단보도에 진입했습니다. 음향안내와 보행 신호를 확인하고 건너세요.",
        "차량 소리와 신호를 확인하세요.",
    ],
    "subway_entry": [
        "{station_name}역 입구에 도착했습니다.",
        "역 안에서는 GPS 안내를 사용하지 않습니다. 이동을 마치면 화면을 네 번 터치해주세요.",
    ],
    "subway_ride": [
        "{line_name}을 타고 {from_station}에서 {to_station}까지 총 {edge_count}개 구간 이동하세요.",
        "승차 전 승강장과 열차 사이 간격을 확인하세요.",
    ],
    "transfer": [
        "{station_name}역에서 환승합니다.",
        "환승 구간은 역사 내부 이동동선과 엘리베이터 정보를 우선 사용합니다.",
    ],
    "subway_internal": [
        "{station_name}역 안에서 {step}. 이동을 마치면 화면을 네 번 터치해주세요.",
    ],
    "subway_exit": [
        "{station_name}역 밖으로 나간 후 화면을 네 번 터치해주세요.",
        "확인 후 GPS 안내를 다시 시작합니다.",
    ],
    "station_passage": [
        "{station_name}역 안을 지나 역 밖으로 이동하세요.",
        "엘리베이터 또는 안내 동선을 따라 역 밖으로 이동한 뒤 화면을 네 번 터치해주세요. 확인 후 GPS 안내를 다시 시작합니다.",
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


def naturalize_indoor_step(value: str) -> str:
    text = value.strip()
    floor = re.match(r"^\((B?)(\d+)(?:F)?\)\s*", text)
    if floor:
        level = f"지하 {floor.group(2)}층" if floor.group(1) else f"{floor.group(2)}층"
        text = f"{level} {text[floor.end():]}"
    text = text.replace("엘리베이터 탑승", "엘리베이터에 탑승")
    for ending, replacement in (
        ("탑승", "탑승하세요"),
        ("하차", "내리세요"),
        ("이용", "이용하세요"),
        ("이동", "이동하세요"),
        ("통과", "통과하세요"),
    ):
        if text.endswith(ending):
            return f"{text[:-len(ending)]}{replacement}"
    if text.endswith("승강장"):
        return f"{text}으로 이동하세요"
    return text if text.endswith((".", "요")) else f"{text}로 이동하세요"


def location_tokens(value: str) -> set[str]:
    text = normalize_name(value)
    tokens: set[str] = set()
    for pattern in (
        r"\d+(?:,\d+)?번출구",
        r"\d+번",
        r"[상하내외]선",
        r"[A-Z]계단",
        r"\d+-\d+",
        r"화장실",
        r"개찰구",
        r"대합실",
        r"승강장",
        r"엘리베이터",
        r"EV",
        r"E/V",
        r"발매기",
        r"환승",
        r"갈아타",
    ):
        tokens.update(match.group(0) for match in re.finditer(pattern, text, flags=re.IGNORECASE))
    return tokens


def voice_guidance_devices_for_station(station_name: str) -> list[dict[str, Any]]:
    station = station_catalog(station_name)
    if not station:
        return []
    devices = station.get("voice_guidance_devices") or []
    return [device for device in devices if isinstance(device, dict)]


def related_voice_guidance_devices(
    station_name: str,
    context: str = "",
    line_code: str | None = None,
    limit: int = 3,
    allow_fallback: bool = True,
) -> list[dict[str, Any]]:
    devices = voice_guidance_devices_for_station(station_name)
    if line_code:
        line_filtered = [device for device in devices if str(device.get("line_code") or "") == str(line_code)]
        if line_filtered:
            devices = line_filtered
    if not devices:
        return []

    context_tokens = location_tokens(context)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, device in enumerate(devices):
        location = str(device.get("install_location") or "")
        device_tokens = location_tokens(location)
        score = len(context_tokens & device_tokens) * 10
        normalized_location = normalize_name(location)
        normalized_context = normalize_name(context)
        if normalized_location and normalized_location in normalized_context:
            score += 20
        if "환승" in normalized_context and ("환승" in normalized_location or "갈아타" in normalized_location):
            score += 8
        if "엘리베이터" in normalized_context and ("EV" in device_tokens or "E/V" in device_tokens or "엘리베이터" in device_tokens):
            score += 6
        scored.append((score, -idx, device))
    scored.sort(reverse=True)
    selected = [device for score, _, device in scored if score > 0][:limit]
    if selected:
        return selected
    if not allow_fallback:
        return []

    priority_words = ("출구", "개찰구", "대합실", "엘리베이터", "E/V", "화장실", "승강장")
    fallback = [
        device
        for device in devices
        if any(word in str(device.get("install_location") or "") for word in priority_words)
    ]
    return (fallback or devices)[:limit]


def voice_guidance_text(devices: list[dict[str, Any]]) -> str:
    locations = []
    seen: set[str] = set()
    for device in devices:
        location = str(device.get("install_location") or "").strip()
        if not location or location in seen:
            continue
        seen.add(location)
        locations.append(location)
    if not locations:
        return ""
    if len(locations) == 1:
        return f"음성유도기는 {locations[0]}에 설치되어 있습니다."
    return f"음성유도기는 {', '.join(locations)} 등에 설치되어 있습니다."


def station_catalog(station_name: str) -> dict[str, Any] | None:
    return subway_catalog_by_name().get(normalize_name(station_name))


def station_api_code(station: dict[str, Any] | None, line_code: str | None) -> str | None:
    if not station or not line_code:
        return None
    for line in station.get("lines") or []:
        if str(line.get("line_code") or "") == str(line_code):
            return str(line.get("api_station_code") or line.get("station_code") or "") or None
    return None


def truthy_flag(value: Any) -> bool:
    return str(value or "").strip().upper() in {"Y", "YES", "TRUE", "1", "O", "있음", "유"}


def station_line_records(station: dict[str, Any] | None, field: str, line_code: str | None) -> list[dict[str, Any]]:
    if not station:
        return []
    records = [item for item in station.get(field) or [] if isinstance(item, dict)]
    if not line_code:
        return records
    filtered = [item for item in records if str(item.get("lnCd") or "") == str(line_code)]
    return filtered or records


def platform_gap_description(avg_gap_cm: float) -> str:
    if avg_gap_cm >= 12:
        return "먼 편입니다"
    if avg_gap_cm <= 7:
        return "가까운 편입니다"
    return "보통입니다"


def subway_boarding_safety_text(station_name: str, line_code: str | None) -> str:
    station = station_catalog(station_name)
    if not station:
        return "탑승 전 스크린도어와 열차 간격을 확인하세요."

    screen_records = station_line_records(station, "screen_doors", line_code)
    safety_records = station_line_records(station, "safety_platforms", line_code)
    gap_records = station_line_records(station, "platform_train_distances", line_code)

    gap_values = []
    for item in gap_records:
        try:
            gap_values.append(float(item.get("sfDst")))
        except (TypeError, ValueError):
            continue

    clauses = []
    if gap_values:
        avg_gap_cm = sum(gap_values) / len(gap_values)
        clauses.append(f"열차와 승강장 간격은 {platform_gap_description(avg_gap_cm)}.")

    if screen_records:
        has_screen_door = any(truthy_flag(item.get("scrCharExt")) for item in screen_records)
        clauses.append("스크린도어는 있고" if has_screen_door else "스크린도어 정보는 없고")
    if safety_records:
        has_safety_platform = any(truthy_flag(item.get("sfFotExt")) for item in safety_records)
        clauses.append("안전발판은 있습니다." if has_safety_platform else "안전발판은 없습니다.")

    if not clauses:
        return "탑승 전 스크린도어와 열차 간격을 확인하세요."
    return f"탑승 안전정보입니다. {' '.join(clauses)}"


def movement_group_key(record: dict[str, Any]) -> tuple[str, str]:
    return (
        str(record.get("mvPathMgNo") or record.get("nextStinCd") or "0"),
        str(record.get("imgPath") or ""),
    )


def movement_order(record: dict[str, Any]) -> int:
    value = record.get("mvTpOrdr") or record.get("exitMvTpOrdr") or 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def selected_step_payload(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[movement_group_key(record)].append(record)
    if not grouped:
        return []
    selected = sorted(grouped.values(), key=lambda group: (len(group), -movement_order(group[0])), reverse=True)[0]
    selected = sorted(selected, key=movement_order)
    steps = []
    seen: set[str] = set()
    for item in selected:
        if not item.get("mvContDtl"):
            continue
        text = compact_step_text(item.get("mvContDtl"))
        if text in seen:
            continue
        seen.add(text)
        steps.append(
            {
                "text": text,
                "line_code": item.get("lnCd"),
                "station_code": item.get("stinCd"),
                "next_station_code": item.get("nextStinCd"),
                "movement_path_id": item.get("mvPathMgNo"),
                "movement_path_type": item.get("mvPathDvNm"),
                "start_hint": item.get("stMovePath"),
                "end_hint": item.get("edMovePath"),
                "image_url": item.get("imgPath"),
            }
        )
    return steps[:limit]


def movement_steps_payload(
    station_name: str,
    prefer: str = "elevator",
    limit: int = 5,
    line_code: str | None = None,
    next_station_name: str | None = None,
    transfer_to_line_code: str | None = None,
    transfer_next_station_name: str | None = None,
) -> list[dict[str, Any]]:
    station = subway_catalog_by_name().get(normalize_name(station_name))
    if not station:
        return []
    station_code = station_api_code(station, line_code)
    next_station = station_catalog(next_station_name or "")
    next_station_code = station_api_code(next_station, line_code) if next_station else None

    if prefer == "transfer":
        transfer_records = [
            record
            for record in station.get("elevator_movements") or []
            if record.get("mvPathDvNm") == "환승경로"
            and (not line_code or str(record.get("lnCd") or "") == str(line_code))
            and (not station_code or str(record.get("stinCd") or "") == station_code)
        ]
        if transfer_to_line_code:
            line_text = f"{transfer_to_line_code}호선"
            direction_name = normalize_name(transfer_next_station_name or "")
            direction_hint = direction_name[:2] if len(direction_name) >= 2 else direction_name
            grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for record in transfer_records:
                grouped[movement_group_key(record)].append(record)
            scored_groups = []
            for group in grouped.values():
                text = " ".join(str(record.get("mvContDtl") or "") for record in group)
                normalized = normalize_name(text)
                score = 0
                if line_text in text:
                    score += 4
                if direction_name and direction_name in normalized:
                    score += 3
                elif direction_hint and direction_hint in normalized:
                    score += 2
                scored_groups.append((score, len(group), group))
            scored_groups = [item for item in scored_groups if item[0] > 0]
            if scored_groups:
                scored_groups.sort(key=lambda item: (item[0], item[1]), reverse=True)
                transfer_records = scored_groups[0][2]
        if transfer_records:
            return selected_step_payload(transfer_records, limit)

    fields = ["station_movements", "elevator_movements"] if prefer == "station" else ["elevator_movements", "station_movements"]
    for field in fields:
        records = list(station.get(field) or [])
        if prefer != "transfer":
            normal_records = [record for record in records if record.get("mvPathDvNm") != "환승경로"]
            if normal_records:
                records = normal_records
        if line_code:
            line_filtered = [record for record in records if str(record.get("lnCd") or "") == str(line_code)]
            if line_filtered:
                records = line_filtered
        if station_code:
            station_filtered = [record for record in records if str(record.get("stinCd") or "") == station_code]
            if station_filtered:
                records = station_filtered
        if next_station_code:
            direction_filtered = [record for record in records if str(record.get("nextStinCd") or "") == next_station_code]
            if direction_filtered:
                records = direction_filtered
        if records:
            return selected_step_payload(records, limit)
    return []


def movement_steps(
    station_name: str,
    prefer: str = "elevator",
    limit: int = 5,
    line_code: str | None = None,
    next_station_name: str | None = None,
    transfer_to_line_code: str | None = None,
    transfer_next_station_name: str | None = None,
) -> list[str]:
    return [
        step["text"]
        for step in movement_steps_payload(
            station_name,
            prefer=prefer,
            limit=limit,
            line_code=line_code,
            next_station_name=next_station_name,
            transfer_to_line_code=transfer_to_line_code,
            transfer_next_station_name=transfer_next_station_name,
        )
    ]


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


def spoken_distance_m(value: Any) -> int:
    return max(0, int(round(float(value or 0))))


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
    suffix = "" if follows_braille else " 이 구간은 점자블록 정보가 부족합니다. 보행로 경계와 주변소리를 확인하며 이동하세요."
    for idx, segment in enumerate(walk_segments(group)):
        direction = segment["direction"]
        distance = segment["distance_m"]
        spoken_distance = spoken_distance_m(distance)
        if idx == 0 and direction == "직진으로":
            text = f"{base} {spoken_distance}미터 이동하세요.{suffix}"
        else:
            text = f"{direction} {base} {spoken_distance}미터 이동하세요.{suffix}"
        instructions.append(instruction(item_type, text, distance_m=distance, direction=direction))


def has_subway_ride_before_outdoor(groups: list[list[dict[str, Any]]], start_index: int) -> bool:
    for next_group in groups[start_index + 1 :]:
        edge_type = str((next_group[0].get("properties") or {}).get("edge_type") or "unknown")
        if edge_type == "subway_ride":
            return True
        if edge_type in {"walk", "crosswalk"}:
            return False
    return False


def subway_segment(group: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not group:
        return None
    first_props = group[0].get("properties") or {}
    if first_props.get("edge_type") != "subway_ride":
        return None
    last_props = group[-1].get("properties") or {}
    line_code = str(first_props.get("line_code") or "")
    line_name = str(first_props.get("line_name") or f"{line_code}호선")
    return {
        "line_code": line_code,
        "line_name": line_name,
        "from_station": station_name_from_node(first_props.get("route_from_node"), first_props.get("route_from_node_id")),
        "to_station": station_name_from_node(last_props.get("route_to_node"), last_props.get("route_to_node_id")),
        "next_station": station_name_from_node(first_props.get("route_to_node"), first_props.get("route_to_node_id")),
    }


def nearest_subway_segment(groups: list[list[dict[str, Any]]], start_index: int, step: int) -> dict[str, Any] | None:
    index = start_index
    while 0 <= index < len(groups):
        segment = subway_segment(groups[index])
        if segment:
            return segment
        index += step
    return None


def generate_instructions(route_geojson: dict[str, Any]) -> list[dict[str, Any]]:
    props = route_geojson.get("properties") or {}
    features = route_geojson.get("features") or []
    instructions: list[dict[str, Any]] = [
        instruction("route_start", "안내를 시작합니다."),
        instruction("route_start", "주변 상황에 유의하세요."),
    ]

    last_subway_line: str | None = None
    inside_station = False
    groups = group_consecutive(features)
    for group_index, group in enumerate(groups):
        first_props = group[0].get("properties") or {}
        last_props = group[-1].get("properties") or {}
        edge_type = first_props.get("edge_type")
        dist = distance_of(group)
        spoken_dist = spoken_distance_m(dist)
        if dist < 1 and edge_type not in {"subway_ride"}:
            continue

        if edge_type in {"walk", "route_start_connector", "route_end_connector"}:
            append_walk_instructions(instructions, group)
            continue

        if edge_type == "crosswalk":
            has_audible = any((f.get("properties") or {}).get("has_audible_signal") for f in group)
            has_signal = any((f.get("properties") or {}).get("has_ped_signal") for f in group)
            if has_audible:
                text = f"횡단보도에 진입했습니다. 음향안내와 보행 신호를 확인하고 {spoken_dist}미터 건너세요."
            elif has_signal:
                text = f"{spoken_dist}미터 횡단보도를 건너세요. 음향신호기 정보가 확인되지 않습니다."
            else:
                text = f"{spoken_dist}미터 횡단보도를 건너세요. 차량 소리를 확인하세요."
            instructions.append(instruction("crosswalk", text, distance_m=dist))
            continue

        if edge_type == "subway_connector":
            from_name = station_name_from_node(first_props.get("route_from_node"), first_props.get("route_from_node_id"))
            to_name = station_name_from_node(last_props.get("route_to_node"), last_props.get("route_to_node_id"))
            station_name = from_name or to_name
            if station_name:
                if inside_station:
                    exit_devices = related_voice_guidance_devices(
                        station_name,
                        context="출구 엘리베이터 개찰구 대합실",
                        limit=3,
                    )
                    exit_voice_text = voice_guidance_text(exit_devices)
                    voice_suffix = f" {exit_voice_text}" if exit_voice_text else ""
                    instructions.append(
                        instruction(
                            "subway_exit",
                            f"{station_name}역 밖으로 나간 후 화면을 네 번 터치해주세요. "
                            f"{voice_suffix.strip()} "
                            "확인 후 GPS 안내를 다시 시작합니다.",
                            station_name=station_name,
                            voice_guidance_devices=exit_devices,
                            indoor_data_confidence="catalog_text",
                        )
                    )
                    inside_station = False
                elif has_subway_ride_before_outdoor(groups, group_index):
                    next_segment = nearest_subway_segment(groups, group_index + 1, 1)
                    line_code = next_segment["line_code"] if next_segment else None
                    next_station_name = next_segment["next_station"] if next_segment else None
                    direction_text = (
                        f" {next_segment['line_name']} {next_station_name} 방면 승강장으로 이동합니다."
                        if next_segment
                        else ""
                    )
                    instructions.append(
                        instruction(
                            "subway_entry",
                            f"{station_name}역 입구에 도착했습니다. 역 안에서는 GPS 안내를 사용하지 않습니다. "
                            f"엘리베이터와 역사 이동 안내에 따라 이동합니다.{direction_text} "
                            "역 안으로 들어가면 화면을 네 번 터치해주세요.",
                            station_name=station_name,
                            line_code=line_code,
                        )
                    )
                    entry_devices = related_voice_guidance_devices(
                        station_name,
                        context=f"출구 개찰구 대합실 {next_station_name or ''}",
                        line_code=line_code,
                        limit=3,
                    )
                    entry_voice_text = voice_guidance_text(entry_devices)
                    if entry_voice_text:
                        instructions.append(
                            instruction(
                                "subway_internal",
                                f"{entry_voice_text} 음성유도기를 확인하며 이동하세요.",
                                station_name=station_name,
                                line_code=line_code,
                                voice_guidance_devices=entry_devices,
                                indoor_data_confidence="catalog_text",
                            )
                        )
                    for step in movement_steps(
                        station_name,
                        "station",
                        limit=5,
                        line_code=line_code,
                        next_station_name=next_station_name,
                    ):
                        step_devices = related_voice_guidance_devices(
                            station_name,
                            context=step,
                            line_code=line_code,
                            limit=2,
                            allow_fallback=False,
                        )
                        step_voice_text = voice_guidance_text(step_devices)
                        voice_suffix = f" {step_voice_text}" if step_voice_text else ""
                        instructions.append(
                            instruction(
                                "subway_internal",
                                f"{station_name}역 안에서 {naturalize_indoor_step(step)}.{voice_suffix} "
                                "이동을 마치면 화면을 네 번 터치해주세요.",
                                station_name=station_name,
                                line_code=line_code,
                                voice_guidance_devices=step_devices,
                                indoor_data_confidence="catalog_text",
                            )
                        )
                    inside_station = True
                else:
                    instructions.append(
                        instruction(
                            "station_passage",
                            f"{station_name}역 안을 지나 역 밖으로 이동하세요. "
                            "엘리베이터 또는 안내 동선을 따라 역 밖으로 이동한 뒤 화면을 네 번 터치해주세요. "
                            "확인 후 GPS 안내를 다시 시작합니다.",
                            station_name=station_name,
                        )
                    )
            else:
                instructions.append(instruction("subway_connector", f"지하철역 연결 구간을 {spoken_dist}미터 이동하세요.", distance_m=dist))
            continue

        if edge_type == "facility_connector":
            instructions.append(
                instruction("facility_connector", f"엘리베이터 또는 접근성 시설 연결 구간을 {spoken_dist}미터 이동하세요.", distance_m=dist)
            )
            continue

        if edge_type == "subway_ride":
            line_code = str(first_props.get("line_code") or "")
            line_name = str(first_props.get("line_name") or f"{line_code}호선")
            from_name = station_name_from_node(first_props.get("route_from_node"), first_props.get("route_from_node_id"))
            to_name = station_name_from_node(last_props.get("route_to_node"), last_props.get("route_to_node_id"))
            safety_text = subway_boarding_safety_text(from_name, line_code)
            if last_subway_line and line_code != last_subway_line:
                transfer_next_station_name = station_name_from_node(first_props.get("route_to_node"), first_props.get("route_to_node_id"))
                instructions.append(
                    instruction(
                        "transfer",
                        f"{from_name}역 {last_subway_line}호선에서 {line_name}으로 환승하세요. "
                        "환승 이동 안내를 시작하려면 화면을 네 번 터치해주세요.",
                        station_name=from_name,
                    )
                )
                for step in movement_steps(
                    from_name,
                    "transfer",
                    limit=6,
                    line_code=last_subway_line,
                    transfer_to_line_code=line_code,
                    transfer_next_station_name=transfer_next_station_name,
                ):
                    step_devices = related_voice_guidance_devices(
                        from_name,
                        context=f"환승 {step} {transfer_next_station_name}",
                        line_code=last_subway_line,
                        limit=2,
                        allow_fallback=False,
                    )
                    step_voice_text = voice_guidance_text(step_devices)
                    voice_suffix = f" {step_voice_text}" if step_voice_text else ""
                    instructions.append(
                        instruction(
                            "subway_internal",
                            f"{from_name}역 안에서 {naturalize_indoor_step(step)}.{voice_suffix} "
                            "이동을 마치면 화면을 네 번 터치해주세요.",
                            station_name=from_name,
                            line_code=last_subway_line,
                            transfer_to_line_code=line_code,
                            voice_guidance_devices=step_devices,
                            indoor_data_confidence="catalog_text",
                        )
                    )
            instructions.append(
                instruction(
                    "subway_ride",
                    f"{line_name}을 타고 {from_name}역에서 {to_name}역까지 총 {len(group)}개 구간 이동하세요. "
                    f"{safety_text} "
                    f"{to_name}역에 도착하면 화면을 네 번 터치해주세요.",
                    line_code=line_code,
                    from_station=from_name,
                    to_station=to_name,
                    segment_count=len(group),
                    distance_m=dist,
                )
            )
            last_subway_line = line_code
            inside_station = True
            continue

        if edge_type == "crosswalk_connector":
            instructions.append(instruction("move", f"횡단보도 접근 연결부를 {spoken_dist}미터 이동하세요.", distance_m=dist))
        else:
            instructions.append(instruction("move", f"{edge_type} 구간을 {spoken_dist}미터 이동하세요.", distance_m=dist))

    end_label = (props.get("end") or {}).get("label") or "목적지"
    instructions.append(instruction("destination", f"{end_label} 주변에 도착했습니다. 안내를 종료합니다."))
    return instructions


def template_payload() -> dict[str, Any]:
    return {
        "generated_for": "IEUM route voice guidance",
        "templates": INSTRUCTION_TEMPLATES,
        "subway_internal_data_source": str(SUBWAY_CATALOG),
    }
