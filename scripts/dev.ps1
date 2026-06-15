#Requires -Version 7
# Start local development environment via Docker Compose
# Usage: .\scripts\dev.ps1 [up|down|build|logs]

param([string]$Command = "up")

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

switch ($Command) {
    "up"    { docker compose up --build }
    "down"  { docker compose down }
    "build" { docker compose build }
    "logs"  { docker compose logs -f }
    default { Write-Host "Usage: dev.ps1 [up|down|build|logs]" }
}
