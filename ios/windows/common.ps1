Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-OpenGlassAdb {
    param([string]$Adb)
    if (-not $Adb) {
        $found = Get-Command adb.exe -ErrorAction SilentlyContinue
        if ($found) { $Adb = $found.Source }
    }
    if (-not $Adb) {
        $candidate = Join-Path $env:USERPROFILE 'Downloads\platform-tools\adb.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $Adb = $candidate }
    }
    if (-not $Adb -or -not (Test-Path -LiteralPath $Adb -PathType Leaf)) {
        throw 'ADB not found. Install Android Platform Tools and pass -Adb <absolute adb.exe path>.'
    }
    return (Resolve-Path -LiteralPath $Adb).Path
}

function Invoke-OpenGlassAdb {
    param([string]$Adb, [string[]]$Arguments)
    $output = @(& $Adb @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "ADB failed ($LASTEXITCODE): $($output -join [Environment]::NewLine)" }
    return ($output -join [Environment]::NewLine)
}

function Get-OpenGlassRokid {
    param([string]$Adb, [string]$Serial, [string]$Package, [switch]$UsbOnly)
    $rows = Invoke-OpenGlassAdb $Adb @('devices', '-l')
    $ready = @($rows -split '\r?\n' | ForEach-Object {
        if ($_ -match '^([A-Za-z0-9_.:-]+)\s+device(?:\s|$)') { $Matches[1] }
    })
    if ($UsbOnly) { $ready = @($ready | Where-Object { $_ -notmatch ':' }) }
    if (-not $Serial) {
        if ($ready.Count -ne 1) { throw 'Expected exactly one authorized Android device. Unlock/authorize it or pass -Serial from adb devices -l.' }
        $Serial = $ready[0]
    }
    if ($Serial -notmatch '^[A-Za-z0-9_.:-]+$' -or $Serial -notin $ready) {
        throw 'The selected Rokid device is not authorized/online on the requested transport.'
    }
    $listing = Invoke-OpenGlassAdb $Adb @('-s', $Serial, 'shell', 'pm', 'list', 'packages')
    $installed = @($listing -split '\r?\n' | ForEach-Object {
        if ($_ -match '^package:(org\.opensqz\.openglass\.rokid(?:\.debug)?)\s*$') { $Matches[1] }
    })
    if (-not $Package) {
        if ($installed.Count -ne 1) { throw 'Expected exactly one OpenGlass Rokid package. Install the collector or pass -Package for the installed debug/release variant.' }
        $Package = $installed[0]
    }
    if ($Package -notin $installed) { throw 'The requested OpenGlass Rokid package is not installed.' }
    $info = Invoke-OpenGlassAdb $Adb @('-s', $Serial, 'shell', 'dumpsys', 'package', $Package)
    if ($info -notmatch 'versionName=(0\.1\.3(?:-debug)?)(?:\s|$)') {
        throw 'Collector version 0.1.3 is required for pc_base_url. Upgrade or review newer protocol compatibility first.'
    }
    $version = $Matches[1]
    $resolved = Invoke-OpenGlassAdb $Adb @('-s', $Serial, 'shell', 'cmd', 'package', 'resolve-activity', '--brief', '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER', $Package)
    $activities = @($resolved -split '\r?\n' | Where-Object { $_ -match ('^' + [regex]::Escape($Package) + '/[A-Za-z0-9_.$]+$') })
    if ($activities.Count -ne 1) { throw 'Could not resolve the installed collector launcher activity. No app was started.' }
    [pscustomobject]@{ Serial = $Serial; Package = $Package; Version = $version; Activity = $activities[0] }
}

function Resolve-OpenGlassPython {
    param([string]$Python = 'python')
    $found = Get-Command $Python -ErrorAction Stop
    $version = @(& $found.Source -c 'import sys; assert sys.version_info >= (3, 11); print(sys.executable)' 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'A working Python 3.11+ is required. Pass -Python <python.exe path>.' }
    return $found.Source
}
