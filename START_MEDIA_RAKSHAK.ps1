$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Add your credentials before expecting cloud connectivity." -ForegroundColor Yellow
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -m venv .venv
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed. Check your network and Python installation." }
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check your network, then retry." }
& $python -m uvicorn app.main:app --reload --port 8000
