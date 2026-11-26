# CalSync result_s.json -> CalSync 서버 전송 스크립트
# 사내 프록시(McAfee, NTLM SSO) + Menlo UA 정책 + 사내 TLS CA(Windows 인증서 저장소)를
# PowerShell/.NET 기능으로 통과한다. 별도 라이브러리 불필요.
#
# 사용 예:
#   powershell -ExecutionPolicy Bypass -File sender\send.ps1                     # 기본 경로 사용, 자격증명 입력 프롬프트
#   powershell -ExecutionPolicy Bypass -File sender\send.ps1 -Path C:\...\result_s.json
#   powershell -ExecutionPolicy Bypass -File sender\send.ps1 -Username sender1    # 비밀번호만 프롬프트
#
# 동작:
#   1. result_s.json 읽기 (이벤트 아닌 updateTime 등 항목은 자동 건너뜀)
#   2. 서버에 로그인 -> 세션 쿠키 + CSRF 토큰 획득
#   3. batchId = 내용 해시(안정적 재시도: 같은 내용이면 같은 batchId로 서버 중복 접수 방지)
#   4. POST /api/v1/requests/import 로 일괄 등록 요청 (관리자 승인 전까지 Google 반영 없음)
#   5. GET /api/v1/receipts 로 현재 상태 출력
param(
    [string]$Origin = "",
    [string]$Path = "",
    [string]$Username = "",
    [string]$Password = "",
    [switch]$ReceiptOnly,  # 전송 없이 영수증 상태만 조회
    [switch]$DryRun        # 로그인/전송 없이 보낼 내용만 확인
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# --- 설정 파일: %USERPROFILE%\YYS_butler\CalSync\send-config.json (비밀번호는 저장하지 않음) ---
$ConfigDir = Join-Path $env:USERPROFILE "YYS_butler\CalSync"
$ConfigFile = Join-Path $ConfigDir "send-config.json"
if (-not (Test-Path -LiteralPath $ConfigDir)) { New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null }
$saved = @{}
if (Test-Path -LiteralPath $ConfigFile) {
    try { (Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $saved[$_.Name] = $_.Value } } catch { }
}
if (-not $Origin) { $Origin = if ($saved["origin"]) { $saved["origin"] } elseif ($env:CALSYNC_ORIGIN) { $env:CALSYNC_ORIGIN } else { Read-Host "CalSync 서버 URL (예: https://calendar.example.com)" } }
if (-not $Path) { $Path = if ($saved["jsonPath"]) { $saved["jsonPath"] } else { Join-Path $env:USERPROFILE "YYS_butler\result_s.json" } }
if (-not $Username) { $Username = [string]$saved["username"] }
function Save-Config {
    @{ origin = $Origin; jsonPath = $Path; username = $Username } |
        ConvertTo-Json | Set-Content -LiteralPath $ConfigFile -Encoding UTF8
}

# --- 사내 프록시 (NTLM SSO). 시스템 프록시 설정에서 자동 감지, 실패 시 고정값. ---
$proxyUri = $null
try {
    $reg = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction Stop
    if ($reg.ProxyEnable -and $reg.ProxyServer) {
        $srv = $reg.ProxyServer
        if ($srv -notmatch "^https?://") { $srv = "http://$srv" }
        $proxyUri = $srv
    }
} catch { }
if (-not $proxyUri) { $proxyUri = $env:CALSYNC_PROXY }   # 프록시 고정 주소는 커밋하지 않는다

# Menlo 게이트웨이가 비브라우저 UA를 차단하므로 브라우저 UA 사용.
$UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

function New-Request {
    param([string]$Method, [string]$Uri, [string]$Body = $null, [hashtable]$Headers = @{})
    $req = [System.Net.HttpWebRequest]::Create($Uri)
    $req.Method = $Method
    $req.UserAgent = $UA
    $req.Proxy = New-Object System.Net.WebProxy($proxyUri)
    $req.Proxy.Credentials = [System.Net.CredentialCache]::DefaultCredentials   # NTLM SSO
    $req.CookieContainer = $script:cookies
    $req.AllowAutoRedirect = $true
    $req.Timeout = 60000
    foreach ($k in $Headers.Keys) { $req.Headers.Add($k, $Headers[$k]) }
    if ($Body) {
        $req.ContentType = "application/json; charset=utf-8"
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
        $req.ContentLength = $bytes.Length
        $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
    }
    return $req
}

function Invoke-Api {
    param([string]$Method, [string]$Uri, [string]$Body = $null, [hashtable]$Headers = @{})
    $req = New-Request -Method $Method -Uri $Uri -Body $Body -Headers $Headers
    try {
        $resp = $req.GetResponse()
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if (-not $resp) { throw }
    }
    $code = [int]$resp.StatusCode
    $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
    $text = $reader.ReadToEnd(); $reader.Close(); $resp.Close()
    return [pscustomobject]@{ Code = $code; Text = $text }
}

function Get-Sha256Hex([string]$text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { ($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($text)) | ForEach-Object { $_.ToString("x2") }) -join "" }
    finally { $sha.Dispose() }
}

