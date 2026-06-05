Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$Katago = $env:KATAGO
if ([string]::IsNullOrWhiteSpace($Katago)) {
    $Katago = "D:\katago\LizzieYzy Next OpenCL\app\engines\katago\windows-x64\katago.exe"
}
$Model = $env:MODEL
if ([string]::IsNullOrWhiteSpace($Model)) {
    $Model = "D:\katago\LizzieYzy Next OpenCL\app\weights\default.bin.gz"
}
$Config = $env:CONFIG
if ([string]::IsNullOrWhiteSpace($Config)) {
    $Config = "D:\katago\LizzieYzy Next OpenCL\app\engines\katago\configs\analysis.cfg"
}

$Out = "target\humansl-gpu-run"
$SgfByRank = "target\humansl-input\sgf-by-rank"
$Prepared = Join-Path $Out "prepared-sgf"
$Jsonl = Join-Path $Out "evaluation.jsonl"
$Log = Join-Path $Out "batch-evaluate-existing.log"
$LabelRanks = "18k,17k,16k,15k,14k,13k,12k,11k,10k,9k,8k,7k,6k,5k,4k,3k,2k,1k,1d,2d,3d,4d,5d,6d,7d,8d,9d,10d,11d"
$Profiles = "rank_18k,rank_17k,rank_16k,rank_15k,rank_14k,rank_13k,rank_12k,rank_11k,rank_10k,rank_9k,rank_8k,rank_7k,rank_6k,rank_5k,rank_4k,rank_3k,rank_2k,rank_1k,rank_1d,rank_2d,rank_3d,rank_4d,rank_5d,rank_6d,rank_7d,rank_8d,rank_9d"

New-Item -ItemType Directory -Force -Path $Out | Out-Null

function Write-Log {
    param([string]$Message)
    $Line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
    Write-Host $Line
    Add-Content -Path $Log -Value $Line -Encoding UTF8
}

function Invoke-Logged {
    param([string]$Title, [string]$File, [string[]]$Arguments)
    Write-Log "BEGIN $Title"
    Write-Log "CMD $File $($Arguments -join ' ')"
    $OldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $File @Arguments 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $Text = "$($_.Exception.Message)"
            } else {
                $Text = "$_"
            }
            Write-Host $Text
            Add-Content -Path $Log -Value $Text -Encoding UTF8
        }
        $ExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    } finally {
        $ErrorActionPreference = $OldErrorActionPreference
    }
    if ($ExitCode -ne 0) {
        Write-Log "FAIL $Title exit=$ExitCode"
        exit $ExitCode
    }
    Write-Log "END $Title"
}

"[run] existing SGF batch evaluation" | Set-Content -Path $Log -Encoding UTF8

Invoke-Logged "prepare existing ranked SGFs" "python" @(
    "scripts\prepare_ranked_sgf_samples.py",
    "--input-root", $SgfByRank,
    "--out", $Prepared,
    "--per-rank", "25",
    "--ranks", $LabelRanks,
    "--allow-partial"
)

Invoke-Logged "evaluate existing ranked SGFs" "python" @(
    "scripts\evaluate_strength_samples.py", "$Prepared\**\*.sgf",
    "--katago", $Katago,
    "--model", $Model,
    "--config", $Config,
    "--human-model", "human-sl-models\b18c384nbt-humanv0.bin.gz",
    "--human-profiles", $Profiles,
    "--max-games", "100000",
    "--min-moves", "80",
    "--max-moves", "180",
    "--max-visits", "100",
    "--human-max-visits", "1",
    "--batch-positions", "16",
    "--human-batch-positions", "16",
    "--parallel-engines", "2",
    "--katago-response-timeout", "1800",
    "--rules", "Chinese",
    "--resume-jsonl",
    "--jsonl", $Jsonl
)

Write-Log "OK existing batch evaluation complete"
