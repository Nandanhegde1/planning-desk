# PowerShell equivalent of run.sh, for Windows without Git Bash.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) {
    Write-Host "Creating virtualenv and installing dependencies. This takes a minute."
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from the template. Add your Foundry key, then run this again."
    exit 1
}

Write-Host "Checking external services before starting."
.\.venv\Scripts\python.exe scripts\check_apis.py

.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
