# IEUM 경로 산출 방식

작성일: 2026-05-25

## 0. 한눈에 보는 전체 경로 산출 로직

IEUM의 경로 산출은 단순히 지도 API에서 경로를 받아오는 방식이 아니다. 우리가 직접 구축한 서울시 접근성 graph를 기반으로, 각 이동 구간의 접근성 정보를 비용으로 환산한 뒤 가장 비용이 낮은 경로를 찾는 방식이다.

전체 흐름은 다음과 같다.

```text
1. 원천 데이터 수집
   - 보행로, 점자블록, 횡단보도, 음향신호기
   - 지하철역, 노선, 엘리베이터, 역사 내부 이동동선

2. 통합 graph 구축
   - 이동 지점을 node로 변환
   - 이동 가능한 구간을 edge로 변환
   - 보행, 횡단보도, 지하철, 엘리베이터 연결을 하나의 graph에 통합

3. SQLite 탐색 DB 생성
   - nodes 테이블: 이동 지점 저장
   - edges 테이블: 이동 구간, 거리, geometry, 접근성 속성 저장
   - raw_properties에 원본 feature 속성 저장

4. 접근성 정보 보강
   - 일반 보행 edge 주변의 점자블록, 횡단보도, 음향신호기 근접 여부 계산
   - near_braille_count, near_crosswalk_count, near_audible_signal_count 저장

5. 시각장애인용 edge 비용 계산
   - 안전하고 안내 가능한 구간은 비용 감소
   - 위험하거나 정보가 부족한 구간은 비용 증가
   - 최종 비용은 visual_impairment_weight로 저장
   - 엘리베이터 연결, 지하철 내부 이동동선 보유 여부, 환승 부담을 함께 반영

6. 사용자 입력 처리
   - 주소/장소명은 Kakao REST API로 좌표 변환
   - 좌표 입력은 그대로 사용
   - 지하철역명은 DB fallback 검색

7. graph node snap
   - 출발지 좌표에서 가장 가까운 graph node 선택
   - 도착지 좌표에서 가장 가까운 graph node 선택

8. Dijkstra 경로 탐색
   - 거리 length_m가 아니라 visual_impairment_weight 합이 가장 낮은 경로 탐색
   - 보행, 횡단보도, 지하철, 환승, 엘리베이터 연결이 같은 graph 안에서 함께 계산됨
   - 지하철 노선 변경 시 환승 penalty를 추가해 불필요한 환승을 억제

9. 결과 복원
   - 선택된 edge 목록을 순서대로 복원
   - edge geometry를 이어 route GeoJSON 생성
   - 경로 요약과 접근성 포함량 계산

10. 안내 생성 및 시각화
    - 점자블록 반영 구간은 “점자블록을 따라 이동”
    - 점자블록 정보 부족 구간은 “보행로를 따라 이동”
    - geometry 방향 변화로 좌회전/우회전 안내 생성
    - 횡단보도는 음향신호/보행신호 여부 반영
    - 지하철은 노선, 환승, 역 내부 이동동선 안내 생성
    - 지도에는 반투명 경로 띠와 edge type별 선을 함께 표시
```

이를 의사코드로 표현하면 다음과 같다.

```text
build_graph():
    nav_data = load_walk_braille_crosswalk_audible_data()
    subway_data = load_station_line_elevator_movement_data()

    nodes = create_walk_nodes(nav_data)
    nodes += create_subway_station_nodes(subway_data)
    nodes += create_accessibility_facility_nodes(nav_data, subway_data)

    edges = create_walk_edges(nav_data)
    edges += create_crosswalk_edges(nav_data)
    edges += create_subway_ride_edges(subway_data)
    edges += create_connector_edges(nodes)

    save_nodes_edges_to_geojson(nodes, edges)
    save_nodes_edges_to_sqlite(nodes, edges)


enrich_accessibility():
    for edge in sqlite.edges:
        if edge is walk-like:
            edge.near_braille_count = count_nearby_braille(edge.geometry)
            edge.near_crosswalk_count = count_nearby_crosswalk(edge.geometry)
            edge.near_audible_signal_count = count_nearby_audible_signal(edge.geometry)

        edge.visual_impairment_weight = calculate_accessibility_cost(edge)
        update_sqlite(edge)


route(start_input, end_input):
    start_coord = resolve_location(start_input)
    end_coord = resolve_location(end_input)

    start_node = nearest_graph_node(start_coord)
    end_node = nearest_graph_node(end_coord)

    graph = load_edges_as_adjacency_list(sqlite.edges)
    selected_edges = dijkstra(
        graph,
        start_node,
        end_node,
        cost = visual_impairment_weight
    )

    route_geojson = restore_edge_geometries(selected_edges)
    summary = summarize_accessibility_coverage(selected_edges)
    instructions = generate_route_instructions(route_geojson)

    return route_geojson, summary, instructions
```

