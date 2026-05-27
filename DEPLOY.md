# IEUM Route API 배포 가이드

이 문서는 AWS EC2 Ubuntu 프리티어 환경에서 `IEUM_route_search` 백엔드를 Docker와 Nginx 뒤에 올리는 기준 절차입니다.

## 권장 디렉터리 구조

운영 서버에서는 보통 앱별로 `/srv` 또는 `/opt` 아래에 프로젝트를 둡니다. 이 프로젝트는 다음 구조를 추천합니다.

```bash
/srv/ieum/IEUM_route_search
```

예시 준비:

```bash
sudo mkdir -p /srv/ieum
sudo chown -R $USER:$USER /srv/ieum
cd /srv/ieum
git clone <YOUR_REPOSITORY_URL> IEUM_route_search
cd IEUM_route_search
mkdir -p storage
cp .env.example .env
```

`.env`에는 최소한 아래 값들을 채워주세요.

```env
KAKAO_REST_API_KEY=your_kakao_rest_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here
```

## Docker 구성

이 폴더에는 아래 파일이 이미 준비되어 있습니다.

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`

핵심 조건:

- FastAPI 컨테이너 내부 포트는 `8000`
- 호스트에는 `127.0.0.1:8020:8000`으로만 바인딩
- 외부에서는 `8020`에 직접 접근 불가
- Nginx만 80 포트에서 받아 내부 `127.0.0.1:8020`으로 전달
- `restart: always` 포함
- SQLite DB는 `./storage/ieum_graph.sqlite`에 영속 저장

## Nginx 구성

샘플 파일:

```text
deploy/nginx/ieum-route.conf
```

EC2에서 적용:

```bash
sudo cp deploy/nginx/ieum-route.conf /etc/nginx/sites-available/ieum-route.conf
sudo ln -sf /etc/nginx/sites-available/ieum-route.conf /etc/nginx/sites-enabled/ieum-route.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

Nginx는 외부 `http://52.78.2.168/...` 요청을 내부 Docker 백엔드 `http://127.0.0.1:8020/...`로 전달합니다.

## 실행 순서

1. Docker 및 Compose 플러그인 설치

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

로그아웃 후 다시 접속하거나 `newgrp docker`를 실행합니다.

2. 프로젝트 이동

```bash
cd /srv/ieum/IEUM_route_search
```

3. 환경 파일 준비

```bash
cp .env.example .env
mkdir -p storage
```

4. 이미지 빌드 및 백그라운드 실행

```bash
docker compose up -d --build
```

5. 컨테이너 상태 확인

```bash
docker compose ps
```

6. 로컬 헬스 체크

```bash
curl -i http://127.0.0.1:8020/health
```

7. 외부 진입 헬스 체크

```bash
curl -i http://52.78.2.168/health
```

정상이라면 둘 다 `200 OK`와 `{"status":"ok"}`를 반환합니다.

## 프론트에서 사용할 BASE URL

현재 조건에서는 아래 주소 하나만 고정하면 됩니다.

```text
http://52.78.2.168
```

예:

- `http://52.78.2.168/api/v1/routes`
- `http://52.78.2.168/api/v1/voice/destination`

## 실시간 로그 확인

Nginx access log:

```bash
sudo tail -f /var/log/nginx/access.log
```

Nginx error log:

```bash
sudo tail -f /var/log/nginx/error.log
```

Docker 컨테이너 실시간 로그:

```bash
docker compose logs -f
```

백엔드 서비스 로그만 보고 싶다면:

```bash
docker compose logs -f ieum-route-api
```

## 트러블슈팅

### 1. `502 Bad Gateway`

확인 순서:

```bash
docker compose ps
docker compose logs -f ieum-route-api
curl -i http://127.0.0.1:8020/health
```

대개 원인은 아래 셋 중 하나입니다.

- 컨테이너가 아직 기동 중이거나 종료됨
- Python 의존성 또는 `ffmpeg` 문제
- 첫 SQLite 생성이 진행 중이라 시작이 오래 걸리는 상태

첫 실행은 `storage/ieum_graph.sqlite` 생성 때문에 몇 분 걸릴 수 있습니다.

### 2. 음성 인식에서 `400 Bad Request`

컨테이너 로그를 먼저 봅니다.

```bash
docker compose logs -f ieum-route-api
```

주요 원인:

- 업로드 오디오 포맷 불일치
- `ffmpeg` 부재
- Whisper 디코딩 실패

이 Dockerfile은 `ffmpeg`를 포함하므로 서버 기본 구성 문제는 줄어듭니다.

### 3. Gemini 목적지 추출이 비어 있음

`.env`의 `GEMINI_API_KEY`를 다시 확인합니다.

```bash
docker compose exec ieum-route-api env | grep GEMINI
```

### 4. Nginx는 정상인데 앱이 응답 없음

아래 두 로그를 동시에 보면 흐름이 바로 보입니다.

```bash
sudo tail -f /var/log/nginx/access.log
docker compose logs -f ieum-route-api
```

판단 기준:

- Nginx access log만 찍히고 Docker 로그가 안 찍히면 프록시 설정 문제
- 둘 다 찍히는데 4xx/5xx면 애플리케이션 로직 또는 요청 데이터 문제

## 운영 자주 쓰는 명령어

재시작:

```bash
docker compose restart
```

재빌드 포함 재배포:

```bash
docker compose up -d --build
```

중지:

```bash
docker compose down
```

이미지 정리:

```bash
docker image prune -f
```
