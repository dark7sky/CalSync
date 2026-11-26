# CalSync 수신부 확정 설계 — Docker + Nginx Proxy Manager + Google Calendar

상태: 최초 설계 기록. 현재 구현은 승인 대기 없이 인증된 submitter/admin의 요청을 즉시 실행한다.

## 확정 경로

~~~text
접속자 로컬 JSON
    └─ Python prepare.py (오프라인, 5개 내용 필드만 추출)
         └─ 복사 / Menlo Browser의 CalSync 페이지에 붙여넣기
              └─ HTTPS 443 도메인 (Nginx Proxy Manager)
                   └─ Docker CalSync FastAPI :8000 (공용 포트 미노출)
                        ├─ SQLite 요청 DB (/data 영속 볼륨)
                        ├─ 사용자 조회/등록/삭제 요청
                        ├─ 요청 즉시 실행 및 처리 이력
                        └─ Google Calendar REST API (서버에서 OAuth 토큰으로 호출)
~~~

- 웹에서 직접 텍스트를 등록하던 기존 방식과 동일한 **브라우저 HTTP POST**로 수신한다.
- 브라우저 내부 JavaScript \`fetch\`는 같은 원점으로 POST하므로 클라이언트 PC의 직접 외부 API 접속이 불필요하다.
- **로컬 프로그램이 직접 \`requests.post\`를 실행하는 구조는 채택하지 않는다.**
- 브라우저가 로컬 JSON 내용을 전달받는 단계(클립보드 붙여넣기/파일 업로드)는 Menlo 정책에 따라 달라진다. 현재는 **직접 폼 입력은 기존 동작 경험이 있고**, 일괄 붙여넣기는 실환경 검증이 필요하다.
- 무인 동기화(사용자 개입 없이 로컬→브라우저로 업로드)는 현재 범위에 포함되지 않는다.
- API는 컨테이너 내부 :8000에서 실행하고, NPM에서 TLS를 종료한다. NPM에서 HTTPS 443으로만 외부 노출한다.

## Google 인증 결정

- 서버 운영자가 Google Cloud 프로젝트에서 Calendar API를 활성화하고 OAuth 동의 화면/데스크톱 OAuth 클라이언트를 만든다.
- 서버 운영자가 신뢰 가능한 로컬 PC에서 1회 OAuth 동의를 마쳐 \`secrets/google-token.json\`을 생성한다.
- 토큰은 **서버 DB에 저장하는 것을 기본**으로 사용하고, 초기 파일 마운트(\`secrets/google-token.json\`, 읽기 전용)는 폰으로 유지한다. 사용자의 Menlo 브라우저 또는 로컬 클라이언트에는 제공하지 않는다.
- 관리자는 CalSync 웹의 **"Google 계정 재연결"** 버튼으로 토큰을 갱신한다. 이를 위해 ① OAuth 클라이언트에 \`<CALSYNC_PUBLIC_ORIGIN>/oauth/callback\`을 승인된 redirect URI로 추가하고, ② \`credentials.json\`을 컨테이너에 읽기 전용(\`/run/secrets/google-credentials.json\`)으로 마운트해야 한다. 재연결은 관리자 세션 + 일회용 state(10분)로 보호하며, 동의 완료 즉시 토큰은 브라우저가 아니라 서버 DB에 저장된다.
- Scope: \`https://www.googleapis.com/auth/calendar.events\` (캘린더 이벤트 읽기/쓰기). OAuth 사용자의 계정은 지정된 캘린더에 쓰기 권한이 있어야 한다.
- **수신 대상 캘린더는 서버의 \`CALSYNC_CALENDAR_ID\` 환경변수 한 개로 고정**한다. JSON의 \`calendarId\`는 무시한다.
- 서버가 필요 시 Access Token을 자동 갱신한다. Refresh Token 회수/만료 시 서버에서 재동의해야 한다.
- OAuth Testing 게시 상태에서는 Refresh Token 만료가 짧을 수 있으므로 Google Cloud 앱 게시 상태를 별도 확인한다.
- 초기 버전은 **사용자 OAuth 방식으로 확정**한다. Service Account/Workspace Domain-Wide Delegation은 현재 사용하지 않는다.

## 이벤트 수신 정책

| 기능 | 경로 | 구현 정책 |
| --- | --- | --- |
| 로그인 | GET/POST \`/login\` | 제출자/관리자 두 암호, HttpOnly Secure Cookie |
| 사용자의 화면 | GET \`/\` | Menlo로 접근 가능한 HTML/JS |
| 캘린더 조회 | GET \`/api/v1/events\` | Google Calendar에서 특정 calendarId만 조회 |
| 새 일정 일괄 접수 | POST \`/api/v1/requests/import\` | batchId + events(최대 100개), 즉시 실행 후 건별 상태 반환 |
| 삭제 요청 접수 | POST \`/api/v1/requests/delete\` | Google eventId와 etag 확인 후 즉시 실행 |
| 일괄 삭제 | POST \`/api/v1/requests/delete-batch\` | batchId와 deletions, 즉시 실행 및 재전송 중복 방지 |
| Sender 처리 결과 | GET \`/api/v1/receipts\` | 같은 계정의 최근 1,000건 상태와 성공한 Google event ID |
| 접수 내역 확인 | GET \`/api/v1/requests\` | 상태 확인 |
| 관리자 이전 대기·실패 건 실행 | POST \`/api/v1/admin/requests/{id}/approve\` | 기존 요청 반영 또는 재시도 |
| 관리자 거절 | POST \`/api/v1/admin/requests/{id}/reject\` | PENDING → REJECTED |
| Google 연결 상태 | GET \`/api/v1/admin/google/status\` | 토큰 저장 위치·access token 만료 시각 (관리자) |
| Google 재연결 | POST \`/api/v1/admin/google/reconnect\` | consent URL 발급, state 일회용·10분 유효 (관리자) |
| Google OAuth 콜백 | GET \`/oauth/callback\` | code 검증·교환 후 토큰을 서버 DB에 저장 |
| 거부 요청 기록 | GET \`/api/v1/admin/rejections\` | 422·409 거부 시 받은 원본 payload (관리자) |
| 개인정보처리방침 | GET \`/privacy\` | 공개 정적 페이지 (OAuth 동의 화면 링크용) |
| 서버 상태 | GET \`/healthz\` | 공개 상태 정보만 반환 |

예시 전송 페이로드(가상 데이터):

~~~json
{
  "batchId": "example20261111a1",
  "events": [
    {
      "subject": "샘플 일정",
      "startTime": {"dateTime": "2026-11-12T13:00:00+09:00", "timeZone": "Asia/Seoul"},
      "endTime": {"dateTime": "2026-11-12T14:00:00+09:00", "timeZone": "Asia/Seoul"},
      "chair": {"displayName": "예시 이름", "id": "example", "email": "sample@example.com"},
      "location": "예시 회의실"
    }
  ]
}
~~~

- 원본 JSON에서 사용되는 **다섯 내용 키**는 \`subject\`, \`startTime\`, \`endTime\`, \`chair\`, \`location\`이다.
- \`type\`, \`uid\`, \`calendarId\`, \`attendees\`, \`URL\`은 Google 이벤트 콘텐츠에 사용하지 않는다.
- \`subject\` → Google Calendar \`summary\`; \`location\` → \`location\`.
- \`startTime\`/\`endTime\` → \`start\`/\`end\` (\`dateTime\` 우선, 없으면 \`date\`; \`timeZone\` 검증). 종일 일정 \`end.date\`는 Google API 기준 **exclusive**. 실제 소스 형식 검증 후 조정.
- \`chair.displayName\` → Google \`description\`의 '주최: ...'로 표시. \`chair.email\`을 참석자 자동 초대/Google organizer 변경에 이용하지 않는다.
- 기타 수동 입력을 지원하는 '내용/참석자/메모' 필드는 **현재 JSON 자동 변환 범위 외**이며 추후 웹 폼 확장 가능.
- 서버는 요청을 DB에 저장한 후 같은 POST 처리 중 Google API에 반영한다. 각 항목의 처리 결과를 확인해야 한다.

## 중복·삭제 안전성

- 같은 batchId와 item 순서로 재전송하면 동일 접수 건을 돌려준다. batchId를 재사용하면서 내용이 달라지면 409 오류.
- Google Calendar 생성 전에 내부 eventId(\`cs\` + 랜덤 16바이트의 hex)를 저장해, 불확실한 네트워크 장애 후에도 같은 Google eventId로 재시도한다.
- 삭제 요청은 **CalSync 웹에서 선택한 Google eventId**를 대상으로 하며, JSON의 \`uid\`로 자동 삭제하지 않는다.
- 삭제 직전 Google Calendar의 \`etag\`를 비교하고 조건부 삭제를 사용한다. 변경된 이벤트는 **CONFLICT**로 처리한다.
- 파일 누락/빈 JSON/오류가 실제 삭제 요청을 자동 생성하지 않게 한다.
- 원본의 명시적 삭제 규칙과 다중 파일 구조는 실제 JSON 샘플을 받은 뒤 보내는 쪽에서 구현한다.
- 관리자 처리 중 시스템이 비정상 종료되면 \`PROCESSING\` 상태가 남을 수 있다. 현 MVP에서는 DB를 확인하고 운영자가 복구 처리해야 한다(후속 작업: 자동 재조정).

## 보안·배포 유의 사항

- 공용 도메인 연결 전 NPM에서 유효한 TLS 인증서, Force SSL 및 필요시 Access List/IP 정책을 적용한다.
- 각 사용자별 계정을 별도로 생성하고 비밀번호는 Argon2id로 해싱한다. 무작위 HttpOnly/Secure/SameSite 쿠키는 서버 DB에서 검증·폐기할 수 있으며, CSRF·Origin 검사와 CSP도 적용한다.
- NPM과 CalSync는 Docker 사설 네트워크로 연결하고 CalSync의 :8000 포트는 호스트에 공개하지 않는다.
- NPM의 웹 프록시가 \`Host\`/\`X-Forwarded-Proto\`를 올바르게 전달하는지, Menlo가 Secure Cookie/JS fetch/클립보드 입력을 허용하는지 확인한다.
- 보안 정책상 캘린더 정보가 외부 개인 서버로 전송될 수 있는지 별도 승인 필요. 전송 제한을 우회하는 설계는 금지.
- 권한은 viewer(읽기), submitter(읽기/즉시 변경), admin(즉시 변경/전체 이력 관리)으로 분리하며 제출자는 자신의 요청 이력만 조회한다. 사용자 이름별 인증 실패 횟수를 제한한다.
- 데이터 DB, OAuth 자격 증명, 비밀번호, 실 업무 일정을 공개 저장소에 커밋하지 않는다.

## 남은 과제

1. 실제 Google Cloud OAuth/Calendar ID 구성.
2. NPM 도메인 연결 및 Menlo Browser 접근 테스트.
3. Menlo 클립보드 복사/붙여넣기·브라우저 fetch 정책 테스트.
4. 실제 JSON 샘플을 받아 date/dateTime 우선순위, chair, 삭제 마커 규칙 검증.
5. 지속 운영에 필요한 사용자별 로그인·보안 로깅·복구 워커·테스트 강화.

관련: [개발 계획서](PROJECT_PLAN.md), [Google Calendar Events API](https://developers.google.com/workspace/calendar/api/v3/reference/events).


## 사용자 인증·암호화 정책 (2026-11-11 추가)

- **전송:** NPM의 유효한 TLS 인증서로 HTTPS 443만 외부 공개. NPM→FastAPI는 같은 호스트의 비공개 Docker 네트워크를 이용한다. Google Calendar 호출 역시 HTTPS.
- **로그인:** 사용자 이름 + 비밀번호. 비밀번호는 Argon2id 해시로 SQLite에 저장하며 기본 계정/초기 비밀번호는 없다.
- **세션:** 난수 생성 opaque token은 브라우저 HttpOnly/Secure/SameSite=Strict 쿠키로 전달하고 서버 DB에는 **토큰의 SHA-256 해시**만 저장한다. 서버는 만료, 로그아웃, 계정 비활성화를 즉시 검증할 수 있다. 브라우저용 JWT는 사용하지 않는다.
- **권한:** viewer는 조회만, submitter는 즉시 등록·삭제 및 자기 이력 확인, admin은 전체 이력과 이전 요청 관리. 모든 데이터 API는 세션 인증이 필요하다. \`/healthz\`는 일반 상태 문자열만 반환한다.
- **웹 보호:** 상태 변경 POST는 CSRF 토큰과 요청 Origin 검증; CSP, HSTS, frame-ancestors 'none', no-store 등의 헤더 추가. 로그인 실패는 사용자별 15분당 10회 제한.
- **데이터 보관:** TLS는 전송 중 암호화이고 DB/Google Calendar 저장 데이터의 E2EE를 뜻하지 않는다. 호스트 \`data/\`/OAuth 비밀 파일 접근을 제한하고 필요 시 디스크 암호화를 적용한다.
- **실운영 추가:** NPM 접근 제한/레이트 리미트, 사용자 수·활동 로그 감사, 재시도 대기 처리, MFA/SSO 필요 여부 검토.

계정 생성 및 변경 CLI, 설치 방법: [README](../README.md).
