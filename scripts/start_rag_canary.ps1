param([int]$Port = 8001)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workDir = Join-Path $projectRoot '.rag-upgrade-work'
$pidPath = Join-Path $workDir 'canary-server.pid'
if (Test-Path -LiteralPath $pidPath) {
    $existingPid = [int](Get-Content -LiteralPath $pidPath)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$existingPid" -ErrorAction SilentlyContinue
    if ($existing -and $existing.CommandLine.Contains($projectRoot) -and
        $existing.CommandLine -match 'uvicorn\s+app\.main:app' -and
        $existing.CommandLine -match "--port\s+$Port(\s|$)") {
        Write-Output "Canary already running, PID: $existingPid; URL: http://127.0.0.1:$Port/login"
        return
    }
}
$credentialsPath = Join-Path $workDir 'canary-admin.json'
$credentials = Get-Content -Raw -LiteralPath $credentialsPath | ConvertFrom-Json

# Only this child process receives the v2 routing and local HTTP cookie settings.
$env:PYTHONUTF8 = '1'
$env:KB_READ_MODEL = 'v2'
$env:KB_V2_DUAL_WRITE = 'true'
$env:RAG_EVAL_OUTPUT_DIR = Join-Path $workDir 'eval-v2-controlled'
$env:AUTH_COOKIE_SECURE = 'false'
$env:AUTH_ALLOWED_ORIGINS = '["http://127.0.0.1:' + $Port + '"]'
$env:LANGFUSE_PUBLIC_KEY = ''
$env:CHECKPOINTER_DB_PATH = Join-Path $workDir 'canary-checkpoints.sqlite'
if ($credentials.canary_auth_signing_key) {
    $env:AUTH_SIGNING_KEY = $credentials.canary_auth_signing_key
}

$server = Start-Process -FilePath (Join-Path $projectRoot '.venv-win\Scripts\python.exe') `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port") `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $workDir 'canary-server.stdout.log') `
    -RedirectStandardError (Join-Path $workDir 'canary-server.stderr.log')
$server.Id | Set-Content -LiteralPath $pidPath
Write-Output "Canary PID: $($server.Id); URL: http://127.0.0.1:$Port/login"
Write-Output "Credentials remain local: $credentialsPath"
