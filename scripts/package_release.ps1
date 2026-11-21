# Package source for another computer.
# Excludes .venv, .env, database, uploads, logs, and other private data.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root "dist"
$Stage = Join-Path $Dist "CareerZoneX-src"
$Zip = Join-Path $Dist "CareerZoneX-src.zip"

if (-not (Test-Path $Dist)) { New-Item -ItemType Directory -Path $Dist | Out-Null }
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Path $Stage | Out-Null

$Items = @(
    "backend", "frontend", "scripts", "extension", "tests", "docs", ".streamlit",
    "pyproject.toml", "requirements.txt", "requirements-dev.txt", "requirements-rag.txt",
    "start.ps1", "start.bat", "README.md", ".env.example", ".gitignore"
)
foreach ($item in $Items) {
    $src = Join-Path $Root $item
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $Stage $item) -Recurse -Force
    }
}

foreach ($sub in @("storage\uploads", "storage\logs", "storage\exports", "storage\private", "storage\chroma")) {
    New-Item -ItemType Directory -Path (Join-Path $Stage $sub) -Force | Out-Null
}
New-Item -ItemType File -Path (Join-Path $Stage "storage\.gitkeep") -Force | Out-Null

if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Zip -CompressionLevel Optimal
Write-Host "Created:" -ForegroundColor Green
Write-Host $Zip
Write-Host "NOT included: .env, storage/careerzone.db, uploads. See docs/from-zero deployment guide for data migration."
