# CalSync 프로젝트 계획서 v0.3

- 갱신: 2026-11-11
- 우선순위: **수신부(Docker, Nginx Proxy Manager, Google Calendar) 우선 구축**, 송신부는 최소 프로토타입만 제공하며 상세 완성은 사용자 측에서 수행.
- 상태: **수신부 MVP 소스 커밋 완료 / 실제 서버 배포 및 외부 연동 미검증**
- 수신·배포 상세: [RECEIVER_DESIGN.md](RECEIVER_DESIGN.md)
- 운영 설치: [README.md](../README.md)

## 1. 목표 및 결정 사항

**CalSync**는 지정된 Google Calendar의 이벤트를 Menlo Browser 이용자에게 보여주고, 신규 일정 생성/기존 일정 삭제 요청을 전달받아 Google Calendar API에 반영하는 서비스다.

### 확정 요구사항

1. 접속자는 **Menlo Security Browser service**를 통해 HTTPS 443 도메인에 접속한다.
2. 접속자 PC에서 외부로의 **직접 통신은 모든 포트에서 차단**되어 있다. 일반적인 로컬 REST POST, WebSocket, 주기적 서버 폴링은 불가능하다.
3. 기존에는 Menlo Browser에서 서버의 캘린더 사이트에 접속해 일정 정보 텍스트를 직접 입력·등록할 수 있었다.
4. 수신부는 **Docker 컨테이너**로 서버에 설치하며 **Nginx Reverse Proxy Manager**에서 도메인 연결 및 HTTPS를 제공한다.
5. Google Calendar의 **관리자가 지정한 특정 Calendar ID 하나**만 조회하고 수정한다. 외부 JSON의 \`calendarId\`는 신뢰하지 않는다.
6. 신규 이벤트의 제목, 시작/종료 일시, 장소, 내용, 참석자, 메모 등을 웹에서 제출하는 요구가 있다. **초기 JSON 연동에서는 아래 다섯 필드만 사용**한다.
7. 기존 일정에 대해서는 **삭제 요청**을 제출할 수 있다.
8. Desktop Client는 로컬 JSON 자동 수집/분석/변경 감지를 목표로 하지만, 현재 네트워크 조건에서 완전 무인 업로드는 보장되지 않는다. 일단 **Menlo 브라우저를 통한 사용자 제출**을 MVP로 한다.

### JSON 구조 (사용자가 제공한 정보)

| 입력 JSON 키 | 사용? | 의미 및 반영 |
| --- | --- | --- |
| \`subject\` | **예** | Google event \`summary\` |
| \`startTime\` | **예** | 객체 \`date\`, \`dateTime\`, \`timeZone\` → \`start\` |
| \`endTime\` | **예** | 객체 \`date\`, \`dateTime\`, \`timeZone\` → \`end\` |
| \`chair\` | **예** | 객체 \`displayName\`, \`id\`, \`email\`; 현재 \`displayName\`을 description에 주최자 명칭으로 표시 |
| \`location\` | **예** | Google event \`location\` |
| \`type\` | 아니요 | 무시 |
| \`uid\` | 아니요 | 현재 미사용, 추후 내부 식별자로만 쓸지 검토 |
| \`calendarId\` | 아니요 | 서버에 설정한 수신 대상 calendar ID만 사용 |
| \`attendees\` | 아니요 | JSON에서 읽지 않음, 초대 발송 안 함 |
| \`URL\` | 아니요 | 무시 |

- Google Calendar의 \`organizer\`/일정 생성자를 \`chair\`와 동일한 계정으로 바꾸는 기능은 제공하지 않는다.
- \`dateTime\`이 있으면 시간 지정 일정으로 우선 해석하고, 없으면 \`date\`를 종일 일정으로 해석한다. 실제 값 형태가 확정되면 어댑터를 수정한다.
- Google 종일 이벤트 종료일(\`end.date\`)은 **exclusive**이므로 샘플에서 원본 semantics를 확인해야 한다.
- JSON 전체 래퍼, ID/삭제 표시 규칙, 반복 일정은 샘플 제공 후 검토한다.
- JSON의 5개 필드 제한은 **자동 추출 대상**의 규칙이다. 초기 웹 수동 폼은 동일 필드의 1건 요청을 지원하며, 별도 내용/참석자/메모 웹 입력은 후속 확장 대상으로 남긴다.

## 2. 통신 설계

~~~text
[로컬 JSON] ---> [Desktop Client / prepare.py (오프라인)]
                           |
                  준비된 JSON 텍스트
                           |
                      [사용자]
                           |
              [Menlo Browser - HTTPS 443]
                           |
            [Nginx Proxy Manager: TLS 도메인]
                           |
            [CalSync Docker / FastAPI :8000]
                 |                  |
           [접수 DB/승인]     [Google Calendar API]
                                    |
                         [고정된 Calendar ID]
~~~

일정 생성/삭제 요청은 웹에서 \`POST\`한다. HTML Form POST와 JavaScript \`fetch POST\`는 둘 다 브라우저 HTTP 통신이므로 **동일 원점의 일반 웹 제출 수단**이다. 단, Menlo가 임의 JS fetch·클립보드·파일 업로드까지 허용하는지는 실환경 검증이 필요하다. **로컬 Desktop Client 직접 POST와는 통신 경로가 다르다.**

**수신 방식 확정:** 접속자는 Menlo로 CalSync 사이트에 접속해 웹 UI의 단건 입력 또는 다건 JSON 일괄 입력을 사용한다. 서버는 수신을 우선 DB의 PENDING 상태로 저장한 뒤 승인·검증을 거쳐 Google API를 호출한다.

## 3. 수신부 주요 컴포넌트

| 위치 | 구현 | 책임 |
| --- | --- | --- |
| Nginx Proxy Manager | HTTPS 443 도메인 프록시 | TLS 종료, 외부 접근 정책 |
| Docker CalSync | Python FastAPI, Uvicorn | 웹 UI, 입력 검증, 권한, 수신 API |
| Docker 볼륨 | SQLite DB | 요청 상태, 승인 처리, Google ID 매핑 |
| 서버 secrets | OAuth Refresh Token 파일 (읽기 전용) | 지정 Google 계정에서 Calendar API 호출 |
| Desktop | Python \`sender/prepare.py\` | 로컬 JSON 5개 필드 추출, 붙여넣기용 데이터 생성 |

**OAuth 방식 확정:** 운영자가 1회 Google 계정 동의(Desktop OAuth flow)로 Refresh Token을 발급하여 서버에 마운트한다. Scope는 \`https://www.googleapis.com/auth/calendar.events\`이고, Calendar ID는 서버 환경변수로 고정한다. Workspace 위임 및 서비스 계정은 초기 버전에서 사용하지 않는다.

**수신 인터페이스 확정:** \`POST /api/v1/requests/import\`로 \`{"batchId":"...","events":[...]}\`을 받는다. 브라우저 UI는 텍스트 붙여넣기에서 JSON 배열·단일 객체를 받아 이 형태로 변환하고, 동일 원점으로 POST한다. 배치 최대 100건.

**삭제 인터페이스 확정:** \`POST /api/v1/requests/delete\`로 \`eventId\`, \`expectedEtag\`를 받는다. 해당 이벤트가 지정 Calendar에 존재하는지 확인하고 PENDING으로 접수한다. 승인 시 변경 여부를 재확인한다.

## 4. 안전성 및 정책

- 승인 전 Google Calendar는 변경되지 않는다. 관리자 승인 실행 때만 삽입/삭제.
- 인증: 사용자별 ID+Argon2id 비밀번호 해시, DB에 저장되는 취소 가능 opaque 세션(HttpOnly+Secure+SameSite=Strict), CSRF 및 Origin 검증. viewer/submitter/admin 역할 분리.
- 원본 JSON의 \`calendarId\`를 무시하고 고정 Calendar ID만 사용.
- 삽입 요청에는 batchId/item 순서의 멱등성, Google 이벤트에는 미리 생성한 이벤트 ID를 사용해 반복 실행 중복을 방지한다.
- 삭제는 **CalSync 웹 UI에서 선택한 Google event ID를 기반**으로 한다. 원본 JSON 항목이 파일에서 사라졌다는 이유만으로 자동 삭제하지 않는다.
- 이미 변경된 Google 일정의 삭제는 ETag 충돌로 차단한다.
- Google OAuth 비밀·실제 일정·기업 네트워크 정보는 공개 GitHub 저장소에 올리지 않는다.
- 조직의 Menlo 정보 반출/Google Calendar 사용 정책을 따르며 보안 통제를 우회하지 않는다.
- 현 웹 역할은 공유 암호 기반의 초기 MVP로서 사용자별 권한/감사 로깅은 아직 고도화 대상이다.

## 5. 작업 단계 및 인수 기준

| Phase | 주 작업 | 완료 기준 |
| --- | --- | --- |
| 0. 통신/보안 검증 | Menlo POST/fetch·붙여넣기 허용, NPM 접속 | 승인된 경로로 웹 요청 제출 확인 |
| 1. 수신부 Docker | FastAPI + 데이터 저장 + HTML UI | Compose로 실행, 로그인, 접수 및 조회 |
| 2. Google 연동 | 운영자 OAuth, 고정 캘린더 조회/쓰기 | 실계정 조회, 테스트 일정 insert/delete 검증 |
| 3. 관리자 승인 | 수신→PENDING→승인/거절→Google 반영 | 중복 생성·삭제 충돌 검증 |
| 4. 로컬 도구 최소판 | JSON 5개 필드 변환, 텍스트 출력 | CLI 생성·Menlo 브라우저 붙여넣기 검증 |
| 5. 사용자 측 송신부 고도화 | 원본 JSON 샘플·자동 감시·삭제 판별 | 사용자 구현, 수신 계약 유지 |
| 6. 운영 안정화 | 인증 고도화, rate limit, 작업 복구, 테스트 | 실패 복구/감사/권한 점검 |

현재 리포지터리에는 **Phase 1~4에 대한 MVP 소스**를 추가했다. 각 단계의 실환경 테스트/인수는 아직 진행되지 않았다.

## 6. 남은 결정 항목

1. 서버에서 선택한 **Google Calendar ID** 및 OAuth 사용자 계정.
2. 실제 외부 도메인, NPM Container/Docker Network 연결.
3. Menlo Browser가 JSON 텍스트 붙여넣기와 같은 원점 fetch POST를 허용하는지.
4. 실제 JSON 파일의 최상위 구조 및 날짜 타입, 시간대 표기, 삭제 상태 표현.
5. 수동 입력에서 '내용/참석자/메모'를 Google에 어떻게 보존할지, 참석자 초대 알림이 필요한지.
6. MFA/SSO 필요성, 사용자별 이벤트 접근 범위와 자동 승인 정책 (기본적으로 관리자 수동 승인).
7. 업무 일정 및 개인정보 외부 전송에 대한 조직 승인 상태.
8. 전송 성공 결과를 송신 PC로 어떻게 가져올지(현 직접 네트워크 제한하에서는 수동 확인).

## 7. 참조

- [수신부 상세 설계](RECEIVER_DESIGN.md)
- [설치 및 배포](../README.md)
- [Google Calendar Events API](https://developers.google.com/workspace/calendar/api/v3/reference/events)
- [Google Calendar OAuth scopes](https://developers.google.com/workspace/calendar/api/auth)


## 보안 구현 업데이트 (2026-11-11)

- Docker 수신부는 **외부 HTTPS 443만 노출**한다. NPM이 TLS를 종료하며 Backend 8000은 사설 Docker 네트워크에서만 접근한다.
- 인증은 JWT 대신 **Argon2id 비밀번호 + DB 기반 opaque 세션 쿠키**를 사용한다. 사용자 계정 관리와 세션 폐기 지원.
- 조회/전송 API 전부 로그인 필수. 등록·삭제 요청은 submitter 또는 admin, Google API 실제 쓰기는 admin 승인 시만 허용.
- 로그인 실패 제한, CSRF, Origin, CSP/HSTS 등 기본 보안 적용. 인프라 방화벽/프록시 요청 제한은 별도 설정 필요.
- TLS는 **전송 중 암호화**이며 저장 데이터의 E2EE는 제공하지 않는다. DB와 OAuth 토큰 파일은 서버측 접근 제한 대상이다.
