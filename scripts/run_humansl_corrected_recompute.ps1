Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot
$env:PYTHONIOENCODING = "utf-8"

$Out = "target\humansl-gpu-run"
$Log = Join-Path $Out "humansl-corrected-recompute.log"
$CorrectedJsonl = Join-Path $Out "evaluation-humansl-corrected.jsonl"
$CorrectedMoveJsonl = Join-Path $Out "move-evaluation-humansl-corrected.jsonl"

$Katago = "D:\katago\LizzieYzy Next OpenCL\app\engines\katago\windows-x64\katago.exe"
$Model = "D:\katago\LizzieYzy Next OpenCL\app\weights\default.bin.gz"
$Config = "D:\katago\LizzieYzy Next OpenCL\app\engines\katago\configs\analysis.cfg"
$HumanModel = Join-Path $RepoRoot "human-sl-models\b18c384nbt-humanv0.bin.gz"
$Profiles = "rank_18k,rank_17k,rank_16k,rank_15k,rank_14k,rank_13k,rank_12k,rank_11k,rank_10k,rank_9k,rank_8k,rank_7k,rank_6k,rank_5k,rank_4k,rank_3k,rank_2k,rank_1k,rank_1d,rank_2d,rank_3d,rank_4d,rank_5d,rank_6d,rank_7d,rank_8d,rank_9d"

New-Item -ItemType Directory -Force -Path $Out | Out-Null

function Write-Log {
    param([string]$Message)
    $Line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Write-Host $Line
    Add-Content -Path $Log -Value $Line -Encoding UTF8
}

Write-Log "BEGIN corrected HumanSL-only recompute resume"

python scripts\recompute_humansl_from_move_jsonl.py `
    "target\humansl-gpu-run\prepared-sgf\**\*.sgf" `
    --move-jsonl "target\humansl-gpu-run\move-evaluation.jsonl" `
    --out-jsonl $CorrectedJsonl `
    --out-move-jsonl $CorrectedMoveJsonl `
    --katago $Katago `
    --model $Model `
    --config $Config `
    --human-model $HumanModel `
    --human-profiles $Profiles `
    --human-batch-positions 16 `
    --katago-response-timeout 1800 `
    --rules Chinese `
    --resume-jsonl 2>&1 | ForEach-Object {
        $Text = "$_"
        Write-Host $Text
        Add-Content -Path $Log -Value $Text -Encoding UTF8
    }

$ExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
if ($ExitCode -ne 0) {
    Write-Log "FAIL corrected HumanSL-only recompute resume exit=$ExitCode"
    exit $ExitCode
}

Write-Log "OK corrected HumanSL-only recompute resume"