# ---------- 1. result_s.json 읽고 유효 이벤트만 추출 ----------
$events = @()
if (-not $ReceiptOnly) {
    if (-not (Test-Path $Path)) { Write-Error "JSON 파일이 없습니다: $Path"; exit 1 }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($raw -is [System.Management.Automation.PSCustomObject] -and $raw.events) { $raw = $raw.events }
    if ($raw -isnot [System.Array]) { Write-Error "JSON 최상위가 배열/이벤트 객체가 아닙니다."; exit 1 }

    $all = @()
    foreach ($item in $raw) {
        # 이벤트 아닌 메타 항목(updateTime만 있는 등)은 건너뜀
        if (-not $item.subject -or -not $item.startTime -or -not $item.endTime) { continue }
        $all += $item
    }
    if ($all.Count -eq 0) { Write-Output "보낼 유효 이벤트가 없습니다."; exit 0 }
    Write-Output "[1] $($all.Count)개 이벤트 로드: $Path"

    # ---------- 2. uid별 전송 이력으로 중복 등록 방지 ----------
    # butler가 result_s.json을 주기적으로 갱신해도 같은 일정은 한 번만 등록한다.
    $sentFile = Join-Path $ConfigDir "sent.json"
    $sent = @{}
    if (Test-Path -LiteralPath $sentFile) {
        try { (Get-Content -LiteralPath $sentFile -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $sent[$_.Name] = $_.Value } } catch { }
    }
    $usedKeys = @("chair", "endTime", "location", "startTime", "subject")
    $pending = @()
    foreach ($item in $all) {
        $norm = [ordered]@{}
        foreach ($k in $usedKeys) { if ($null -ne $item.$k) { $norm[$k] = $item.$k } }
        $fp = Get-Sha256Hex (ConvertTo-Json $norm -Depth 10 -Compress)
        $uid = if ($item.uid) { [string]$item.uid } else { "fp:" + $fp }
        if ($sent[$uid] -eq $fp) { continue }
        $pending += [pscustomobject]@{ uid = $uid; fp = $fp; payload = $norm }
    }
    if ($pending.Count -eq 0) { Write-Output "[2] 새 이벤트 없음 - 이전 전송 이력과 동일합니다 (중복 방지)"; exit 0 }
    Write-Output "[2] 신규/변경 $($pending.Count)건 (전체 $($all.Count)건)"

    $events = @($pending | ForEach-Object { $_.payload })
    $payloadJson = ConvertTo-Json $events -Depth 10 -Compress
    $batchId = (Get-Sha256Hex $payloadJson).Substring(0, 32)
    Write-Output "[3] batchId = $batchId"

    if ($DryRun) {
        Write-Output "[DryRun] 로그인/전송 없이 종료합니다. 전송될 본문 일부:"
        Write-Output ($payloadJson.Substring(0, [Math]::Min(400, $payloadJson.Length)))
        exit 0
    }
}