핵심은 다음 한 문장으로 정리할 수 있다.

> IEUM은 서울시 보행 및 지하철 접근성 데이터를 node-edge graph로 통합하고, 각 edge에 시각장애인 이동 적합도 기반 비용을 부여한 뒤, Dijkstra 알고리즘으로 총 접근성 비용이 가장 낮은 경로를 산출한다.

최종 경로 선택 원칙은 다음과 같다.

- 점자블록이 있거나 점자블록과 가까운 보행 구간 우선
- 음향신호기가 있는 횡단보도 우선
- 보행신호 또는 음향신호 정보가 부족한 횡단보도 회피
- 엘리베이터 연결 우선
- 지하철 내부 이동동선 정보가 있는 역 우선
- 장거리 이동에서는 지하철 이용 허용
- 노선 변경 시 환승 penalty를 부여해 불필요한 환승 억제
- 데이터 신뢰도가 낮은 구간 회피
- 최종적으로 전체 `visual_impairment_weight`가 가장 낮은 경로 선택

## 1. 현재 구현 범위

현재 IEUM은 서울시 보행 접근성 데이터와 지하철 접근성 데이터를 통합한 graph 기반 경로 탐색 MVP를 구축한 상태다.

구현된 범위는 다음과 같다.

- 출발지/도착지 입력
- Kakao REST API 기반 주소/장소명 좌표 변환
- 서울시 접근성 graph node로 위치 보정
- 보행로, 점자블록, 횡단보도, 음향신호기, 지하철역, 지하철 노선, 엘리베이터 정보 통합
- 시각장애인 이동에 적합한 가중치 기반 경로 탐색
- 지하철 이용 여부를 포함한 통합 경로 산출
- 선택된 경로의 접근성 데이터 포함량 요약
- 경로 안내 멘트 생성
- 지도 기반 경로 및 접근성 layer 시각화

아직 구현 전인 범위는 실시간 GPS 기반 내비게이션 기능이다.

- 실시간 현재 위치 추적
- 다음 안내 지점까지 남은 거리 계산
- 경로 이탈 감지
- 자동 재탐색
- 보행 중 방향 보정

따라서 현재 단계는 `정적 경로 산출 + 지도 시각화 + 안내 생성`까지 완료된 상태로 볼 수 있다.

## 2. 사용 데이터

경로 산출은 `nav_map`, `subway_station_catalog`, `ieum_seoul_accessibility_dataset`의 데이터를 기반으로 한다.

### 2.1 보행 및 접근성 데이터

출처: `nav_map`

주요 데이터:

- 보행 node
- 보행 edge
- 점자블록 node/edge
- 횡단보도 edge
- 음향신호기 point
- 지하철 엘리베이터 point

활용 방식:

- 보행 가능한 경로망 구성
- 점자블록 존재 여부 및 근접 여부 판단
- 횡단보도 통과 구간 판단
- 음향신호기 존재 여부 판단
- 보행 경로의 시각장애인 적합도 계산

### 2.2 지하철 접근성 데이터

출처: `subway_station_catalog`

주요 데이터:

- 서울시 지하철역 위치
- 지하철 노선 연결 정보
- 역별 엘리베이터 정보
- 역사 내부 이동동선
- 출구 정보
- 장애인 화장실 등 접근성 시설 정보

