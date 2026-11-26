# CalSync Windows Desktop Client

이 프로그램은 **Windows 10/11, x64**용 CalSync JSON 감시·전송 클라이언트입니다.

> **전송 방식 (2026-11-12 실측):** 이 PC에서 나가는 `POST`는 Menlo가 `200 text/html` 격리 셸로 대체하므로 **직접 HTTP POST는 서버에 도달하지 않습니다**(`GET`은 그대로 통과). 그래서 클라이언트는 **시스템 Edge를 Playwright로 조작해 Menlo 셸에 렌더링된 CalSync 화면에 입력·제출**합니다. 폼 제출은 Menlo 클라우드 브라우저에서 발생하므로 서버에 도달하며, 서버가 받는 `Origin`은 `null`입니다. 따라서 서버 `.env`에 `CALSYNC_ALLOWED_ORIGINS=null`이 필요합니다. 수동 복사/붙여넣기 워크플로는 대체 경로로 유지됩니다.

## 직접 전송 사용법

1. **설정 → CalSync HTTPS URL**에 서버 주소(예: `https://calendar.example.com`)를 입력합니다.
2. **CalSync 계정**에 서버 관리자가 만든 submitter 이상 계정의 사용자 이름을 입력합니다.
3. **서버로 직접 전송**을 처음 실행하면 비밀번호를 한 번 입력받아 **Windows 자격 증명 관리자**(`CalSync\<계정>`)에 저장하고, 이후에는 저장된 값을 사용합니다. 전송은 화면의 `#importText`에 배치 JSON을 넣고 `#sendBatch`를 눌러 Menlo 경유로 제출합니다.
4. **자동 전송** 체크박스를 켜면 감시 중 새 항목이 생길 때 자동으로 전송합니다. 전송할 때마다 Edge 창이 잠시 열립니다.
5. **서버 영수증 자동 조회**로 `/api/v1/receipts` 결과를 대기열에 반영합니다. 배포 서버는 등록·삭제 요청을 **승인 없이 즉시 실행**하며 결과를 `SUCCEEDED`/`FAILED`/`CONFLICT`로 기록합니다. `SUCCEEDED` + `googleEventId`가 확인된 건만 원본 식별자와 매핑됩니다.

전송 계층 동작 방식:

- Menlo 셸은 화면을 클라우드 브라우저에서 렌더링하므로, 로그인 폼과 대시보드는 **일반 DOM처럼 조작**할 수 있습니다(`get_by_role`, `keyboard.insert_text`). 단, 입력 전에 해당 필드를 **클릭해 포커스를 줘야** 합니다.
- Menlo 셸은 응답 렌더링이 간헐적으로 실패(`tc-cof-client-inspect-host-unreachable`)해 빈 화면이 됩니다. 로그인 판정은 화면 문구가 아니라 **`/` 재방문 후 대시보드 표시 여부**로 확인하고, 화면 로드 실패 시 캐시 우회 URL로 재시도합니다.
- 별도 브라우저를 내려받지 않고 **설치된 Microsoft Edge**(`channel="msedge"`)를 사용합니다.
- 비밀번호는 Windows Credential Locker에만 저장하며 설정 파일·로그·명령줄에 남기지 않습니다. CLI로 관리할 수 있습니다: `python -m credentials set|status|delete --username <계정>`.
- 사내 TLS CA 인증서가 Windows 인증서 저장소에 신뢰 등록되어 있어야 합니다(SSL 가로채기 대응).
- PowerShell/.NET 기반 `transport.ps1`·`transport.py`는 직접 POST가 허용되는 환경을 위한 보존 코드이며 현재 정책에서는 사용하지 않습니다.

## 다운로드 / 실행

