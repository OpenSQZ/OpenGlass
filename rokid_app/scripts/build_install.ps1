param(
    [switch]$Install,
    [string]$Serial = "",
    [string]$Adb = $env:ROKID_ADB,
    [string]$Sdk = $env:ANDROID_HOME,
    [ValidateSet('Debug', 'Wifi')][string]$Variant = 'Debug'
)

$ErrorActionPreference = "Stop"
$projectRoot = Join-Path (Split-Path -Parent $PSScriptRoot) "OpenGlassRokidSensor"
if (-not $Sdk) { $Sdk = $env:ANDROID_SDK_ROOT }
if ($Sdk) {
    $Sdk = (Resolve-Path -LiteralPath $Sdk).Path
    $env:ANDROID_HOME = $Sdk
    $env:ANDROID_SDK_ROOT = $Sdk
}
if (-not $Sdk -and -not (Test-Path -LiteralPath (Join-Path $projectRoot "local.properties"))) {
    throw "Set ANDROID_HOME, pass -Sdk, or configure local.properties in the Android project."
}

# Resolve the device before spending time on a build. Never choose a device silently.
if ($Install) {
    if (-not $Adb -and $Sdk) { $Adb = Join-Path $Sdk "platform-tools\adb.exe" }
    if (-not $Adb) { $Adb = "adb.exe" }
    $Adb = (Get-Command $Adb -ErrorAction Stop).Source
    $devices = @(& $Adb devices -l)
    if ($LASTEXITCODE -ne 0) { throw "adb devices failed." }
    $ready = @($devices | Where-Object { $_ -match '^\S+\s+device(?:\s|$)' } |
        ForEach-Object { ($_ -split '\s+')[0] })
    if (-not $Serial) {
        if ($ready.Count -ne 1) { throw "Connect and authorize one device, or pass -Serial. USB and wireless entries count separately." }
        $Serial = $ready[0]
    }
    if ($Serial -notin $ready) { throw "Selected device is not authorized/online: $Serial" }
}

Push-Location $projectRoot
try {
    & .\gradlew.bat ":app:assemble$Variant"
    if ($LASTEXITCODE -ne 0) { throw "Gradle build failed; nothing installed." }
} finally {
    Pop-Location
}
$variantFolder = $Variant.ToLowerInvariant()
$apk = Join-Path $projectRoot "app\build\outputs\apk\$variantFolder\app-$variantFolder.apk"
if (-not (Test-Path -LiteralPath $apk)) { throw "Build did not produce the expected APK." }
Write-Host "Built: $apk"
if ($Install) {
    & $Adb -s $Serial install -r $apk
    if ($LASTEXITCODE -ne 0) {
        throw "Install failed. Check vendor authorization or signing mismatch. This script never uninstalls an existing app."
    }
    Write-Host "Installed. Allow Camera and Microphone on the glasses when prompted."
}
Write-Host "For iPhone: join its hotspot/shared Wi-Fi and open both OpenGlass apps (Auto mode). For PC: configure runtime.local.json and start glasses_panel.py."