활용 방식:

- 지하철역 node 생성
- 노선별 역 간 이동 edge 생성
- 출발지/도착지와 지하철역 연결
- 지하철 이용 경로 산출
- 역 내부 이동 안내 멘트 생성
- 승차역은 노선과 다음 역 방향(`lnCd`, `stinCd`, `nextStinCd`)을 기준으로 내부 이동동선을 선택
- 환승역은 `환승경로` 이동동선을 우선 사용하고, 환승 후 노선/방면 문구를 반영

### 2.3 최종 통합 graph

출처: `ieum_seoul_accessibility_dataset`

주요 산출물:

- `data/ieum_route_graph_nodes.geojson`
- `data/ieum_route_graph_edges.geojson`
- `data_gz/`
- `routing/ieum_graph.sqlite`

경로 탐색에는 최종적으로 `routing/ieum_graph.sqlite`를 사용한다.

SQLite 주요 테이블:

- `nodes`: 보행 node, 지하철역 node 등
- `edges`: 보행 edge, 횡단보도 edge, 지하철 이동 edge, 연결 edge 등
- `metadata`: graph 생성 및 보강 상태 기록

### 2.4 gz 데이터셋 구성과 사용 관계

`ieum_seoul_accessibility_dataset/data_gz`는 세 종류로 나뉜다.

```text
data_gz/source/
data_gz/layers/
data_gz/ieum_route_graph_*.gz
```

#### 2.4.1 source: 원천 gz 전체 보관

`source` 폴더는 원천 프로젝트의 gz 전체 사본이다.

```text
data_gz/source/nav_map/
data_gz/source/subway_station_catalog/
```

복사 기준:

```text
nav_map/data_gz/*.gz
-> data_gz/source/nav_map/*.gz

subway_station_catalog/data_gz/*.gz
-> data_gz/source/subway_station_catalog/*.gz
```

현재 보관 개수:

```text
nav_map source gz: 11 files
subway_station_catalog source gz: 10 files
```

`source`의 역할:

- 원천 데이터 추적
- 제출 자료 보존
- 데이터 검증 및 재처리 근거
- 최종 graph가 어떤 원천에서 만들어졌는지 설명하기 위한 archive

서버가 경로를 찾을 때 `source`의 모든 gz를 매번 직접 읽지는 않는다. 경로 탐색은 아래 최종 graph gz를 사용한다.

#### 2.4.2 최종 경로 탐색용 graph gz

실제 경로 탐색의 핵심 입력은 다음 두 파일이다.

```text
data_gz/ieum_route_graph_nodes.geojson.gz
data_gz/ieum_route_graph_edges.geojson.gz
```

이 두 파일은 `nav_map`과 `subway_station_catalog`의 원천 데이터를 통합, 정규화해 만든 최종 node-edge graph다.

주요 반영 원천:

```text
nav_map/walk_nodes.geojson.gz
nav_map/walk_network.geojson.gz
nav_map/braille_network_nodes.geojson.gz
nav_map/braille_network_links.geojson.gz
nav_map/crosswalk_links_enriched.geojson.gz
nav_map/audible_signal_points.geojson.gz
nav_map/subway_elevators.geojson.gz
subway_station_catalog/merged_station_points.geojson.gz
subway_station_catalog/line_segments_display.geojson.gz
```

최종 graph에 포함되는 정보:

- 보행 node
- 보행 edge
- 점자블록 node/edge
- 횡단보도 endpoint/edge
- 음향신호기 위치
- 지하철 엘리베이터 위치
- 지하철역 node
- 지하철역 간 이동 edge
- 지하철역-보행망 connector
- 지하철역-엘리베이터 connector
- 접근성 시설 connector
- edge별 거리, geometry, 접근성 flag, 신뢰도, 비용 계산용 속성

실행 시 흐름:

```text
ieum_route_graph_nodes.geojson.gz
ieum_route_graph_edges.geojson.gz
-> build_sqlite_graph.py
-> routing/ieum_graph.sqlite
-> enrich_sqlite_accessibility.py
-> Dijkstra 경로 탐색
```