- GitHub [Actions → Windows client build](https://github.com/dark7sky/CalSync/actions/workflows/windows-client.yml) → 가장 최근 성공한 실행 → **Artifacts: CalSync-Windows-x64**에서 zip을 받습니다.
- 압축 파일 내 \`CalSync.exe\`를 실행합니다. 첫 실행 시 Windows SmartScreen에서 서명되지 않은 프로그램 경고가 나타날 수 있습니다. 공개 저장소의 소스/워크플로를 검토하거나 직접 빌드하여 사용하세요.
- Python 설치 없이 실행되도록 PyInstaller \`--onefile --windowed\`로 패키징합니다.
- EXE는 코드 서명하지 않았으며 회사 PC의 소프트웨어 설치/실행 정책을 따라야 합니다.

직접 빌드 시 Windows PowerShell에서 (Python 3.12 권장):

~~~powershell
powershell -ExecutionPolicy Bypass -File sender\windows\build.ps1
# 결과: dist\CalSync.exe
~~~

## GUI 사용 순서 (Menlo 브라우저 수동 워크플로)

> '서버로 직접 전송'을 사용하면 아래 5~10단계의 수동 복사/붙여넣기 없이 바로 전송·확인할 수 있습니다. 아래 절차는 직접 전송 경로를 쓸 수 없는 환경을 위한 대체 수동 워크플로입니다.

1. **JSON 폴더**를 선택합니다. 해당 폴더 바로 아래의 `*.json` 파일을 감시합니다.
2. **CalSync HTTPS URL**에 Nginx Proxy Manager 도메인(예: \`https://calendar.example.com\`)을 지정합니다. 외부 전송은 하지 않고 브라우저 열기에만 사용합니다.
3. **명시적 삭제 type 값**을 입력합니다. 원본 \`type\` 필드의 *실제 삭제 상태 값*을 확인한 뒤, 여러 개면 쉼표로 구분합니다. 예: \`deleted,cancelled\`. 값을 비워 두면 **자동 삭제 요청을 전혀 만들지 않습니다.**
4. **로컬 자동 감시 시작**. QFileSystemWatcher로 변경을 감지하고 주기적으로 전체 JSON 파일을 다시 검사합니다. 오프라인 상태에서도 동작합니다.
5. 새 이벤트가 감지되면 대기열에 생성 요청이 쌓입니다. **전송 데이터 준비 · 복사**를 클릭하면 최대 100건의 생성 또는 삭제 요청을 담은 JSON을 클립보드에 복사합니다.
6. **Menlo 웹페이지 열기**를 누릅니다. 이 버튼은 *Windows 기본 브라우저*를 열기 때문에 **실제로 Menlo Browser로 접속되었는지는 직접 확인**해야 합니다. 필요하면 허용된 Menlo Browser를 직접 열고 CalSync URL로 접속하세요.
7. 웹페이지에 로그인해 **JSON 일괄 생성 / 삭제 요청** 입력창에 붙여넣고 제출합니다. 제출된 요청은 관리자 승인 단계 없이 서버가 즉시 Google Calendar에 반영합니다.
8. Windows 앱의 **웹 제출 버튼을 눌렀음 (확인 전)**은 수동 제출을 위한 예비 경로입니다. 실제 제출 여부를 확인하는 대화상자가 뜨며, 이 상태는 서버 성공을 뜻하지 않습니다. 가능하면 **서버로 직접 전송**을 사용하세요.
9. 웹페이지의 **요청 상태 JSON 생성** 버튼으로 결과를 복사하거나, Windows 앱에서 **서버 영수증 자동 조회**를 실행합니다.
10. Windows 앱의 **서버 영수증/상태 가져오기**에 붙여넣습니다. `SUCCEEDED`와 `googleEventId`가 있는 생성 건만 성공한 원격 이벤트로 확인합니다.

- 배치 ID는 재시도를 위해 고정됩니다. 같은 배치에 다른 데이터로 재사용하지 않습니다.
- 한 번에 **신규 요청 또는 삭제 요청 중 한 종류만** 생성합니다. 대기열에 두 종류가 있으면 각각 준비하여 제출합니다.
- 같은 일정을 재전송해도 서버는 배치 ID + 항목 순서 기준으로 중복 접수를 방지합니다.
- 서버에서 Google 이벤트 생성 시 미리 정한 ID를 사용해 Google API 재시도에 따른 중복도 제한합니다.

## 원본 JSON 규칙

기본 지원 구조: 이벤트 한 건인 JSON 객체, 이벤트 배열, 또는 \`{"events":[...]}\`.

| 원본 필드 | 사용 목적 |
| --- | --- |
| \`subject\` | Google \`summary\` |
| \`startTime\`, \`endTime\` | 각각 \`date\`, \`dateTime\`, \`timeZone\` 객체 |
| \`chair\` | Google description에 표시할 주최자 이름 |
| \`location\` | 장소 |
| \`uid\` | **클라이언트 로컬 식별용**. Google 이벤트에 콘텐츠로 전달하지 않음 |
| \`type\` | 사용자가 입력한 명시적 삭제 상태와 일치하는지 판단 |
| \`calendarId\`, \`attendees\`, \`URL\` | Google 이벤트 콘텐츠에 전달하지 않음 |

정상 신규 이벤트의 예(가상 자료):

~~~json
[
  {
    "type": "new",
    "uid": "demo-123",
    "subject": "일정 예제",
    "startTime": {"dateTime": "2026-12-01T09:00:00+09:00", "timeZone": "Asia/Seoul"},
    "endTime": {"dateTime": "2026-12-01T10:00:00+09:00", "timeZone": "Asia/Seoul"},
    "chair": {"displayName": "Sample Organizer"},
    "location": "Demo Room"
  }
]
~~~

명시적인 삭제 마커 예:

~~~json
[{"type":"deleted","uid":"demo-123"}]
~~~

**주의:** 삭제는 이전에 같은 파일 이름 + \`uid\`를 가진 이벤트의 Google 측 성공 영수증을 가져왔을 때만 준비합니다. 실제 \`type\` 값은 아직 샘플 미제공이므로 위의 \`deleted\`는 예시입니다. 파일 이름 변경은 원본 식별자를 바꿀 수 있습니다.

## 삭제 및 수정 안전 규칙

- 원본 파일이 없어지거나, JSON이 비어 있거나, 파싱에 실패해도 원격 Google 일정을 삭제하지 않습니다.
- \`type\` 삭제 규칙을 명시적으로 지정해야만 삭제 요청을 생성합니다.
- Google event ID가 확인되지 않은 삭제 이벤트는 경고만 표시하고 서버로 전송하지 않습니다.
- 이미 제출된 요청의 처리 상태는 서버 영수증으로 확인합니다.
- 기존 이벤트의 내용 변경은 **자동 반영**됩니다. 서버는 원본 수정(in-place edit)을 지원하지 않으므로, Google event ID가 확인된 일정은 **기존 일정 삭제 요청 + 변경된 내용의 신규 등록 요청**으로 처리합니다. 서버 응답 대기 중이던 미확정 건은 대기열 항목의 내용을 그대로 교체합니다.
- 서버가 검증에서 배치를 거부(422/409)하면 그 배치는 `REJECTED`로 종료하고 재전송을 반복하지 않습니다. 원본을 수정하면 다음 스캔에서 다시 등록 대상이 됩니다.
- \`uid\`가 없으면 콘텐츠 해시로 임시 식별하므로 내용 수정 시 다른 신규 이벤트로 오인될 수 있습니다. 안정적인 동기화를 위해 원본에 \`uid\`가 있는 것이 권장됩니다.

## 시스템 트레이

- **JSON 감시가 실행 중일 때 X로 창을 닫으면** \`트레이로\`, \`완전히 종료\`, \`취소\`를 묻는 대화상자가 표시됩니다.
- \`트레이로\`: 창은 숨고 감시/대기열 작업은 계속됩니다. 시스템 트레이 CalSync 아이콘으로 접근합니다.
- 트레이 메뉴: \`지금동기화\`, \`로그보기\`, \`창복원\`, \`종료\`. 아이콘 더블클릭/좌클릭으로도 창을 복원합니다.
- 로그는 설정 폴더에 월별 파일(\`202611.log\`)로 기록됩니다.
- 감시가 정지된 상태에서 X를 누르면 일반 종료합니다. 시스템 트레이를 지원하지 않는 환경에서는 일반 종료합니다.
- 실행 종료 및 재시작 후에도 영속 DB의 대기열과 전송 상태가 유지됩니다. 감시 활성 설정도 복원됩니다.

## 로컬 데이터 위치 및 보안

- 설정/상태 폴더: `%USERPROFILE%\YYS_butler\CalSync\` (첫 실행 시 자동 생성)
  - `settings.ini`: JSON 폴더, CalSync URL, 계정명, 삭제 type 값, 자동 전송/감시 체크 상태, 재스캔 주기
  - `client.db`: 감시·전송 대기열 상태 DB
  - `send-config.json`: `sender\send.ps1` CLI가 마지막으로 사용한 URL/JSON 경로/계정명
- 비밀번호는 **Windows 자격 증명 관리자**(`CalSync\<계정>`)에만 저장합니다. 설정 파일·로그·명령줄에는 남기지 않으며, 앱은 저장된 값을 읽어 쓰고 CLI(`python -m credentials`)로 관리합니다.
- 앱 자체는 Google OAuth 토큰, Google 비밀번호를 저장하지 않습니다.
- 클립보드에 복사한 JSON에는 **실제 일정 데이터**가 있으므로 공유 PC에서는 주의하고 업무 보안 정책에 따라 클립보드를 정리하세요.
- GitHub/이슈에 실제 일정 데이터를 첨부하지 마세요.

## 아직 확인이 필요한 외부 조건

- **영수증 자동 조회는 서버의 `/api/v1/receipts` 지원에 의존**합니다. 현재 배포 서버는 최신 코드로 등록·삭제를 즉시 실행하고 영수증도 반환합니다. 영수증 조회가 실패하면 요청 자체는 처리됐을 수 있으므로 웹의 **요청 상태**에서 확인하세요.
- Menlo 정책상 **자동화된 브라우저 조작과 비서명 EXE 실행이 허용되는지**(조직 승인 필요).
- 실제 JSON 파일 구조, `type` 삭제 상태 값, 종일 일정의 종료일 의미.
- 외부 도메인·Google Calendar에 업무 정보를 보내는 행위에 대한 조직 정책.

**전송 경로 요약:** PC → Menlo 격리 셸(Edge 자동 조작) → Menlo 클라우드 브라우저 → `https://<도메인>` (Origin: null) → **즉시 실행** → Google Calendar. 서버는 `CALSYNC_ALLOWED_ORIGINS=null`이 설정되어 있어야 하며, 이 값이 없으면 로그인·import 요청이 `403 Unexpected Origin`으로 거부됩니다.
