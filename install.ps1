# Karakos Installer for Windows
# Run: irm https://raw.githubusercontent.com/mcarmody/karakos-package/main/install.ps1 | iex
# Or:  powershell -ExecutionPolicy Bypass -File install.ps1
#
# Override upstream source via env vars (useful for forks):
#   $env:KARAKOS_REPO = "user/repo"; .\install.ps1

param(
    [string]$InstallDir = "$env:USERPROFILE\karakos"
)

$KarakosRepo = if ($env:KARAKOS_REPO) { $env:KARAKOS_REPO } else { "mcarmody/karakos-package" }
$KarakosRepoUrl = if ($env:KARAKOS_REPO_URL) { $env:KARAKOS_REPO_URL } else { "https://github.com/$KarakosRepo.git" }

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "Warning: $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "Error: $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "================================" -ForegroundColor Cyan
Write-Host "  Karakos Installer" -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

# --- Check Windows version ---
$osVersion = [System.Environment]::OSVersion.Version
if ($osVersion.Build -lt 19041) {
    Write-Err "Windows 10 version 2004 or later required for WSL 2."
    Write-Err "Current build: $($osVersion.Build)"
    exit 1
}

# --- Check/install winget ---
$hasWinget = Get-Command winget -ErrorAction SilentlyContinue
if (-not $hasWinget) {
    Write-Err "winget not found. Please install App Installer from the Microsoft Store."
    Write-Err "https://apps.microsoft.com/detail/9NBLGGH4NNS1"
    exit 1
}

# --- Install Git ---
$hasGit = Get-Command git -ErrorAction SilentlyContinue
if (-not $hasGit) {
    Write-Step "Installing Git..."
    winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $hasGit = Get-Command git -ErrorAction SilentlyContinue
    if (-not $hasGit) {
        Write-Warn "Git installed but not in PATH yet. You may need to restart your terminal."
        Write-Warn "After restarting, run this script again."
        exit 1
    }
    Write-Step "Git installed."
} else {
    Write-Step "Git found: $(git --version)"
}

# --- Install Docker Desktop ---
$hasDocker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $hasDocker) {
    Write-Step "Installing Docker Desktop..."
    Write-Host "  This will install Docker Desktop with WSL 2 backend." -ForegroundColor Gray
    Write-Host "  A restart may be required after installation." -ForegroundColor Gray
    Write-Host ""

    winget install --id Docker.DockerDesktop -e --accept-source-agreements --accept-package-agreements

    Write-Warn "Docker Desktop installed."
    Write-Warn "Please:"
    Write-Warn "  1. Restart your computer if prompted"
    Write-Warn "  2. Launch Docker Desktop from the Start menu"
    Write-Warn "  3. Wait for it to finish starting (whale icon in system tray stops animating)"
    Write-Warn "  4. Run this script again"
    Write-Host ""
    Write-Step "Run this same command after Docker Desktop is running."
    exit 0
} else {
    # Check if Docker daemon is actually running
    $dockerRunning = $true
    try {
        docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { $dockerRunning = $false }
    } catch {
        $dockerRunning = $false
    }

    if (-not $dockerRunning) {
        Write-Err "Docker is installed but not running."
        Write-Err "Please start Docker Desktop and wait for it to finish loading, then run this script again."
        exit 1
    }

    Write-Step "Docker found and running."
}

# --- Check Docker Compose ---
try {
    docker compose version 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "no compose" }
    Write-Step "Docker Compose available."
} catch {
    Write-Err "Docker Compose not available. Make sure Docker Desktop is up to date."
    exit 1
}

# --- Install jq ---
$hasJq = Get-Command jq -ErrorAction SilentlyContinue
if (-not $hasJq) {
    Write-Step "Installing jq..."
    winget install --id jqlang.jq -e --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    Write-Step "jq installed."
} else {
    Write-Step "jq found."
}

# --- Install Python ---
# Required on the host (not just inside the container) because the repo's
# root .mcp.json registers `karakos-admin` as `python3 mcp/admin-server.py`.
# Claude Code on the host spawns that MCP server, so it needs a real python3
# on PATH. Windows 11 ships a `python3` Microsoft Store stub on PATH that
# opens the Store and exits non-zero when invoked — treat that as "not
# installed" and install the real thing.
function Test-RealPython3 {
    $cmd = Get-Command python3 -ErrorAction SilentlyContinue
    if (-not $cmd) { return $false }
    # Reject the WindowsApps Store stub (path under WindowsApps that exits 9009 on invoke).
    if ($cmd.Source -like "*\WindowsApps\*") { return $false }
    try {
        $ver = & python3 --version 2>&1
        return ($LASTEXITCODE -eq 0 -and $ver -match "^Python 3\.")
    } catch {
        return $false
    }
}