즉, 원천 gz를 그대로 매번 탐색하는 것이 아니라, 원천 gz들을 종합해 만든 최종 graph gz를 SQLite로 변환해 탐색한다.

#### 2.4.3 layers: 런타임 지도 표시 및 context 계산용 gz

`layers` 폴더는 웹 지도 표시와 경로 주변 접근성 context 계산을 위해 직접 읽는 gz다.

```text
data_gz/layers/braille_network_links.geojson.gz
data_gz/layers/crosswalk_links_enriched.geojson.gz
data_gz/layers/audible_signal_points.geojson.gz
data_gz/layers/subway_elevators.geojson.gz
data_gz/layers/merged_station_points.geojson.gz
data_gz/layers/line_segments_display.geojson.gz
```

사용 목적:

- 점자블록 layer 표시
- 횡단보도 layer 표시
- 음향신호기 layer 표시
- 지하철 엘리베이터 marker 표시
- 지하철역 marker 표시
- 지하철 노선 layer 표시
- 경로 주변 점자블록/횡단보도/음향신호기 개수 계산

#### 2.4.4 런타임에 직접 읽는 source gz

다음 파일은 `source`에 있지만 런타임 안내 생성에도 직접 사용한다.

```text
data_gz/source/subway_station_catalog/merged_station_accessibility_catalog.json.gz
```

사용 목적:

- 역별 엘리베이터 이동동선 안내
- 역사 내부 이동동선 안내
- 출구/접근성 정보 안내
- 시각장애인 음성유도기 설치 위치 안내 보강
- 지하철 내부 안내 멘트 생성
- `mvPathMgNo`, `mvTpOrdr`, `exitMvTpOrdr` 기준으로 같은 역내 이동경로를 묶고 순서대로 안내
- 이동경로 이미지(`imgPath`)가 있는 경우 instruction payload에 함께 포함

추가 보관 원천:

```text
data_gz/source/subway_station_catalog/seoul_metro_voice_guidance_devices_20250812.csv.gz
```

이 파일은 서울교통공사 지하철 시각장애인 음성유도기 설치 위치 정보 CSV를 gzip으로 보관한 것이다. 역명, 호선, 외부역번호, 설치위치 텍스트를 `merged_station_accessibility_catalog.json.gz`의 `voice_guidance_devices` 필드로 병합해 사용한다.

역내 안내 생성 시에는 현재 이동 단계 문구와 `voice_guidance_devices.install_location`의 출구, 개찰구, 대합실, 승강장, 엘리베이터, 환승 관련 키워드를 비교해 관련 설치 위치만 짧게 안내한다. 정확한 실내 좌표/거리가 없는 데이터이므로 음성유도기 위치는 경로 자체가 아니라 보조 확인 지점으로 안내한다.

#### 2.4.5 현재 경로 탐색에 직접 쓰지 않는 gz

다음 파일은 보관/검증/추후 확장용이며, 현재 Dijkstra 경로 탐색에는 직접 입력되지 않는다.

```text
data_gz/source/nav_map/roads.json.gz
data_gz/source/nav_map/seoul_boundary.geojson.gz
data_gz/source/subway_station_catalog/ACCESSIBILITY_COVERAGE.json.gz
data_gz/source/subway_station_catalog/NETWORK_VALIDATION.json.gz
data_gz/source/subway_station_catalog/NETWORK_VERIFICATION.json.gz
data_gz/source/subway_station_catalog/NAV_MAP_CROSS_VALIDATION.json.gz
```

현재 미사용 이유:

- `roads.json.gz`: 큰길 우선, 도로명 기반 안내 등 추후 보강용
- `seoul_boundary.geojson.gz`: 서울시 서비스 영역 필터링용
- validation 계열: 데이터 품질 검증 결과이며 경로 계산 입력이 아님

기획서 표현:

> IEUM은 원천 gz 전체를 `source`에 보존하되, 경로 탐색에는 원천 데이터를 통합 및 정규화한 최종 node-edge graph gz를 사용한다. 지도 표시와 경로 주변 접근성 context 계산에는 별도 `layers` gz를 사용하며, 지하철 내부 안내에는 역별 접근성 catalog gz를 직접 참조한다.

