# ============================================================
# CareerZone X 一键启动（Windows PowerShell）
# 为什么分两个进程：FastAPI 提供 JSON API，Streamlit 只做 UI；
# 分开后前端崩了不影响数据接口，也便于单独排障。
# 幂等：已在运行时不重复启动；登录自启动也调用这个脚本。
# ============================================================
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "未找到 .venv，请先执行：python -m venv .venv; pip install -r requirements.txt"
}

# 已经启动过就直接退出；避免登录自启动和手动双击各起一份。
$UiReady = $false
try {
    $UiResp = Invoke-WebRequest -Uri "http://127.0.0.1:8501" -UseBasicParsing -TimeoutSec 3
    if ($UiResp.StatusCode -ge 200 -and $UiResp.StatusCode -lt 500) { $UiReady = $true }
} catch { }
if ($UiReady) {
    Write-Host "CareerZone X 已在运行：http://127.0.0.1:8501" -ForegroundColor Green
    exit 0
}

$ApiReady = $false
try {
    $ApiResp = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 3
    if ($ApiResp.status -eq "ok") { $ApiReady = $true }
} catch { }

# 每次启动先做幂等建表/管理员种子；已存在就跳过，不会覆盖密码。
& $Python -m scripts.init_db

if (-not $ApiReady) {
    $ApiLog = Join-Path $Root "storage\logs\api.out.log"
    $ApiErr = Join-Path $Root "storage\logs\api.err.log"
    Start-Process -FilePath $Python -ArgumentList "-m","scripts.run_api" -WorkingDirectory $Root -RedirectStandardOutput $ApiLog -RedirectStandardError $ApiErr -WindowStyle Hidden
}

$Ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        if ($Health.status -eq "ok") { $Ready = $true; break }
    } catch { }
}
if (-not $Ready) {
    Write-Host "API 未就绪，查看 storage\logs\api.err.log" -ForegroundColor Red
    exit 1
}

Write-Host "API 已就绪：http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "用户端启动中：http://127.0.0.1:8501" -ForegroundColor Green
& $Python -m streamlit run frontend/app.py --server.address 127.0.0.1 --server.port 8501
