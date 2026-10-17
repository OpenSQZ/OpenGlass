<# Keep this terminal open. Ctrl+C removes only the relay mapping created by this run. #>
param(
    [string]$Adb,
    [string]$Serial,
    [string]$Package,
    [string]$IPhoneUdid,
    [string]$Python = 'python',
    [ValidateRange(1, 65535)][int]$LocalPort = 18080,
    [ValidateRange(1, 65535)][int]$IPhonePort = 18080,
    [ValidateRange(1, 65535)][int]$RokidPort = 18080
)
. (Join-Path $PSScriptRoot 'common.ps1')
$adbPath = Resolve-OpenGlassAdb $Adb
$pythonPath = Resolve-OpenGlassPython $Python
$device = Get-OpenGlassRokid -Adb $adbPath -Serial $Serial -Package $Package -UsbOnly
$relayArgs = @(
    (Join-Path $PSScriptRoot 'usb_relay.py'),
    '--adb', $adbPath, '--rokid-serial', $device.Serial, '--activity', $device.Activity,
    '--local-port', "$LocalPort", '--iphone-port', "$IPhonePort", '--rokid-port', "$RokidPort"
)
if ($IPhoneUdid) { $relayArgs += @('--iphone-udid', $IPhoneUdid) }
& $pythonPath @relayArgs
if ($LASTEXITCODE -ne 0) { throw "USB relay exited with code $LASTEXITCODE. Read the diagnostic above; no unrelated processes were stopped." }