## 3. 경로 graph 구조

IEUM 경로 탐색은 모든 이동 가능 구간을 node와 edge로 표현한다.

### 3.1 node

node는 경로의 연결 지점이다.

예시:

- 보행로 끝점
- 횡단보도 연결점
- 점자블록 연결점
- 지하철역
- 엘리베이터 접근점

### 3.2 edge

edge는 node와 node 사이의 이동 가능한 구간이다.

주요 edge type:

- `walk`: 일반 보행 구간
- `braille_walk`: 점자블록 기반 보행 구간
- `crosswalk`: 횡단보도 구간
- `facility_connector`: 엘리베이터 등 접근성 시설 연결 구간
- `subway_connector`: 보행망과 지하철역 연결 구간
- `subway_ride`: 지하철 역 간 이동 구간

각 edge는 다음 정보를 가진다.

- 시작 node
- 종료 node
- 실제 거리 `length_m`
- 경로 geometry
- edge type
- 지하철 노선 코드
- 점자블록 여부
- 보행신호 여부
- 음향신호 여부
- 접근성 데이터 근접 count
- 시각장애인용 경로 비용 `visual_impairment_weight`

## 4. 출발지/도착지 처리

사용자는 주소, 장소명, 지하철역명 또는 좌표를 입력할 수 있다.

처리 순서:

1. 입력값이 좌표인지 확인
2. 좌표가 아니면 Kakao REST API로 장소/주소 검색
3. Kakao 검색이 실패하면 지하철역 DB fallback 검색
4. 확보한 위경도 좌표를 graph의 가장 가까운 node에 연결

이 과정을 통해 사용자의 현실 좌표를 IEUM graph 위의 경로 탐색 시작점과 도착점으로 변환한다.

## 5. 시각장애인용 경로 비용

IEUM은 단순 최단거리 경로를 선택하지 않는다.

각 edge에 `visual_impairment_weight`를 부여하고, 이 비용이 가장 낮은 경로를 선택한다.

기본 개념:

- 안전하거나 안내 가능한 구간은 비용 감소
- 위험하거나 정보가 부족한 구간은 비용 증가
- 장거리 이동에서 지하철 이용이 적합하면 지하철 edge 비용을 낮게 반영

### 5.1 비용 감소 요소

다음 정보가 있는 edge는 경로 선택에서 더 선호된다.

- 점자블록이 있는 보행 구간
- 점자블록과 가까운 보행 구간
- 음향신호기가 있는 횡단보도
- 보행신호가 있는 횡단보도
- 지하철역 접근 연결 구간
- 지하철 탑승 구간
- 엘리베이터 및 접근성 시설 연결 구간
- 지하철 내부 이동동선 정보가 있는 역의 연결 구간

### 5.2 비용 증가 요소

다음 조건은 경로 선택에서 불리하게 반영된다.

- 보행신호 정보가 없는 횡단보도
- 음향신호기 정보가 없는 횡단보도
- 접근성 정보가 부족한 보행 구간
- 데이터 신뢰도가 낮은 구간
- 엘리베이터가 있는 역에서 엘리베이터를 거치지 않는 일반 지하철 연결 구간
- 지하철 노선 변경으로 발생하는 환승

### 5.3 접근성 근접 정보 반영

초기에는 점자블록, 횡단보도, 음향신호기 데이터가 별도 layer로 존재해도 선택된 `walk` edge에는 직접 반영되지 않는 문제가 있었다.

이를 보완하기 위해 `enrich_sqlite_accessibility.py`를 통해 SQLite edge에 다음 값을 추가했다.

- `near_braille_count`
- `near_crosswalk_count`
- `near_audible_signal_count`
- `accessibility_enriched`

보강 결과:

- 전체 edge: 380,345개
- 점자블록 근접 edge: 90,194개
- 음향신호기 근접 edge: 62,552개
- 횡단보도 근접 edge: 81,666개

이제 일반 보행 edge라도 주변에 점자블록이나 음향신호기 데이터가 있으면 경로 비용에 반영된다.

