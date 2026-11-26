# CalSync Windows transport: JSON-in / JSON-out HTTPS transport for the GUI client.
# Passes the corporate McAfee proxy with NTLM SSO (DefaultCredentials) and a
# browser User-Agent (Menlo gateway blocks non-browser agents). Requires the
# corporate TLS CA in the Windows certificate store (SSL interception).
#
# Machine interface, invoked by transport.py:
#   powershell -File transport.ps1 -Origin https://... -Mode health
#   powershell -File transport.ps1 -Origin https://... -Mode import  -Username u -BatchFile b.json
#   powershell -File transport.ps1 -Origin https://... -Mode receipts -Username u
# Password arrives via env CALSYNC_SEND_PASSWORD (never the command line).
# Prints exactly one JSON line: {"ok":true/false,"stage":...,"http":...,"data":...,"error":...}
param(
    [Parameter(Mandatory = $true)][string]$Origin,
    [Parameter(Mandatory = $true)][ValidateSet("health", "import", "receipts")][string]$Mode,
    [string]$BatchFile = "",
    [string]$Username = ""
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Origin = $Origin.TrimEnd("/")

$proxyUri = $null
try {
    $reg = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction SilentlyContinue
    if ($reg -and $reg.ProxyEnable -and $reg.ProxyServer) {
        $srv = [string]$reg.ProxyServer
        if ($srv -notmatch "^https?://") { $srv = "http://$srv" }
        $proxyUri = $srv
    }
} catch { }
if (-not $proxyUri) { $proxyUri = $env:CALSYNC_PROXY }   # 프록시 고정 주소는 커밋하지 않는다

$UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

function Emit([bool]$ok, [string]$stage, [int]$http, $data, [string]$error) {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $obj = @{ ok = $ok; stage = $stage; http = $http }
    if ($null -ne $data) { $obj.data = $data }
    if ($error) { $obj.error = $error }
    Write-Output (ConvertTo-Json $obj -Depth 12 -Compress)
    exit ([int](-not $ok))
}

function Read-Body($resp) {
    try {
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $text = $reader.ReadToEnd()
        $reader.Close()
        return $text
    } catch { return "" }
}

function Test-MenloShell([string]$Text) {
    return [bool]($Text -match 'sv_role\s*=\s*"main"')
}

$script:MenloMessage = "Menlo isolation page returned: direct POST is blocked on this network. Use the Windows client (Edge automation)."

function Invoke-CalSync {
    param([string]$Uri, [string]$Method = "Get", $Body = $null, [string]$ContentType = $null, [hashtable]$Headers = $null, $Session = $null)
    $spl = @{ Uri = $Uri; Method = $Method; Proxy = $proxyUri; ProxyUseDefaultCredentials = $true;
              UserAgent = $UA; UseBasicParsing = $true; TimeoutSec = 90 }
    if ($Body -ne $null) { $spl.Body = $Body }
    if ($ContentType) { $spl.ContentType = $ContentType }
    if ($Headers) { $spl.Headers = $Headers }
    if ($Session) { $spl.WebSession = $Session }
    Invoke-WebRequest @spl
}

try {
    $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

    if ($Mode -eq "health") {
        $r = Invoke-CalSync -Uri "$Origin/healthz" -Session $session
        Emit $true "health" ([int]$r.StatusCode) ($r.Content | ConvertFrom-Json) ""
    }

    $password = $env:CALSYNC_SEND_PASSWORD
    if (-not $Username -or -not $password) {
        Emit $false "credentials" 0 $null "username/password missing"
    }

    $form = "username=" + [Uri]::EscapeDataString($Username) + "&password=" + [Uri]::EscapeDataString($password)
    $login = Invoke-CalSync -Uri "$Origin/login" -Method Post -Body $form `
        -ContentType "application/x-www-form-urlencoded; charset=utf-8" -Session $session
    if (Test-MenloShell $login.Content) { Emit $false "menlo" 200 $null $script:MenloMessage }

    # __Host- 접두 쿠키는 자동 저장되지 않을 수 있으므로 Set-Cookie를 직접 등록한다.
    $setCookie = [string]($login.Headers["Set-Cookie"] -join "; ")
    if ($setCookie -match "__Host-calsync_session=([^;]+)") {
        $cookie = New-Object System.Net.Cookie("__Host-calsync_session", $matches[1], "/", ([Uri]$Origin).Host)
        $cookie.Secure = $true
        $session.Cookies.Add($cookie)
    }
    if ($session.Cookies.GetCookies([Uri]$Origin).Count -eq 0) {
        Emit $false "session" 0 $null "login response carried no session cookie"
    }

    $r = Invoke-CalSync -Uri "$Origin/api/v1/session" -Session $session
    $csrf = ($r.Content | ConvertFrom-Json).csrfToken
    if (-not $csrf) { Emit $false "session" ([int]$r.StatusCode) $null "csrf token missing" }

    if ($Mode -eq "receipts") {
        $r = Invoke-CalSync -Uri "$Origin/api/v1/receipts" -Session $session
        Emit $true "receipts" ([int]$r.StatusCode) ($r.Content | ConvertFrom-Json) ""
    }

    if (-not (Test-Path -LiteralPath $BatchFile)) { Emit $false "params" 0 $null "BatchFile missing" }
    $raw = Get-Content -LiteralPath $BatchFile -Raw -Encoding UTF8
    $doc = $raw | ConvertFrom-Json
    if ($doc.events) { $endpoint = "/api/v1/requests/import" }
    elseif ($doc.deletions) { $endpoint = "/api/v1/requests/delete-batch" }
    else { Emit $false "params" 0 $null "batch must contain events or deletions" }
    $r = Invoke-CalSync -Uri "$Origin$endpoint" -Method Post -Body $raw `
        -ContentType "application/json; charset=utf-8" `
        -Headers @{ "X-CSRF-Token" = $csrf } -Session $session
    if (Test-MenloShell $r.Content) { Emit $false "menlo" 200 $null $script:MenloMessage }
    Emit $true "import" ([int]$r.StatusCode) ($r.Content | ConvertFrom-Json) ""
} catch [System.Net.WebException] {
    $resp = $_.Exception.Response
    $detail = $_.ErrorDetails.Message
    if (-not $detail -and $resp) { $detail = Read-Body $resp }
    if ($resp) { Emit $false "http" ([int]$resp.StatusCode) $null $detail }
    Emit $false "network" 0 $null $_.Exception.Message
} catch {
    Emit $false "unknown" 0 $null $_.Exception.Message
}
