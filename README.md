# CalSync

**Menlo Security Browser를 이용하는 사용자의 Google Calendar 일정 수신 서비스**입니다. 수신부는 Docker + FastAPI + Google Calendar API로 구성하고 Nginx Proxy Manager를 통해 HTTPS 도메인에 연결합니다. 로컬 JSON 자동 분석·전송 준비는 가벼운 Python 스크립트로 제공하며, 외부로 직접 통신하지 않습니다.

> **현재 상태:** 수신부 MVP 소스와 Docker 설정이 리포지터리에 있습니다. 실제 도메인, Google OAuth 및 Menlo 환경에서 실행·통합 검증한 상태는 아닙니다. 배포 전 아래 검증 단계를 진행하세요.

## 확정된 통신 방식

~~~text
[접속자 PC JSON]
   → sender/prepare.py (오프라인 5개 필드 추출)
   → 사용자가 Menlo 브라우저에 복사/입력 (붙여넣기 허용 여부 확인 필요)
   → https://calendar.example.com (Nginx Proxy Manager :443)
   → Docker: calsync:8000 (FastAPI)
   → 관리자 승인
   → Google Calendar API (서버 운영자 OAuth)
   → 서버에서 지정한 1개 Google Calendar
~~~

Menlo 브라우저 내 일반 Form POST 또는 JS fetch POST를 사용합니다. **PC 프로그램이 외부 서버에 직접 POST하는 방식은 사용하지 않습니다.** JSON에 쓰이는 내용 필드는 \`subject\`, \`startTime\`, \`endTime\`, \`chair\`, \`location\`의 다섯 개이며, 그 외 \`type\`/\`uid\`/\`calendarId\`/\`attendees\`/\`URL\`은 Google 이벤트 콘텐츠로 전송하지 않습니다.

## 서버 설치

### 1. Google Calendar API OAuth 준비

1. Google Cloud 프로젝트 생성 및 **Google Calendar API 활성화**.
2. OAuth 동의 화면을 구성하고, **Desktop app** 유형 OAuth 클라이언트를 생성.
3. 운영자 PC에 클라이언트 JSON을 \`credentials.json\`으로 내려받습니다. 저장소에는 커밋하지 않습니다.
4. 지정 Google Calendar에 편집 권한이 있는 **운영자 Google 계정**으로 다음 명령을 실행합니다.

~~~bash
python -m pip install google-auth-oauthlib
mkdir -p secrets
python scripts/google_oauth_setup.py --credentials credentials.json --output secrets/google-token.json
~~~

이 동의로 \`https://www.googleapis.com/auth/calendar.events\` 권한이 부여됩니다. Refresh Token은 서버 측에서만 사용합니다. **개인 계정/Workspace 관리 정책과 OAuth 게시 상태에 따라 사용 승인이 필요할 수 있습니다.**

### 2. 서버 환경변수

~~~bash
cp .env.example .env
mkdir -p data secrets
id -u  # CALSYNC_UID 로 사용
id -g  # CALSYNC_GID 로 사용
~~~

\`.env\`를 열어 반드시 다음을 수정합니다.

- \`CALSYNC_CALENDAR_ID\`: Google Calendar 설정에서 확인한 정확한 Calendar ID.
- \`CALSYNC_PUBLIC_ORIGIN\`: 실제 HTTPS 주소(예: \`https://calendar.example.com\`, 뒤에 경로/슬래시 없음).
- \`CALSYNC_SESSION_SECRET\`: 무작위 32자 이상.
- \`CALSYNC_SUBMITTER_PASSWORD\`, \`CALSYNC_ADMIN_PASSWORD\`: 서로 다른 충분히 강한 비밀번호.
- \`CALSYNC_UID\`, \`CALSYNC_GID\`: 호스트에서 \`id -u\`, \`id -g\` 결과.

생성한 \`secrets/google-token.json\`은 **서버의 해당 디렉터리로 안전하게 전송**하고 파일 권한을 제한해야 합니다. 실행 UID가 파일을 읽고 \`data/\`에 쓰기 가능해야 합니다. 저장소에 \`.env\` 또는 OAuth 토큰을 올리지 마세요.

### 3. Docker 실행

~~~bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 calsync
~~~

\`docker-compose.yml\`은 컨테이너 \`8000/tcp\`만 \`expose\`하고, **호스트에 외부 포트를 열지 않습니다.**

### 4. Nginx Proxy Manager 연결

NPM 컨테이너와 CalSync를 **동일 Docker 네트워크**에 연결합니다. 기본 Compose 프로젝트명/네트워크가 \`calsync_default\`라고 가정한 예:

~~~bash
docker network ls
docker network connect calsync_default <실제_NPM_컨테이너_이름>
~~~

NPM의 Proxy Host 설정:

| 항목 | 설정 |
| --- | --- |
| Domain Names | 원하는 CalSync 도메인 |
| Scheme / Forward Hostname | \`http\` / \`calsync\` |
| Forward Port | \`8000\` |
| SSL | 신뢰 가능한 인증서, **Force SSL** |
| 외부 접속 | **HTTPS 443**, NPM 경유 |
| Websocket Support | 기본 기능에 필요하지 않음 |

실제 프로젝트 네트워크명은 \`docker network ls\` 및 \`docker inspect calsync\`로 확인하세요. NPM 컨테이너를 재생성하면 수동 네트워크 연결이 사라질 수 있으므로 NPM Compose에 CalSync 네트워크를 **external network**로 명시하는 배포 구성이 권장됩니다.

브라우저에서 \`https://도메인/login\`에 접속하여 \`CALSYNC_SUBMITTER_PASSWORD\` 또는 \`CALSYNC_ADMIN_PASSWORD\`로 로그인합니다. NPM의 별도 접근 제한 및 요청 레이트 리미팅도 권장합니다.

## 수신 및 승인

- 접속자: Google Calendar 일정 조회 → **신규 일정 입력/JSON 일괄 붙여넣기** → 생성 요청 → 처리 상태 확인.
- 접속자: 일정의 **삭제 요청** 버튼 → 해당 Google event ID와 ETag로 요청 생성.
- 관리자: 요청 목록에서 승인/거절. **승인한 경우에만 Google Calendar에 실제 insert/delete**.
- 전송 중 오류로 같은 batchId를 재제출해도 동일 접수 요청을 반환합니다.
- 최대 일괄 건수: 100개. 동일 캘린더만 사용합니다.
- JSON에서 단순 파일 누락은 삭제 요청이 아닙니다.

### 오프라인 보내는 쪽 최소 예제

~~~bash
python sender/prepare.py my-events.json > prepared.json
~~~

생성된 \`prepared.json\` 텍스트를 **사용자가** Menlo Browser의 CalSync JSON 일괄 입력창에 넣습니다. 원본 JSON의 최상위 형태가 실제 샘플과 다르거나 삭제 항목을 자동 판별해야 한다면 보내는 쪽에서 확장하세요.

현재 Menlo 정책에서 **일반 텍스트 폼 제출은 과거 동작이 확인되었지만, 클립보드 붙여넣기와 JavaScript fetch POST는 아직 검증 전**입니다. 해당 정책이 차단하면 브라우저 직접 입력을 이용하거나 공식 승인을 받으세요.

## 소스 위치

- \`app/main.py\`: FastAPI, 인증/CSRF, Google Calendar 연동, 승인 워크플로
- \`app/static/index.html\`: Menlo 웹 화면 (GET/POST)
- \`scripts/google_oauth_setup.py\`: 운영자 1회 OAuth 발급
- \`sender/prepare.py\`: JSON 오프라인 변환기(네트워크 통신 없음)
- \`docs/RECEIVER_DESIGN.md\`: 구체 수신/배포 아키텍처 및 API
- \`docs/PROJECT_PLAN.md\`: 전체 프로젝트 계획

## 운영 전 필수 확인

- Google API 인증·캘린더 편집 권한 및 OAuth 게시 상태
- Menlo 브라우저에서 로그인/쿠키/JS fetch/클립보드 정책 확인
- NPM HTTPS/인증/IP 제한, 호스트 및 컨테이너 토큰 파일 접근 권한 확인
- 실제 JSON 샘플의 구조·종일 일정 종료일 의미 확인
- **회사 일정 정보를 외부 서버/Google Calendar로 내보내는 행위에 대한 조직 정책 승인**

이 저장소는 **공개 저장소**입니다. 개인·업무 일정, 참석자 정보, OAuth Refresh Token, 비밀번호, 내부 URL은 커밋하지 마세요.
