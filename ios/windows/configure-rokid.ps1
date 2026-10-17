<# Start the installed Rokid collector against the iPhone on the same LAN. #>
[CmdletBinding(DefaultParameterSetName = 'Manual')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Manual')][string]$IPhoneAddress,
    [Parameter(Mandatory = $true, ParameterSetName = 'Automatic')][switch]$AutoDiscover,
    [ValidateRange(1, 65535)][int]$Port = 18080,
    [string]$Adb,
    [string]$Serial,
    [string]$Package,
    [switch]$SkipReceiverCheck
)
. (Join-Path $PSScriptRoot 'common.ps1')
$adbPath = Resolve-OpenGlassAdb $Adb
$device = Get-OpenGlassRokid -Adb $adbPath -Serial $Serial -Package $Package
if ($AutoDiscover) {
    if ($device.Version -notmatch '^(\d+)\.(\d+)\.(\d+)' -or
        [version]$Matches[0] -lt [version]'0.2.0') {
        throw 'Automatic Wi-Fi discovery requires OpenGlassRokidSensor 0.2.0 or newer. Install the new APK first.'
    }
    $result = Invoke-OpenGlassAdb $adbPath @('-s', $device.Serial, 'shell', 'am', 'start', '-S', '-W', '-n', $device.Activity, '--ez', 'auto_discover', 'true')
    if ($result -match 'Error:|Exception') { throw "Collector launch failed: $result" }
    Write-Host 'Automatic Wi-Fi discovery enabled. The glasses verify /health before sending images or audio.'
    Write-Host 'Join the iPhone hotspot or the same Wi-Fi in the glasses settings, and keep OpenGlass receiving on iPhone.'
    Write-Host 'No Windows relay is used in this mode. Verify both streams before unplugging USB.'
    return
}
$address = $null
if (-not [System.Net.IPAddress]::TryParse($IPhoneAddress, [ref]$address) -or
    $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    [System.Net.IPAddress]::IsLoopback($address) -or $address.ToString() -eq '0.0.0.0') {
    throw 'Pass the iPhone Wi-Fi IPv4 address, not localhost, a URL, or the glasses address.'
}
$baseUrl = 'http://{0}:{1}' -f $address.ToString(), $Port
if (-not $SkipReceiverCheck) {
    try { $health = Invoke-RestMethod -Uri "$baseUrl/health" -TimeoutSec 5 }
    catch { throw "iPhone /health is unreachable at $baseUrl. Open OpenGlass on iPhone and start its receiver on the same LAN. $($_.Exception.Message)" }
    if ($health.PSObject.Properties.Name -notcontains 'service' -or $health.service -ne 'openglass-ios') {
        throw 'The endpoint is not the OpenGlass iOS receiver. Nothing was changed.'
    }
}
$result = Invoke-OpenGlassAdb $adbPath @('-s', $device.Serial, 'shell', 'am', 'start', '-S', '-W', '-n', $device.Activity, '--es', 'pc_base_url', $baseUrl)
if ($result -match 'Error:|Exception') { throw "Collector launch failed: $result" }
Write-Host "Started $($device.Package) $($device.Version) -> $baseUrl"
Write-Host 'Check that image_count and audio_packets_in increase on iPhone. The URL is saved by the collector.'
Write-Host 'Wi-Fi was not enabled or reconfigured by this script. Both devices must already have network access.'