if (-not (Test-RealPython3)) {
    Write-Step "Installing Python 3.12..."
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Test-RealPython3)) {
        Write-Err "Python installed but 'python3' is not resolving on PATH."
        Write-Err "You may need to restart your shell or sign out/in, then re-run install.ps1."
        exit 1
    }
    Write-Step "Python 3.12 installed."
} else {
    Write-Step "Python found: $((& python3 --version) 2>&1)"
}

# --- Clone repository ---
if (Test-Path $InstallDir) {
    Write-Step "Directory $InstallDir already exists."
    $existing = Read-Host "  Overwrite? (y/N)"
    if ($existing -ne "y" -and $existing -ne "Y") {
        Write-Step "Keeping existing installation. Skipping clone."
    } else {
        Remove-Item -Recurse -Force $InstallDir
        Write-Step "Cloning karakos into $InstallDir..."
        git clone $KarakosRepoUrl $InstallDir
    }
} else {
    Write-Step "Cloning karakos into $InstallDir..."
    git clone $KarakosRepoUrl $InstallDir
}

Set-Location $InstallDir

# --- Run setup wizard via Git Bash ---
function Find-GitBash {
    # 1. Check common filesystem locations
    $searchPaths = @(
        "${env:ProgramFiles}\Git\bin\bash.exe",
        "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
        "${env:LOCALAPPDATA}\Programs\Git\bin\bash.exe",
        "${env:ProgramFiles}\Git for Windows\bin\bash.exe"
    )

    foreach ($path in $searchPaths) {
        if (Test-Path $path) {
            return $path
        }
    }

    # 2. Check Windows Registry for Git install location
    $regPaths = @(
        "HKLM:\SOFTWARE\GitForWindows",
        "HKCU:\SOFTWARE\GitForWindows",
        "HKLM:\SOFTWARE\WOW6432Node\GitForWindows"
    )
    foreach ($regPath in $regPaths) {
        try {
            $gitDir = (Get-ItemProperty -Path $regPath -ErrorAction SilentlyContinue).InstallPath
            if ($gitDir) {
                $candidate = Join-Path $gitDir "bin\bash.exe"
                if (Test-Path $candidate) {
                    return $candidate
                }
            }
        } catch {}
    }

    # 3. Search PATH, but reject WSL shim (System32\bash.exe or WindowsApps\bash.exe)
    $bashInPath = (Get-Command bash -ErrorAction SilentlyContinue).Source
    if ($bashInPath -and -not ($bashInPath -like "*System32*") -and -not ($bashInPath -like "*WindowsApps*")) {
        return $bashInPath
    }

    return $null
}

$gitBash = Find-GitBash

if (-not $gitBash) {
    Write-Err "Git Bash not found."
    Write-Err ""
    Write-Err "Karakos requires Git for Windows (which includes Git Bash)."
    Write-Err "The WSL bash shim is not supported — Git Bash is required."
    Write-Err ""
    Write-Err "Install Git for Windows:"
    Write-Err "  winget install --id Git.Git"
    Write-Err "  (or download from https://git-scm.com/download/win)"
    Write-Err ""
    Write-Err "Then restart your terminal and run this script again."
    exit 1
}

Write-Step "Using bash: $gitBash"

# Convert Windows path to Git Bash (MSYS) path for the setup script
# C:\Users\foo\karakos -> /c/Users/foo/karakos
$setupPath = $InstallDir -replace '\\', '/'
if ($setupPath -match '^([A-Za-z]):(.*)') {
    $setupPath = '/' + $Matches[1].ToLower() + $Matches[2]
}
$setupCmd = "cd '$setupPath' && bash ./setup.sh"

Write-Host ""
Write-Host "================================" -ForegroundColor Cyan
Write-Host "  Prerequisites installed." -ForegroundColor Cyan
Write-Host "  Launching setup wizard..." -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

& $gitBash --login -c $setupCmd

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Step "Setup complete! Starting Karakos..."
    Write-Host ""

    Set-Location "$InstallDir\config"
    docker compose up -d

    Write-Host ""
    Write-Step "Karakos is starting up. First build takes 5-10 minutes."
    Write-Host ""
    Write-Host "  Dashboard:  http://localhost:3000" -ForegroundColor Cyan
    Write-Host "  Logs:       docker compose logs -f  (run from $InstallDir\config)" -ForegroundColor Gray
    Write-Host ""
} else {
    Write-Err "Setup wizard failed or was cancelled."
    Write-Err "You can re-run it with: cd $InstallDir && bash setup.sh"
    exit 1
}
