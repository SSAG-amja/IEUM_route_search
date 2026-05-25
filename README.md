# IEUM Seoul Accessibility Dataset

서울시 시각장애인 보행 및 지하철 접근성 경로 탐색 MVP 데이터셋과 실행 서버입니다.

## 데이터 구성

이 폴더는 두 종류의 gz 데이터를 함께 보관합니다.

1. 최종 통합 경로 graph
2. 원천 프로젝트 gz 전체 사본

### 최종 통합 graph

경로 탐색용으로 직접 사용하는 압축 데이터입니다.

```text
data_gz/ieum_route_graph_nodes.geojson.gz
data_gz/ieum_route_graph_edges.geojson.gz
data_gz/dataset_manifest.json.gz
data_gz/ieum_accessibility_rules.json.gz
data_gz/route_test_cases.json.gz
```

### 실행용 지도 layer

웹 지도 layer 표시와 접근성 보강에 바로 쓰는 압축 데이터입니다.

```text
data_gz/layers/braille_network_links.geojson.gz
data_gz/layers/crosswalk_links_enriched.geojson.gz
data_gz/layers/audible_signal_points.geojson.gz
data_gz/layers/subway_elevators.geojson.gz
data_gz/layers/merged_station_points.geojson.gz
data_gz/layers/line_segments_display.geojson.gz
```

### 원천 gz 전체 사본

`nav_map/data_gz`와 `subway_station_catalog/data_gz`의 모든 gz 파일을 빠짐없이 복사해 보관합니다.

```text
data_gz/source/nav_map/*.gz
data_gz/source/subway_station_catalog/*.gz
```

현재 복사 기준:

```text
nav_map source gz: 11 files
subway_station_catalog source gz: 10 files
```

이 source 폴더는 원천 데이터 추적, 제출 자료 정리, 재검증을 위한 archive입니다. 서버 실행에는 주로 최종 통합 graph와 `data_gz/layers`가 사용됩니다.

## GitHub 업로드 기준

GitHub 일반 저장소는 100MB 이상 파일 push가 제한됩니다. 따라서 다음 파일은 commit하지 않습니다.

```text
.env
routing/ieum_graph.sqlite
data/*.geojson
data/*.json
routing/results/
__pycache__/
```

대신 `data_gz/`의 압축 데이터는 commit합니다.

서버 첫 실행 시 `routing/ieum_graph.sqlite`가 없으면 `data_gz`를 읽어 SQLite DB를 자동 생성합니다.

## 필수 commit 파일

```text
data_gz/ieum_route_graph_nodes.geojson.gz
data_gz/ieum_route_graph_edges.geojson.gz
data_gz/dataset_manifest.json.gz
data_gz/ieum_accessibility_rules.json.gz
data_gz/route_test_cases.json.gz
data_gz/layers/*.geojson.gz
data_gz/source/nav_map/*.gz
data_gz/source/subway_station_catalog/*.gz
routing/*.py
routing/web/*
.env.example
.gitignore
requirements.txt
README.md
```

## 실행 준비

Python 3.10 이상을 권장합니다. 현재 서버는 Python 표준 라이브러리만 사용합니다.

Kakao 주소/장소명 검색을 쓰려면 `.env.example`을 참고해 `.env` 파일을 만듭니다.

```text
KAKAO_REST_API_KEY=your_kakao_rest_api_key_here
```

`.env`가 없으면 주소/장소명 검색은 제한됩니다. 좌표 직접 입력 테스트는 가능합니다.

## 실행 명령어

```powershell
python routing\route_server.py
```

기본 접속 주소:

```text
http://localhost:8020/
```

다른 포트:

```powershell
$env:IEUM_ROUTE_PORT=8021
python routing\route_server.py
```

Git Bash:

```bash
IEUM_ROUTE_PORT=8021 python routing/route_server.py
```

## 첫 실행 동작

`routing/ieum_graph.sqlite`가 없으면 서버가 자동으로 다음 작업을 수행합니다.

1. `data_gz/ieum_route_graph_nodes.geojson.gz` 읽기
2. `data_gz/ieum_route_graph_edges.geojson.gz` 읽기
3. SQLite graph DB 생성
4. 접근성 근접 정보 보강
5. 엘리베이터, 지하철 내부 이동동선, 환승 비용 반영
6. 서버 시작

첫 실행은 DB 생성 때문에 몇 분 걸릴 수 있습니다. 이후부터는 생성된 SQLite를 바로 읽기 때문에 더 빠르게 시작됩니다.

## 주요 API

경로 검색:

```text
GET /api/route?start=고덕로%20210&end=잠실%20롯데타워
```

좌표 직접 입력:

```text
GET /api/route?start=127.14854472216015,37.55269966641881&end=126.9677018192213,37.5519822941232
```

데이터 layer:

```text
GET /api/dataset?name=subway_line
GET /api/dataset?name=subway_station
GET /api/dataset?name=subway_elevator
GET /api/dataset?name=braille
GET /api/dataset?name=crosswalk
GET /api/dataset?name=audible
```

안내 멘트 템플릿:

```text
GET /api/instruction-templates
```

## 최종 경로 원칙

현재 경로 선택 원칙:

- 점자블록 있는 길 우선
- 음향신호기 있는 횡단보도 우선
- 보행신호 없는 횡단보도 회피
- 엘리베이터 연결 우선
- 지하철 내부 이동동선 보유 역 우선
- 지하철 장거리 이동 허용
- 환승 penalty로 불필요한 환승 억제
- 데이터 신뢰도 낮은 길 회피
- 전체 비용이 가장 낮은 경로를 Dijkstra로 선택

자세한 경로 산출 방식은 상위 폴더의 `IEUM_route.md`를 참고합니다.
