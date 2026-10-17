<# Read-only installation diagnostics. Does not sign, install, pair, or start apps. #>
param([string]$Adb, [string]$Serial, [string]$Package, [string]$Python = 'python', [string]$Ipa)
. (Join-Path $PSScriptRoot 'common.ps1')
Write-Host 'OpenGlass Windows / iPhone preflight'
Write-Host 'Apple credentials are not requested or read by this script.'

try {
    $adbPath = Resolve-OpenGlassAdb $Adb
    $device = Get-OpenGlassRokid -Adb $adbPath -Serial $Serial -Package $Package
    Write-Host "[OK] Rokid collector: $($device.Package), version $($device.Version)"
    Write-Host "     Activity: $($device.Activity)"
    $wifi = Invoke-OpenGlassAdb $adbPath @('-s', $device.Serial, 'shell', 'settings', 'get', 'global', 'wifi_on')
    Write-Host "     Wi-Fi setting: $wifi (0=off; USB relay does not require Wi-Fi)"
    $reverse = Invoke-OpenGlassAdb $adbPath @('-s', $device.Serial, 'reverse', '--list')
    if ($reverse) { Write-Host "     Existing reverse mappings (left untouched):`n$reverse" }
} catch { Write-Warning "Rokid: $($_.Exception.Message)" }

if (Get-Command Get-PnpDevice -ErrorAction SilentlyContinue) {
    $phones = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -match 'iPhone|Apple Mobile Device' })
    Write-Host "[INFO] Windows PnP iPhone/Apple Mobile Device entries: $($phones.Count)"
    foreach ($phone in $phones) { Write-Host "       $($phone.FriendlyName): $($phone.Status)" }
    Write-Host '       PnP detection alone does not prove trust, app installation, or a running listener.'
}
$appleService = @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq 'Apple Mobile Device Service' -or $_.DisplayName -eq 'Apple Mobile Device Service' })
if ($appleService.Count) { Write-Host "[INFO] Apple Mobile Device Service: $($appleService[0].Status)" }
else { Write-Warning 'Apple Mobile Device Service was not found. Follow the official AltServer Windows prerequisites.' }
foreach ($processName in @('AltServer', 'iTunes', 'iCloud')) {
    $running = @(Get-Process -Name $processName -ErrorAction SilentlyContinue).Count -gt 0
    Write-Host "[INFO] $processName running: $running"
}
try {
    $pythonPath = Resolve-OpenGlassPython $Python
    $moduleVersion = @(& $pythonPath -c 'from importlib.metadata import version; print(version("pymobiledevice3"))' 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'pymobiledevice3 is not installed in this Python environment.' }
    Write-Host "[INFO] pymobiledevice3: $($moduleVersion -join ' ')"
    Write-Host '[INFO] USB iPhone identifiers (local output; omit these when sharing logs):'
    & $pythonPath -m pymobiledevice3 usbmux list --usb --simple
    if ($LASTEXITCODE -ne 0) { Write-Warning 'usbmux discovery failed. Check USB, unlock/trust, and Apple Mobile Device Service.' }
} catch { Write-Warning "USB tools: $($_.Exception.Message)" }

if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 18080 -ErrorAction SilentlyContinue)
    if ($listeners.Count) {
        Write-Warning 'Windows port 18080 is occupied. Stop its owning session yourself, or use -LocalPort 18081 for the USB relay.'
        $listeners | Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table
    } else { Write-Host '[OK] No listener currently found on Windows port 18080.' }
}
if ($Ipa) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $Ipa).Path)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName })
        $appPlists = @($names | Where-Object { $_ -match '^Payload/[^/]+\.app/Info\.plist$' })
        if ($appPlists.Count -ne 1) { throw 'IPA must contain one Payload/<name>.app/Info.plist.' }
        $provision = @($names | Where-Object { $_ -match '^Payload/[^/]+\.app/embedded\.mobileprovision$' }).Count -gt 0
        $signature = @($names | Where-Object { $_ -match '^Payload/[^/]+\.app/_CodeSignature/CodeResources$' }).Count -gt 0
        Write-Host "[INFO] IPA layout valid; embedded profile=$provision, signature resources=$signature"
        Write-Host '       File presence is not signature verification. CI unsigned IPA must be re-signed using AltStore/AltServer before installation.'
    } finally { $archive.Dispose() }
}
Write-Host 'On iPhone: unlock, Trust This Computer, enable Developer Mode, then trust your signing profile when prompted.'
Write-Host 'For a previously unopenable app, check the actual error/expiry first; this script does not uninstall it.'
