# Install kvm-switcher onto PATH. Uses this checkout when it has a built
# dist\kvm-switcher.pyz, otherwise the latest GitHub release asset.
#
# The program is a single-file zipapp: one kvm-switcher.pyz that the interpreter
# runs directly. Nothing is unpacked, so update and uninstall stay single-file.
#
# Python is a real prerequisite, so it is checked before anything is written. A
# machine with no Python used to get a shim that failed on every use, and a
# Windows Store alias stub (`python.exe` that exists but only opens the Store)
# passed any existence check, so the check here is a real version probe.
$ErrorActionPreference = "Stop"

$repo = "algun/kvm-switcher"
$ref = "main"
$program = "kvm-switcher.pyz"
$minMajor = 3
$minMinor = 9
$dest = Join-Path $env:LOCALAPPDATA "kvm-switcher"
$bin = Join-Path $env:USERPROFILE "bin"

function Write-Guidance {
    Write-Host ""
    Write-Host "kvm-switcher needs Python $minMajor.$minMinor or newer." -ForegroundColor Red
    Write-Host "Install it, then run this installer again:"
    Write-Host ""
    Write-Host "    winget install --id Python.Python.3.12 -e"
    Write-Host ""
    Write-Host "If 'python' opens the Microsoft Store instead of running Python, that is"
    Write-Host "the Store alias stub, not Python. Install Python as above, then turn the"
    Write-Host "stub off in Settings > Apps > Advanced app settings > App execution"
    Write-Host "aliases (clear python.exe and python3.exe)."
}

# Resolve a working interpreter. `py -3` is preferred because it finds the
# newest installed Python even when PATH has been rearranged.
#
# The probe deliberately contains no quote characters. Windows PowerShell 5.1
# re-quotes an argument on its way to a native command and strips embedded
# double quotes, so a probe like sys.stdout.write("%d.%d" % ...) arrives as
# invalid Python. Two plain numbers, split on whitespace, cannot be mangled.
$probe = 'import sys; print(sys.version_info[0], sys.version_info[1])'

function Test-Interpreter([string]$Exe, [string[]]$Prefix) {
    # Native stderr must not become a terminating error under "Stop".
    $ErrorActionPreference = "Continue"
    $argv = @()
    if ($Prefix) { $argv += $Prefix }
    $argv += @("-c", $probe)
    $output = $null
    try {
        $output = & $Exe @argv 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    # A Store alias stub prints nothing useful and exits non-zero, so anything
    # that is not exactly two numbers here is not Python.
    $text = (@($output) -join " ").Trim()
    if ($text -notmatch '^(\d+)\s+(\d+)$') { return $null }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    $resolvedSource = (Get-Command $Exe -ErrorAction SilentlyContinue).Source
    if (-not $resolvedSource) { $resolvedSource = $Exe }
    return [pscustomobject]@{
        Path   = $resolvedSource
        Prefix = $Prefix
        Major  = $major
        Minor  = $minor
        Text   = "$major.$minor"
    }
}

$python = $null
$tooOld = @()
$candidates = @(
    @{ Name = "py"; Prefix = @("-3") },
    @{ Name = "python"; Prefix = @() },
    @{ Name = "python3"; Prefix = @() }
)
foreach ($candidate in $candidates) {
    $resolved = Get-Command $candidate.Name -ErrorAction SilentlyContinue
    if (-not $resolved) { continue }
    $found = Test-Interpreter $candidate.Name $candidate.Prefix
    if (-not $found) { continue }
    if ($found.Major -gt $minMajor -or ($found.Major -eq $minMajor -and $found.Minor -ge $minMinor)) {
        $python = $found
        break
    }
    $tooOld += "$($found.Path) ($($found.Text))"
}

if (-not $python) {
    if ($tooOld.Count -gt 0) {
        Write-Host "Found Python, but it is too old: $($tooOld -join ', ')" -ForegroundColor Red
    }
    Write-Guidance
    exit 1
}

Write-Host "Using Python $($python.Text) at $($python.Path)"

# Under `irm <url> | iex` both $PSScriptRoot and MyCommand.Path are empty, so the
# path is optional. Requiring it made the documented one-liner die immediately.
$here = $PSScriptRoot
if (-not $here -and $MyInvocation.MyCommand.Path) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
}

