# Install kvm-switcher onto PATH. Uses this checkout when the script is next
# to kvm-switcher.py, otherwise downloads the program from the repo.
#
# A piped run (`irm <url> | iex`) cannot tell which URL it came from, so the
# program is fetched from $ref below. Keep it pointing at the branch you serve
# the one-liner from, or a main-branch install will ship stale program code.
$ErrorActionPreference = "Stop"
$repo = "algun/kvm-switcher"
$ref = "main"
$dest = Join-Path $env:LOCALAPPDATA "kvm-switcher"
$bin = Join-Path $env:USERPROFILE "bin"

# Under `irm <url> | iex` both $PSScriptRoot and MyCommand.Path are empty, so the
# path is optional. Requiring it made the documented one-liner die immediately.
$here = $PSScriptRoot
if (-not $here -and $MyInvocation.MyCommand.Path) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
}

New-Item -ItemType Directory -Force -Path $dest, $bin | Out-Null

$local = if ($here) { Join-Path $here "kvm-switcher.py" } else { $null }
if ($local -and (Test-Path $local)) {
    Copy-Item -Force $local $dest
    if (Test-Path (Join-Path $here "config.json")) {
        Copy-Item -Force (Join-Path $here "config.json") $dest
    }
    Get-ChildItem -Path $here -Filter "*.ps1" -File | Copy-Item -Destination $dest -Force
    Get-ChildItem -Path $here -Filter "*.sh" -File | Copy-Item -Destination $dest -Force
} else {
    # -UseBasicParsing: Windows PowerShell 5.1 can fail without it when the
    # Internet Explorer engine has never been initialised.
    $raw = "https://raw.githubusercontent.com/$repo/$ref"
    foreach ($name in "kvm-switcher.py", "config.json") {
        Invoke-WebRequest -Uri "$raw/$name" -OutFile (Join-Path $dest $name) -UseBasicParsing
    }
}

$shim = Join-Path $bin "kvm-switcher.cmd"
@"
@echo off
python "%LOCALAPPDATA%\kvm-switcher\kvm-switcher.py" %*
"@ | Set-Content -Encoding ascii $shim

Write-Host "Installed $shim"

# The README says this puts kvm-switcher on PATH, so actually add it. Without
# this the shim is installed but the command is not found.
$userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
$entries = @($userPath -split ';' | Where-Object { $_ })
if ($entries -notcontains $bin) {
    $newPath = (@($entries) + $bin) -join ';'
    [System.Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Added $bin to your PATH"
    Write-Host "Open a new terminal for it to take effect."
} else {
    Write-Host "$bin is already on PATH"
}

Write-Host "Then: kvm-switcher configure"
Write-Host "Login watcher: kvm-switcher install"
