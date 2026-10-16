<# Download one successful, explicitly selected GitHub Actions run. Never uploads or starts CI. #>
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')][string]$Repository,
    [Parameter(Mandatory = $true)][ValidateRange(1, [long]::MaxValue)][long]$RunId,
    [string]$ArtifactName,
    [string]$Destination
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$gh = (Get-Command gh -ErrorAction Stop).Source
$runJson = @(& $gh api "repos/$Repository/actions/runs/$RunId" 2>&1)
if ($LASTEXITCODE -ne 0) { throw 'Cannot read the run. Check the repository/run ID and use gh auth login yourself if needed.' }
$run = ($runJson -join [Environment]::NewLine) | ConvertFrom-Json
if ($run.status -ne 'completed' -or $run.conclusion -ne 'success') { throw 'The selected workflow run has not completed successfully.' }
if ($run.path -notmatch '(^|/)ios-ipa\.yml(?:@.*)?$') { throw 'The run is not the ios-ipa.yml workflow. Select the iOS build run.' }
$artifactJson = @(& $gh api "repos/$Repository/actions/runs/$RunId/artifacts?per_page=100" 2>&1)
if ($LASTEXITCODE -ne 0) { throw 'Cannot list the run artifacts.' }
$artifactResponse = ($artifactJson -join [Environment]::NewLine) | ConvertFrom-Json
$candidates = @($artifactResponse.artifacts | Where-Object {
    -not $_.expired -and $_.name -match '^OpenGlass-iOS-unsigned-[0-9]+$'
})
if ($ArtifactName) { $candidates = @($candidates | Where-Object { $_.name -eq $ArtifactName }) }
if ($candidates.Count -ne 1) { throw 'Expected exactly one non-expired OpenGlass iOS artifact. Pass -ArtifactName or inspect the selected run.' }
if (-not $Destination) {
    $Destination = Join-Path (Split-Path -Parent $PSScriptRoot) "artifacts\run-$RunId"
}
$fullDestination = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $fullDestination) { throw "Destination already exists; choose a new empty output path: $fullDestination" }
New-Item -ItemType Directory -Path $fullDestination | Out-Null
& $gh run download "$RunId" --repo $Repository --name $candidates[0].name --dir $fullDestination
if ($LASTEXITCODE -ne 0) { throw "Artifact download failed. Partial output remains for inspection: $fullDestination" }
$ipa = Join-Path $fullDestination 'OpenGlass-1.0.ipa'
if (-not (Test-Path -LiteralPath $ipa -PathType Leaf)) { throw "The artifact is missing OpenGlass-1.0.ipa: $fullDestination" }
Write-Host "Downloaded unsigned IPA: $ipa"
Write-Host "Run: $($run.html_url)"
Write-Host "Commit: $($run.head_sha)"
Get-FileHash -LiteralPath $ipa -Algorithm SHA256 | Format-List
Write-Host 'Next: sign/install with AltStore and AltServer on Windows. Downloading an unsigned IPA does not install or authorize it.'
