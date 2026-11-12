# CalSync

## Windows Desktop 클라이언트

**Windows GUI·JSON 자동 감시·전송 대기열·영수증 동기화·시스템 트레이·Windows 로그인 자동 시작·EXE 빌드**가 추가되었습니다.

- [Windows 실행/설정 안내](sender/windows/README.md)
- [Windows 소스: GUI](sender/windows/app.py) · [오프라인 동기화 엔진](sender/windows/core.py)
- [Windows EXE 다운로드 (GitHub Actions 성공한 실행의 Artifacts)](https://github.com/dark7sky/CalSync/actions/workflows/windows-client.yml)

**Menlo 제약:** PC의 직접 외부 연결이 차단되어 있어, 클라이언트가 로컬 JSON을 자동 스캔하고 전송 대기열을 준비하더라도 **Menlo 웹페이지에서 사용자가 붙여넣기/제출해야 합니다.** 서버에서 승인한 결과를 반영하려면 웹의 영수증 JSON을 Windows 앱으로 가져오세요. 공식적으로 승인된 프로그램-브라우저 전달 경로 없이 완전 무인 동기화를 보장하지 않습니다.

감시 중 X 버튼을 누르면 **트레이로 / 완전히 종료 / 취소**를 선택할 수 있고, 로그인 시 트레이 자동 실행도 설정할 수 있습니다.


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

### 계정 생성 및 접근 권한

서버를 최초 실행한 후 **계정을 별도로 만들어야 로그인할 수 있습니다.** 보안상 기본 관리자나 초기 비밀번호는 제공하지 않습니다.

~~~bash
docker compose up -d --build
docker compose exec -it calsync python -m app.security add-user --username admin --role admin
docker compose exec -it calsync python -m app.security add-user --username sender1 --role submitter
# 읽기 전용 접속자가 필요한 경우
docker compose exec -it calsync python -m app.security add-user --username reader1 --role viewer

# 계정 확인, 비밀번호 재설정, 계정 비활성화
docker compose exec calsync python -m app.security list-users
docker compose exec -it calsync python -m app.security reset-password --username sender1
docker compose exec calsync python -m app.security disable-user --username sender1
~~~

각 계정은 **14자 이상의 비밀번호**를 대화형으로 입력합니다(쉘 명령어 인자로 비밀번호 전달하지 않음). 로그인 시 사용자 이름과 비밀번호를 함께 입력합니다.

- **viewer:** 인증 후 캘린더 읽기만.
- **submitter:** 캘린더 읽기, 본인의 신규 등록·삭제 요청 및 상태 확인.
- **admin:** 캘린더 읽기, 전체 요청 검토·승인·거절.
- OAuth 토큰은 Google 서버 통합 전용이며 접속자에게 배포하지 않습니다.

### 외부 도메인 보안

- 전송 암호화: **클라이언트 ↔ Menlo Browser ↔ HTTPS 443(NPM)**; NPM에서 TLS 종료 후 같은 서버의 비공개 Docker 네트워크로 CalSync 연결. **NPM HTTPS 인증서를 정상 구성하고 Force SSL을 활성화**하세요. Google API도 HTTPS입니다.
- 인증: Argon2id 비밀번호 해시, 데이터베이스에서 즉시 폐기 가능한 **난수 기반 HttpOnly + Secure + SameSite=Strict 세션 쿠키**. JWT를 브라우저에 저장하지 않습니다.
- 권한: 모든 이벤트 조회/등록/삭제 요청 및 이력 조회 API는 로그인 필수. 등록/삭제 요청은 submitter/admin만 가능하고, **Google 일정 실제 수정은 admin 승인 필요**.
- 요청 보호: CSRF 토큰 + Origin 검사, 15분간 동일 사용자 이름 로그인 실패 10회 제한, Content Security Policy, HSTS, X-Frame-Options.
- 추가 권고: NPM Access List, \`client_max_body_size 1m\`, NPM 수준의 로그인 요청 속도 제한, 서버 OS 방화벽, 최소권한 백업, 전체 디스크 암호화. 이들 NPM/호스트 설정은 애플리케이션 코드만으로 자동 적용되지 않습니다.
- **TLS는 전송 구간 암호화이지 Google Calendar나 SQLite에 저장되는 내용의 종단 간 암호화(E2EE)는 아닙니다.** \`data/\`와 \`secrets/\`는 호스트 관리자만 접근하도록 제한하세요.

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