# ---------- 4. 로그인 ----------
if (-not $Username) { $Username = Read-Host "CalSync 사용자 이름" }
if (-not $Password) {
    $sec = Read-Host "CalSync 비밀번호" -AsSecureString
    $Password = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}

# 프록시 NTLM 협상이 POST 도중 일어나면 요청 본문이 유실될 수 있으므로
# 같은 연결에서 먼저 GET으로 터널 인증을 끝내 둔다.
[System.Net.ServicePointManager]::Expect100Continue = $false
try { $null = Invoke-Api -Method "GET" -Uri "$Origin/healthz" } catch { }

$script:cookies = New-Object System.Net.CookieContainer
$loginBytes = [System.Text.Encoding]::UTF8.GetBytes("username=" + [Uri]::EscapeDataString($Username) + "&password=" + [Uri]::EscapeDataString($Password))
$req = New-Request -Method "POST" -Uri "$Origin/login"
$req.AllowAutoRedirect = $false
$req.ContentType = "application/x-www-form-urlencoded"
$req.Referer = "$Origin/login"
$req.ContentLength = $loginBytes.Length
$stream = $req.GetRequestStream(); $stream.Write($loginBytes, 0, $loginBytes.Length); $stream.Close()
try { $resp = $req.GetResponse() } catch [System.Net.WebException] {
    $code = [int]$_.Exception.Response.StatusCode
    $detail = ""
    try {
        $rd = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $detail = $rd.ReadToEnd(); $rd.Close()
    } catch { }
    if ($code -eq 401) {
        Write-Error "로그인 실패(401): $($detail.Trim())"
        Write-Output ("  진단: username='{0}' (길이 {1}), password 길이 {2}, 전송 본문 {3}바이트" -f $Username, $Username.Length, $Password.Length, $loginBytes.Length)
        Write-Output "  - 본문이 0바이트면 프록시 문제, 정상 길이면 계정/비밀번호 문제입니다."
        Write-Output "  - 서버에서 계정 확인: docker compose exec calsync python -m app.security list-users"
        exit 1
    }
    if ($code -eq 429) { Write-Error "로그인 시도 횟수 초과. 15분 후 재시도하세요."; exit 1 }
    throw
}
$status = [int]$resp.StatusCode
$respHeaders = @{}
foreach ($k in $resp.Headers.AllKeys) { $respHeaders[$k] = [string]($resp.Headers[$k] -join "; ") }
$bodyText = ""
try {
    $rd = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
    $bodyText = $rd.ReadToEnd(); $rd.Close()
} catch { }
$resp.Close()
$setCookie = if ($respHeaders.ContainsKey("Set-Cookie")) { $respHeaders["Set-Cookie"] } else { "" }
Write-Output "[4] 로그인 응답: HTTP $status, Set-Cookie: $(if ($setCookie) { '있음' } else { '없음' })"
if ($bodyText -match 'sv_role\s*=\s*"main"') {
    Write-Output "Menlo 격리 페이지가 반환되었습니다: 이 PC에서 직접 POST는 차단됩니다."
    Write-Output "Windows 클라이언트(exe)의 '서버로 직접 전송'(Edge 자동화)을 사용하세요."
    exit 3
}
if ($status -ne 303 -and $status -ne 302) {
    $snippet = $bodyText.Substring(0, [Math]::Min(300, $bodyText.Length))
    Write-Output "  예상과 다른 응답입니다. 본문(앞 300자): $snippet"
    foreach ($k in @("Content-Type", "Server", "Via", "X-Menlo-Disposition", "X-Menlo-Trace", "WWW-Authenticate", "Location")) {
        if ($respHeaders.ContainsKey($k)) { Write-Output "  ${k}: $($respHeaders[$k])" }
    }
}

