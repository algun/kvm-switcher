# Install kvm-switcher onto PATH. Uses this checkout when the script is next
# to kvm-switcher.py, otherwise the latest GitHub release.
$ErrorActionPreference = "Stop"
$repo = "algun/kvm-switcher"
$dest = Join-Path $env:LOCALAPPDATA "kvm-switcher"
$bin = Join-Path $env:USERPROFILE "bin"
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }

New-Item -ItemType Directory -Force -Path $dest, $bin | Out-Null

$local = Join-Path $here "kvm-switcher.py"
if (Test-Path $local) {
    Copy-Item -Force $local $dest
    Copy-Item -Force (Join-Path $here "config.json") $dest
    Copy-Item -Force (Join-Path $here "*.ps1") $dest
    Copy-Item -Force (Join-Path $here "*.sh") $dest
} else {
    $zip = Join-Path $env:TEMP "kvm-switcher.zip"
    Invoke-WebRequest -Uri "https://github.com/$repo/releases/latest/download/kvm-switcher.zip" -OutFile $zip
    $unpack = Join-Path $env:TEMP "kvm-switcher-unpack"
    if (Test-Path $unpack) { Remove-Item -Recurse -Force $unpack }
    Expand-Archive -Force $zip $unpack
    Copy-Item -Force (Join-Path $unpack "kvm-switcher\*") $dest
}

$shim = Join-Path $bin "kvm-switcher.cmd"
@"
@echo off
python "%LOCALAPPDATA%\kvm-switcher\kvm-switcher.py" %*
"@ | Set-Content -Encoding ascii $shim

Write-Host "Installed $shim"
Write-Host "Then: kvm-switcher configure"
Write-Host "Login watcher: kvm-switcher install"
