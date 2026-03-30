param(
    [switch]$OnlySetup,
    [bool]$NoSitemap = $true,
    [string]$CategoriesFile = "config/sitemap-category-ru.csv",
    [switch]$SkipPythonInstall,
    [switch]$SkipDependencyInstall,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Info([string]$Message) {
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Write-Warn([string]$Message) {
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Resolve-PythonExe([string]$RepoRoot) {
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return $venvPython
    }

    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        foreach ($selector in @("-3", "-3.13", "-3.12", "-3.11", "-3.10")) {
            try {
                $resolved = (& py $selector -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1).Trim()
                if ($resolved -and (Test-Path $resolved)) {
                    return $resolved
                }
            }
            catch {
            }
        }
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and $pythonCmd.Source) {
        return $pythonCmd.Source
    }

    foreach ($candidate in @(
            "$env:LocalAppData\Programs\Python\Python313\python.exe",
            "$env:LocalAppData\Programs\Python\Python312\python.exe",
            "$env:LocalAppData\Programs\Python\Python311\python.exe",
            "$env:LocalAppData\Programs\Python\Python310\python.exe"
        )) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return $null
}

function Install-PythonWithWinget {
    $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $wingetCmd) {
        throw "Python was not found and winget is unavailable. Install Python 3.10+ manually and re-run START.bat."
    }

    Write-Info "Python not found. Installing Python 3.13 with winget..."
    & winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install Python (exit code $LASTEXITCODE)."
    }

    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

$RepoRoot = Split-Path -Parent $PSCommandPath
Set-Location $RepoRoot

Write-Info "Repository: $RepoRoot"

$pythonExe = Resolve-PythonExe -RepoRoot $RepoRoot
if (-not $pythonExe) {
    if ($SkipPythonInstall) {
        throw "Python was not found and -SkipPythonInstall was specified."
    }
    Install-PythonWithWinget
    $pythonExe = Resolve-PythonExe -RepoRoot $RepoRoot
}

if (-not $pythonExe) {
    throw "Python is still unavailable after attempted install."
}

Write-Info "Using Python: $pythonExe"

$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Info "Creating virtual environment (.venv)..."
    & $pythonExe -m venv ".venv"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw "Failed to create .venv (exit code $LASTEXITCODE)."
    }
}
else {
    Write-Info "Virtual environment already exists."
}

if (-not $SkipDependencyInstall) {
    Write-Info "Installing/updating pip, setuptools, wheel..."
    & $venvPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) {
        throw "pip upgrade failed (exit code $LASTEXITCODE)."
    }

    Write-Info "Installing requirements.txt..."
    & $venvPython -m pip install -r "requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency install failed (exit code $LASTEXITCODE)."
    }
}
else {
    Write-Warn "Dependency install skipped by -SkipDependencyInstall."
}

if ($OnlySetup) {
    Write-Info "Setup complete. Start parsing with START.bat"
    exit 0
}

$crawlerArgs = @("main.py")
if ($NoSitemap) {
    $crawlerArgs += "--no-sitemap"
}

if ($CategoriesFile) {
    $categoriesPath = if ([System.IO.Path]::IsPathRooted($CategoriesFile)) {
        $CategoriesFile
    }
    else {
        Join-Path $RepoRoot $CategoriesFile
    }

    if (Test-Path $categoriesPath) {
        $crawlerArgs += @("--categories-file", $categoriesPath)
    }
    else {
        Write-Warn "Categories file not found: $categoriesPath. Starting without --categories-file."
    }
}

if ($ExtraArgs -and $ExtraArgs.Count -gt 0) {
    $crawlerArgs += $ExtraArgs
}

Write-Info ("Starting crawler: {0} {1}" -f $venvPython, ($crawlerArgs -join " "))
& $venvPython @crawlerArgs
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    throw "Crawler exited with code $exitCode."
}

Write-Info "Crawler finished successfully."