# __Host- 접두 쿠키는 .NET CookieContainer가 자동 저장하지 않을 수 있으므로 직접 등록한다.
if ($setCookie -match "__Host-calsync_session=([^;]+)") {
    $cookie = New-Object System.Net.Cookie("__Host-calsync_session", $matches[1], "/", ([Uri]$Origin).Host)
    $cookie.Secure = $true
    $script:cookies.Add($cookie)
}
$cookieNames = @($script:cookies.GetCookies([Uri]$Origin) | ForEach-Object { $_.Name })
Write-Output "[4] 로그인 성공: $Username (보유 쿠키: $($cookieNames -join ', '))"
if ($cookieNames.Count -eq 0) {
    Write-Error "세션 쿠키를 받지 못했습니다. 위 응답 본문/헤더를 확인하세요."
    exit 1
}

# ---------- 5. CSRF 토큰 ----------
$r = Invoke-Api -Method "GET" -Uri "$Origin/api/v1/session"
if ($r.Code -ne 200) {
    $nowCookies = @($script:cookies.GetCookies([Uri]$Origin) | ForEach-Object { "$($_.Name) domain=$($_.Domain) path=$($_.Path) secure=$($_.Secure)" })
    Write-Error "세션 조회 실패($($r.Code)): $($r.Text)"
    Write-Output ("  보유 쿠키 {0}개: {1}" -f $nowCookies.Count, ($nowCookies -join ' | '))
    exit 1
}
$csrf = ($r.Text | ConvertFrom-Json).csrfToken
Save-Config
Write-Output "[5] CSRF 토큰 획득"

# ---------- 6. 일괄 등록 요청 ----------
if (-not $ReceiptOnly) {
    $body = ConvertTo-Json @{ batchId = $batchId; events = $events } -Depth 10 -Compress
    $r = Invoke-Api -Method "POST" -Uri "$Origin/api/v1/requests/import" -Body $body -Headers @{ "X-CSRF-Token" = $csrf }
    if ($r.Code -eq 200) {
        $res = $r.Text | ConvertFrom-Json
        $pendingCount = @($res.requests | Where-Object { $_.status -eq "PENDING" }).Count
        Write-Output "[6] 전송 성공: 요청 $($res.requests.Count)건 (PENDING $pendingCount건, 관리자 승인 대기)"
        foreach ($item in $pending) { $sent[$item.uid] = $item.fp }
        $sent | ConvertTo-Json | Set-Content -LiteralPath $sentFile -Encoding UTF8
    }
    elseif ($r.Code -eq 409) { Write-Output "[6] 이미 제출된 배치입니다 (같은 batchId+내용): $($r.Text)" }
    elseif ($r.Code -eq 422) { Write-Error "[6] 유효성 오류(422): $($r.Text)"; exit 1 }
    elseif ($r.Code -eq 403) { Write-Error "[6] 권한 없음(403): submitter 이상 계정이 필요합니다."; exit 1 }
    else { Write-Error "[6] 전송 실패($($r.Code)): $($r.Text)"; exit 1 }
}

# ---------- 7. 영수증 상태 ----------
$r = Invoke-Api -Method "GET" -Uri "$Origin/api/v1/receipts"
if ($r.Code -eq 200) {
    $rec = $r.Text | ConvertFrom-Json
    Write-Output "[7] 현재 요청 상태 (최근):"
    $rec.requests | Select-Object -First 20 | ForEach-Object {
        $gid = if ($_.googleEventId) { $_.googleEventId } else { "" }
        Write-Output ("    batch={0} #{1} {2} -> {3} {4}" -f $_.batchId.Substring(0, [Math]::Min(8, $_.batchId.Length)), $_.batchIndex, $_.operation, $_.status, $gid)
    }
    if ($rec.requests.Count -gt 20) { Write-Output "    ... 총 $($rec.requests.Count)건" }
} else {
    Write-Output "[7] 영수증 조회 실패($($r.Code))"
}
