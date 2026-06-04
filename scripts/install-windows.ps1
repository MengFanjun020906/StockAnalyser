# =====================================================================
# install-windows.ps1 — One-shot Windows host bootstrap for DSA
#
# What this does (Windows side, before WSL):
#   1. Enable WSL2 + Virtual Machine Platform features
#   2. Install Ubuntu (default distro) via `wsl --install -d Ubuntu`
#   3. Print next-step instructions
#
# What this does NOT do:
#   - Does not install Python / Node / project deps inside WSL
#     -> run scripts/bootstrap-wsl.sh inside Ubuntu for that
#   - Does not transfer .env or databases
#     -> see docs/deploy-to-new-windows.md
#
# Usage (open PowerShell as Administrator):
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\scripts\install-windows.ps1
#
# Reboot is typically required after first WSL feature enablement.
# Re-run this script after reboot to continue.
# =====================================================================

[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu",
    [switch]$SkipReboot
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Test-IsAdmin {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($current)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
    Write-Host "ERROR: please run this script in an elevated PowerShell (Run as Administrator)." -ForegroundColor Red
    exit 1
}

Write-Step "Step 1/4: Enable WSL feature"
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart | Out-Null

Write-Step "Step 2/4: Enable Virtual Machine Platform"
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart | Out-Null

Write-Step "Step 3/4: Set WSL default version to 2"
try {
    wsl --set-default-version 2 2>$null
} catch {
    Write-Host "wsl.exe not yet available — will retry after reboot." -ForegroundColor Yellow
}

Write-Step "Step 4/4: Install distro: $Distro"
$installed = $false
try {
    $list = wsl --list --quiet 2>$null
    if ($LASTEXITCODE -eq 0 -and $list -match $Distro) {
        Write-Host "$Distro is already installed." -ForegroundColor Green
        $installed = $true
    }
} catch { }

if (-not $installed) {
    try {
        wsl --install -d $Distro --no-launch
        Write-Host "$Distro install command dispatched." -ForegroundColor Green
    } catch {
        Write-Host "Initial wsl --install failed. After reboot, re-run this script." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "================ NEXT STEPS ================" -ForegroundColor Green
Write-Host "1. Reboot Windows if this is the first time you enabled WSL features." -ForegroundColor White
Write-Host "2. After reboot, open '$Distro' from Start Menu and set username/password." -ForegroundColor White
Write-Host "3. Copy the project folder into WSL, e.g. from PowerShell:" -ForegroundColor White
Write-Host "     wsl -d $Distro" -ForegroundColor Gray
Write-Host "     # then inside WSL:" -ForegroundColor Gray
Write-Host "     mkdir -p ~/code && cd ~/code" -ForegroundColor Gray
Write-Host "     cp -r /mnt/c/path/to/daily_stock_analysis ./" -ForegroundColor Gray
Write-Host "4. Inside WSL, run the bootstrap:" -ForegroundColor White
Write-Host "     cd ~/code/daily_stock_analysis" -ForegroundColor Gray
Write-Host "     bash scripts/bootstrap-wsl.sh" -ForegroundColor Gray
Write-Host "5. Follow docs/deploy-to-new-windows.md for .env and DB notes." -ForegroundColor White
Write-Host "============================================" -ForegroundColor Green

if (-not $SkipReboot) {
    $ans = Read-Host "Reboot now? (y/N)"
    if ($ans -match '^[Yy]') {
        Restart-Computer -Force
    }
}
