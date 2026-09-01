param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$Date = '2026-08-11'
)

$ErrorActionPreference = 'Stop'
$deliverables = Join-Path $RepoRoot 'deliverables'
$packageName = "VOICE_SKILL_HARNESS_PHASE_A_CODEX_RETURN_$Date"
$stage = Join-Path $deliverables $packageName
$zipPath = Join-Path $deliverables "$packageName.zip"

$resolvedRepo = (Resolve-Path $RepoRoot).Path.TrimEnd('\')
New-Item -ItemType Directory -Force -Path $deliverables | Out-Null
$resolvedDeliverables = (Resolve-Path $deliverables).Path
if (-not $resolvedDeliverables.StartsWith($resolvedRepo, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe delivery target: $resolvedDeliverables"
}
if (Test-Path $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

function Copy-TreeFiltered([string]$Source, [string]$Destination) {
    Get-ChildItem -LiteralPath $Source -Recurse -File | Where-Object {
        $_.FullName -notmatch '[\\/]runs[\\/]' -and
        $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and
        $_.FullName -notmatch '[\\/]test_artifacts[\\/]' -and
        $_.Extension -ne '.pyc'
    } | ForEach-Object {
        $relative = $_.FullName.Substring($Source.TrimEnd('\').Length).TrimStart('\')
        $target = Join-Path $Destination $relative
        New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $target
    }
}

Copy-TreeFiltered (Join-Path $RepoRoot 'extensions\assistive_harness') (Join-Path $stage 'extensions\assistive_harness')
Copy-TreeFiltered (Join-Path $RepoRoot 'static\assistive_harness') (Join-Path $stage 'static\assistive_harness')
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'static\omni') | Out-Null
Copy-Item -LiteralPath (Join-Path $RepoRoot 'static\omni\omni-app.js') -Destination (Join-Path $stage 'static\omni\omni-app.js')

$docNames = @(
    'VOICE_SKILL_HARNESS_AUDIT.md',
    'VOICE_SKILL_HARNESS_DESIGN.md',
    'VOICE_SKILL_HARNESS_EXECUTION_REPORT.md',
    'VOICE_SKILL_HARNESS_GO_NO_GO.md',
    'VOICE_SKILL_HARNESS_RERUN_COMMANDS.md',
    'VOICE_SKILL_HARNESS_CHANGED_FILES.md'
)
$docTarget = Join-Path $stage '_codex_context'
New-Item -ItemType Directory -Force -Path $docTarget | Out-Null
foreach ($name in $docNames) {
    $text = Get-Content -Raw -Encoding UTF8 (Join-Path $RepoRoot "_codex_context\$name")
    $text = $text.Replace($env:USERPROFILE, '%USERPROFILE%')
    Set-Content -Encoding UTF8 -NoNewline -Path (Join-Path $docTarget $name) -Value $text
}

$manifest = [ordered]@{
    project = 'Voice-Controlled Skill Harness'
    phase = 'A_MINICPMO_BROWSER'
    date = $Date
    ready_for_phase_b = $false
    feature_default = 'off'
    excluded = @('model weights', 'raw audio/video', 'runs', 'credentials', 'absolute user paths')
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 (Join-Path $stage 'MANIFEST.json')

Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipPath -CompressionLevel Optimal
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
[pscustomobject]@{Zip=$zipPath; SHA256=$hash}