### 5.4 엘리베이터 및 역사 내부 이동동선 가중치

모든 지하철역에 대해 엘리베이터 연결을 전역 규칙으로 우선 반영한다. 특정 역 하나에만 적용하는 방식이 아니라, SQLite의 모든 `subway_connector`, `facility_connector` edge를 대상으로 재계산한다.

적용 원칙:

- `has_elevator=True`인 `subway_connector`는 비용을 크게 감소시킨다.
- `has_elevator=True`인 `facility_connector`도 비용을 크게 감소시킨다.
- 엘리베이터가 있는 역에서 엘리베이터를 거치지 않는 일반 `subway_connector`는 비용을 증가시킨다.
- `has_indoor_route=True`인 역의 지하철 연결 edge는 비용을 추가로 감소시킨다.

최종 적용에서는 엘리베이터가 있는 역의 일반 지하철 연결 edge에 고정 penalty를 추가한다.

```text
NON_ELEVATOR_STATION_CONNECTOR_PENALTY = 650.0
```

따라서 엘리베이터가 있는 역에서는 단순히 일반 출입구보다 엘리베이터를 조금 선호하는 수준이 아니라, 실제 경로가 엘리베이터 connector를 우선 통과하도록 강하게 유도한다.

이 원칙을 통해 지하철역 진입/하차 시 일반 출입구보다 엘리베이터 연결이 실제 경로 탐색에서 우선 선택되도록 한다.

DB 보강 상태:

- `subway_elevator_global_priority=true`
- `subway_indoor_route_weighting=true`

검증 예시:

```text
고덕로 210 -> 청파로 83길
```

서울역 하차 구간에서 다음 edge가 선택되는 것을 확인했다.

```text
서울역 subway_station
-> 서울역 subway_elevator
-> 보행망
```

즉, 안내 문장만 엘리베이터를 말하는 것이 아니라 실제 선택된 graph edge도 엘리베이터 connector를 통과한다.

추가 검증 예시:

```text
고덕로 210 -> 풍성로 22길
```

천호역 하차 구간에서 다음 edge가 선택되는 것을 확인했다.

```text
천호역 subway_station
-> 천호역 subway_elevator
-> 보행망
```

## 6. 경로 탐색 알고리즘

현재 경로 탐색은 Dijkstra 알고리즘을 사용한다.

탐색 기준:

- 단순 거리 `length_m`가 아니라 `visual_impairment_weight` 기준

탐색 흐름:

1. 출발지 좌표를 graph node에 snap
2. 도착지 좌표를 graph node에 snap
3. SQLite `edges`를 adjacency list로 변환
4. `visual_impairment_weight` 기준으로 Dijkstra 실행
5. 선택된 edge 목록을 순서대로 복원
6. edge geometry를 합쳐 route GeoJSON 생성
7. route summary와 안내 멘트 생성

즉, IEUM 경로는 가장 짧은 길이 아니라 시각장애인에게 더 안전하고 안내 가능한 비용이 낮은 길을 선택한다.

### 6.1 환승 penalty가 포함된 Dijkstra

최종 경로 탐색에서는 단순 node만 상태로 보지 않고, 지하철 이용 중인 경우 현재 노선 정보도 함께 본다.

기본 Dijkstra 상태:

```text
(현재 node, 현재 지하철 노선)
```

탐색 중 `subway_ride` edge의 `line_code`가 이전 노선과 다르면 환승으로 판단하고 추가 비용을 부여한다.

```text
이전 노선 == 현재 노선
-> 추가 비용 없음

이전 노선 != 현재 노선
-> 환승 penalty 추가
```

현재 환승 penalty:

```text
TRANSFER_PENALTY = 700.0
```

이 값은 환승을 절대 금지하지는 않는다. 다만 같은 접근성 조건이라면 환승이 적은 경로가 더 낮은 비용을 갖도록 만들어, 불필요한 환승을 억제한다.

## 7. 지하철 경로 포함 방식

지하철도 별도 시스템으로 분리하지 않고 동일 graph 안에 포함한다.