New-Item -ItemType Directory -Force -Path $dest, $bin | Out-Null

$local = if ($here) {
    $beside = Join-Path $here $program
    $inDist = Join-Path $here "dist\$program"
    # An extracted release archive puts the program beside this script; a source
    # checkout has it in dist/ after a build.
    if (Test-Path $beside) { $beside }
    elseif (Test-Path $inDist) { $inDist }
    else { $null }
} else { $null }
if ($local) {
    Write-Host "Installing from $local"
    Copy-Item -Force $local (Join-Path $dest $program)
    # The seed config comes from wherever the program was found.
    $seedDir = Split-Path -Parent $local
    if (Test-Path (Join-Path $seedDir "config.json")) {
        Copy-Item -Force (Join-Path $seedDir "config.json") $dest
    }
    Get-ChildItem -Path $here -Filter "*.ps1" -File | Copy-Item -Destination $dest -Force
    Get-ChildItem -Path $here -Filter "*.sh" -File | Copy-Item -Destination $dest -Force
} else {
    # The zipapp is a release asset, not a file in the repository: it is built
    # at release time. `latest/download` keeps the name stable, which is also
    # what `kvm-switcher update` fetches, so both paths get the same artifact.
    $url = "https://github.com/$repo/releases/latest/download/$program"
    Write-Host "Downloading $url"
    # -UseBasicParsing: Windows PowerShell 5.1 can fail without it when the
    # Internet Explorer engine has never been initialised.
    Invoke-WebRequest -Uri $url -OutFile (Join-Path $dest $program) -UseBasicParsing
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Download failed: $url" -ForegroundColor Red
        exit 1
    }
    # The seed config is optional: a missing one only means `configure` asks.
    try {
        Invoke-WebRequest -Uri "https://raw.githubusercontent.com/$repo/$ref/config.json" `
            -OutFile (Join-Path $dest "config.json") -UseBasicParsing
    } catch {
        Write-Host "No seed config downloaded; 'kvm-switcher configure' will ask instead."
    }
}

# Refuse to leave a payload that is not the program in place: a captive portal
# or a proxy error page would otherwise be installed as a working-looking file.
# Running it is the real check - `version` proves the archive is a zipapp that
# carries the package and that the interpreter can execute it.
$installed = Join-Path $dest $program
$ErrorActionPreference = "Continue"
$reported = & $python.Path $installed version 2>$null
$probeExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
$reportedText = (@($reported) -join "").Trim()
if ($probeExit -ne 0 -or $reportedText -notmatch '^\d+\.\d+') {
    Write-Host "The downloaded program does not run (exit $probeExit); nothing installed." -ForegroundColor Red
    Remove-Item -Force $installed -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "Verified $program $reportedText"

# The shim pins the interpreter the gate verified, so a later PATH change cannot
# silently point it at the Store stub. The .pyz promotes its own staged updates
# at start-up, so the shim needs no .new handling of its own.
$shim = Join-Path $bin "kvm-switcher.cmd"
@"
@echo off
"$($python.Path)" "%LOCALAPPDATA%\kvm-switcher\$program" %*
"@ | Set-Content -Encoding ascii $shim

# A pre-package install left kvm-switcher.py here, and the old shim pointed at
# it. Removing it is the migration: anything that still named the old file now
# fails loudly instead of running a stale copy this installer did not update.
$migrated = $false
$legacy = Join-Path $dest "kvm-switcher.py"
if (Test-Path $legacy) {
    Remove-Item -Force $legacy
    $migrated = $true
    Write-Host "Removed the old single-file program ($legacy)"
}

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
if ($migrated) {
    # The old login entry names the .py that was just removed, so it is now
    # wrong. Registering a service is not this installer's job, so say what is
    # needed rather than doing it behind the user's back.
    Write-Host "Your login watcher still points at the old program." -ForegroundColor Yellow
    Write-Host "Re-register it now: kvm-switcher install"
} else {
    Write-Host "Login watcher: kvm-switcher install"
}
