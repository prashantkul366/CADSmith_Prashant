<#
.SYNOPSIS
  Start the CADSmith web application on Windows.

.EXAMPLE
  .\app\run_app.ps1
  .\app\run_app.ps1 -Port 9000
  .\app\run_app.ps1 -Reload      # reload on source changes, for development

  Expects a virtualenv at .venv in the repository root; see app/README.md.
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$AppHost = "127.0.0.1",
    [string]$Python = "",
    [switch]$Reload
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $root

# Accept either name: ".venv" is this project's convention, "venv" is the
# other common one. -Python overrides both.
$python = ""
foreach ($candidate in @($Python,
                         (Join-Path $root ".venv\Scripts\python.exe"),
                         (Join-Path $root "venv\Scripts\python.exe"))) {
    if ($candidate -and (Test-Path $candidate)) { $python = $candidate; break }
}
if (-not $python) {
    Write-Host "No virtualenv found in $root (looked for .venv and venv)" -ForegroundColor Red
    Write-Host "Create one with:"
    Write-Host "  python -m venv .venv"
    Write-Host "  .venv\Scripts\python -m pip install -r app\requirements-app.txt"
    Write-Host "Or point at an existing one:  .\app\run_app.ps1 -Python C:\path\to\python.exe"
    exit 1
}
Write-Host "Using $python" -ForegroundColor DarkGray

& $python -c "import cadquery" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "CadQuery is not installed in $python" -ForegroundColor Red
    Write-Host "  .venv\Scripts\python -m pip install -r app\requirements-app.txt"
    exit 1
}

# Load .env into this process. The agents read it through python-dotenv too,
# but setting it here means the health check reports the truth before the
# first run starts.
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $split = $trimmed.IndexOf("=")
        if ($split -lt 1) { continue }
        $name = $trimmed.Substring(0, $split).Trim()
        $value = $trimmed.Substring($split + 1).Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

$arguments = @("-m", "uvicorn", "app.server.app:app",
               "--host", $AppHost, "--port", $Port, "--log-level", "info")
if ($Reload) { $arguments += "--reload" }

Write-Host "CADSmith -> http://${AppHost}:${Port}" -ForegroundColor Cyan
& $python @arguments