구성:

- 지하철역은 node
- 역 간 노선 이동은 `subway_ride` edge
- 보행망과 역은 `subway_connector` edge
- 엘리베이터 등 접근성 시설은 `facility_connector` edge

따라서 경로는 다음과 같은 형태로 자연스럽게 산출된다.

```text
출발지
-> 보행 구간
-> 지하철역 접근
-> 지하철 탑승
-> 환승 또는 하차
-> 도착지 주변 보행
-> 목적지
```

단거리 이동에서는 지하철 이용이 선택되지 않을 수 있고, 장거리 이동에서는 지하철 edge의 비용이 낮아 경로에 포함될 수 있다.

### 7.1 환승 처리 방식

현재 MVP에서는 지하철 graph 구조와 Dijkstra 상태 확장을 함께 사용해 환승을 처리한다.

구현 방식:

- 지하철역은 하나의 node로 관리한다.
- 같은 역에 여러 노선이 연결되어 있으면 해당 역 node를 통해 다른 노선 edge로 이동할 수 있다.
- 예를 들어 5호선과 8호선이 만나는 역에서는 `5호선 subway_ride -> 환승역 node -> 8호선 subway_ride` 형태의 경로가 가능하다.
- Dijkstra는 이 graph 위에서 전체 비용이 가장 낮은 경로를 선택한다.
- 노선이 바뀌는 경우 환승 penalty를 추가해 최소환승 원칙을 반영한다.
- 경로 결과에서 연속된 `subway_ride`의 `line_code`가 바뀌면 안내 생성 단계에서 환승으로 판단한다.

즉, 현재 환승은 다음 두 단계로 처리된다.

```text
경로 탐색 단계
-> 환승역 node를 통해 노선 변경 가능
-> line_code 변경 시 환승 penalty 추가

안내 생성 단계
-> line_code 변경 감지 시 환승 안내 생성
```

현재 적용된 것:

- 지하철 노선 graph 포함
- 환승역에서 노선 간 연결 가능
- Dijkstra가 비용이 낮은 지하철 경로 선택
- 노선 변경 시 환승 penalty 적용
- 노선 변경 시 환승 안내 생성
- 환승역 내부 이동동선과 엘리베이터 안내 문장 생성

아직 명시적으로 구현하지 않은 것:

- 동일 노선 유지 bonus
- 환승역 내부 이동동선 길이/복잡도 기반 penalty

따라서 현재 방식은 환승을 절대적으로 금지하지 않고, 환승 penalty를 통해 불필요한 환승이 덜 선택되도록 유도하는 구조다. 접근성 측면에서 더 유리한 경우에는 환승이 포함될 수 있다.

기획서 표현 시에는 다음과 같이 쓰는 것이 정확하다.

> IEUM은 지하철 노선과 환승역을 graph에 통합하여 경로 탐색 과정에서 지하철 이동과 환승을 함께 고려한다. Dijkstra 탐색 상태에 현재 노선 정보를 포함하고, 노선 변경 시 환승 penalty를 부여하여 불필요한 환승을 억제한다. 노선 변경은 경로 결과에서 감지하여 환승 안내로 제공하며, 향후에는 환승역 내부 이동동선 길이와 복잡도까지 추가 가중치로 반영할 예정이다.

## 8. 경로 결과 요약

경로 산출 후 다음 정보를 함께 제공한다.

- 총 거리
- 총 시각장애인용 가중 비용
- edge 개수
- edge type별 개수
- 보행 거리
- 지하철 이동 거리
- 지하철 사용 노선
- 점자블록 직접 포함 거리
- 점자블록 근접 반영 거리
- 횡단보도 포함 거리
- 횡단보도 근접 반영 거리
- 음향신호기 포함 여부
- 음향신호기 근접 반영 거리
- 엘리베이터 연결 여부
- 저신뢰 데이터 포함 여부

예시 검증 경로:

```text
고덕로 210 -> 잠실 롯데타워
```

검증 결과:

- 총 거리: 7,587.8 m
- 시각장애인용 비용: 4,160.0
- 사용 지하철 노선: 5, 8
- 선택 경로에 반영된 점자블록: 456.3 m / 9개 edge
- 선택 경로에 반영된 횡단보도: 270.4 m / 6개 edge
- 선택 경로에 반영된 음향신호기: 442.1 m / 8개 edge

## 9. 안내 멘트 생성 방식

경로 탐색 결과를 기반으로 안내 멘트를 생성한다.

### 9.1 보행 안내

보행 edge는 geometry 좌표의 방향 변화를 분석해 안내를 나눈다.

기본 원칙:

- 점자블록 정보가 반영된 구간: `점자블록을 따라 이동`
- 점자블록 정보가 부족한 구간: `보행로를 따라 이동`
- 방향 변화가 크면 좌회전/우회전 안내 추가

예시:

```text
점자블록을 따라 약 66.6미터 이동하세요.
보행로를 따라 약 151.3미터 이동하세요. 이 구간은 경로에 반영된 점자블록 정보가 부족합니다.
좌회전해 보행로를 따라 약 108.0미터 이동하세요.
우회전해 점자블록을 따라 약 374.0미터 이동하세요.
```

### 9.2 횡단보도 안내

횡단보도는 보행신호와 음향신호기 정보를 반영한다.

예시:

```text
음향신호기가 있는 횡단보도를 약 13.4미터 건너세요. 음향 안내와 보행 신호를 확인하세요.
```

음향신호기 정보가 없으면 차량 흐름과 보행 신호 확인을 강조한다.

### 9.3 지하철 안내

지하철 구간은 다음 정보를 반영한다.

- 탑승 노선
- 출발역
- 도착역
- 이동 구간 수
- 환승 여부
- 역 내부 이동동선
- 엘리베이터 이동동선

예시:

```text
8호선을 이용해 암사역사공원역에서 잠실(송파구청)역까지 5개 구간 이동하세요.
잠실(송파구청)역 내부 이동: 1번 출입구 옆 엘리베이터 탑승
```

## 10. 지도 시각화 방식

웹 지도는 Leaflet을 사용한다.

기본 지도 기능은 유지하면서, 경로와 접근성 정보를 더 잘 구분하기 위해 다음 방식으로 표현한다.

- 경로 전체는 넓은 반투명 halo layer로 먼저 표시
- 실제 edge 선을 그 위에 한 번 더 표시
- 점자블록 반영 보행 구간은 주황색 계열
- 일반 보행 구간은 초록색 계열
- 횡단보도는 붉은색 또는 노란색 계열
- 지하철 연결/시설 연결은 보라색 계열
- 지하철 이동은 노선 색상 기반 표시
- 점자블록, 횡단보도, 음향신호기, 엘리베이터 layer는 별도 toggle로 확인 가능

이 방식은 단순 점/점선 표시보다 경로의 접근성 특성을 시각적으로 더 쉽게 파악할 수 있게 한다.

## 11. 현재 한계

현재 구현은 MVP 경로 산출 엔진이므로 다음 한계가 있다.

- 실시간 GPS 추적은 아직 미구현
- 경로 이탈 감지는 아직 미구현
- 자동 재탐색은 아직 미구현
- 좌회전/우회전은 도로명 기반이 아니라 geometry 방위각 변화 기반
- 지하철 내부에서는 GPS가 불안정하므로 별도 실내 안내 로직이 필요
- 실제 서비스 수준의 보행 내비게이션을 위해서는 현장 테스트가 필요

## 12. 다음 단계

다음 개발 단계는 실시간성 보강이다.

우선순위:

1. 브라우저 GPS `watchPosition()` 기반 현재 위치 추적
2. 현재 위치 marker 및 진행 방향 표시
3. 현재 위치와 route polyline 간 거리 계산
4. 다음 안내 지점까지 남은 거리 계산
5. 25~40m 이상 이탈 시 경고
6. 2~3회 연속 이탈 시 재탐색
7. 지하철 내부 구간에서는 재탐색 민감도 완화

이 단계가 완료되면 IEUM은 정적 경로 안내에서 실시간 보행 내비게이션 MVP로 확장된다.
