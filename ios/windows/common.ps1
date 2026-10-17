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
        if ($_ -match '^package:(org\.opensqz\.openglass\.rokid(?:\.(?:debug|wifi))?)\s*$') { $Matches[1] }
    })
    if (-not $Package) {
        if ($installed.Count -ne 1) { throw 'Expected exactly one OpenGlass Rokid package. Install the collector or pass -Package for the installed debug/release variant.' }
        $Package = $installed[0]
    }
    if ($Package -notin $installed) { throw 'The requested OpenGlass Rokid package is not installed.' }
    $info = Invoke-OpenGlassAdb $Adb @('-s', $Serial, 'shell', 'dumpsys', 'package', $Package)
    if ($info -notmatch 'versionName=((?:0\.1\.3|0\.2\.0)(?:-(?:debug|wifi))?)(?:\s|$)') {
        throw 'Collector version 0.1.3 or 0.2.0 is required. Upgrade or review newer protocol compatibility first.'
    }
    $version = $Matches[1]
    $resolved = Invoke-OpenGlassAdb $Adb @('-s', $Serial, 'shell', 'cmd', 'package', 'resolve-activity', '--brief', '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER', $Package)
    $activities = @($resolved -split '\r?\n' | Where-Object { $_ -match ('^' + [regex]::Escape($Package) + '/[A-Za-z0-9_.$]+$') })
    if ($activities.Count -ne 1) { throw 'Could not resolve the installed collector launcher activity. No app was started.' }
    [pscustomobject]@{ Serial = $Serial; Package = $Package; Version = $version; Activity = $activities[0] }
}

function Resolve-OpenGlassPython {
    param([string]$Python = 'python')
    $candidates = @()
    if ($Python -eq 'python') {
        $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
        $candidates += Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
        $candidates += Join-Path $repoRoot 'tmp\ios-tools\Scripts\python.exe'
        $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pathPython) { $candidates += $pathPython.Source }
        $candidates += Join-Path $env:USERPROFILE 'miniconda3\python.exe'
        $candidates += Join-Path $env:USERPROFILE 'anaconda3\python.exe'
    } else {
        $explicit = Get-Command $Python -ErrorAction SilentlyContinue
        if ($explicit) { $candidates += $explicit.Source }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        # Do not launch Windows' Store app-execution alias during a diagnostic.
        if ($candidate -match '[\\/]Microsoft[\\/]WindowsApps[\\/]' -or
            -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $version = @(& $candidate -c 'import sys; assert sys.version_info >= (3, 11); print(sys.executable)' 2>&1)
        if ($LASTEXITCODE -eq 0) { return (Resolve-Path -LiteralPath $candidate).Path }
    }
    throw 'No working Python 3.11+ found (WindowsApps Store aliases are skipped). Pass -Python C:\path\to\python.exe, then install ios/windows/requirements.txt into that environment.'
}
