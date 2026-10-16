<# Start the installed Rokid collector against the iPhone on the same LAN. #>
param(
    [Parameter(Mandatory = $true)][string]$IPhoneAddress,
    [ValidateRange(1, 65535)][int]$Port = 18080,
    [string]$Adb,
    [string]$Serial,
    [string]$Package,
    [switch]$SkipReceiverCheck
)
. (Join-Path $PSScriptRoot 'common.ps1')
$address = $null
if (-not [System.Net.IPAddress]::TryParse($IPhoneAddress, [ref]$address) -or
    $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    [System.Net.IPAddress]::IsLoopback($address) -or $address.ToString() -eq '0.0.0.0') {
    throw 'Pass the iPhone Wi-Fi IPv4 address, not localhost, a URL, or the glasses address.'
}
$baseUrl = 'http://{0}:{1}' -f $address.ToString(), $Port
$adbPath = Resolve-OpenGlassAdb $Adb
$device = Get-OpenGlassRokid -Adb $adbPath -Serial $Serial -Package $Package
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
