Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Get-EnvOrDefault {
    param([string]$Name, [string]$Default)
    $Value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $Default }
    return $Value
}

function Normalize-ExternalValue {
    param([string]$Value)
    return ("$Value" -replace '^[\s`''"]+', '' -replace '[\s`''"]+$', '')
}

function Write-Log {
    param([string]$Message)
    $Stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $Line = "[$Stamp] $Message"
    Write-Host $Line
    Add-Content -Path $script:RunLog -Value $Line -Encoding UTF8
}

function Invoke-Logged {
    param(
        [string]$Title,
        [string]$File,
        [string[]]$Arguments
    )
    Write-Log "BEGIN $Title"
    Write-Log "CMD $File $($Arguments -join ' ')"
    $Started = Get-Date
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
            Add-Content -Path $script:RunLog -Value $Text -Encoding UTF8
        }
        $ExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    } finally {
        $ErrorActionPreference = $OldErrorActionPreference
    }
    $Elapsed = [int]((Get-Date) - $Started).TotalSeconds
    if ($ExitCode -ne 0) {
        Write-Log "FAIL $Title exit=$ExitCode elapsed=${Elapsed}s"
        throw "$Title failed with exit code $ExitCode"
    }
    Write-Log "END $Title elapsed=${Elapsed}s"
}

function Test-RequiredFile {
    param([string]$Label, [string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
}

function New-Sha256File {
    param([string]$Path, [string]$OutPath)
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    "$Hash  $(Split-Path -Leaf $Path)" | Set-Content -Path $OutPath -Encoding ASCII
    return $Hash
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot
$DefaultOgsUrl = "https://za3k.com/ogs/ogs_games_2013_to_2025-05/sgfs-by-date.tar.gz"

$Katago = Normalize-ExternalValue (Get-EnvOrDefault "KATAGO" "D:\katago\LizzieYzy Next OpenCL\app\engines\katago\windows-x64\katago.exe")
$Model = Normalize-ExternalValue (Get-EnvOrDefault "MODEL" "D:\katago\LizzieYzy Next OpenCL\app\weights\default.bin.gz")
$Config = Normalize-ExternalValue (Get-EnvOrDefault "CONFIG" "D:\katago\LizzieYzy Next OpenCL\app\engines\katago\configs\analysis.cfg")
$HumanModel = Normalize-ExternalValue (Get-EnvOrDefault "HUMAN_MODEL" (Join-Path $RepoRoot "human-sl-models\b18c384nbt-humanv0.bin.gz"))
$SgfByRankRoot = Normalize-ExternalValue (Get-EnvOrDefault "SGF_BY_RANK_ROOT" (Join-Path $RepoRoot "target\humansl-input\sgf-by-rank"))
$AutoFetchOpenSgfs = Get-EnvOrDefault "AUTO_FETCH_OPEN_SGFS" "1"
$RefreshSgfs = Get-EnvOrDefault "REFRESH_SGFS" "1"
$OgsUrl = Normalize-ExternalValue (Get-EnvOrDefault "OGS_URL" $DefaultOgsUrl)
$OgsMinDate = Normalize-ExternalValue (Get-EnvOrDefault "OGS_MIN_DATE" "2025-01-01")
$AllowPartialSgfs = Get-EnvOrDefault "ALLOW_PARTIAL_SGFS" "0"
$Out = Normalize-ExternalValue (Get-EnvOrDefault "OUT" (Join-Path $RepoRoot "target\humansl-gpu-run"))
$MachineId = Get-EnvOrDefault "MACHINE_ID" "windows-opencl-gpu"
$Operator = Get-EnvOrDefault "OPERATOR" "semanym"
$PerRank = [int](Get-EnvOrDefault "PER_RANK" "25")
$MaxVisits = [int](Get-EnvOrDefault "MAX_VISITS" "100")
$HumanMaxVisits = [int](Get-EnvOrDefault "HUMAN_MAX_VISITS" "1")
$ParallelEngines = [int](Get-EnvOrDefault "PARALLEL_ENGINES" "4")
$MaxMoves = [int](Get-EnvOrDefault "MAX_MOVES" "180")
$MinMoves = [int](Get-EnvOrDefault "MIN_MOVES" "80")
$BatchPositions = [int](Get-EnvOrDefault "BATCH_POSITIONS" "16")
$HumanBatchPositions = [int](Get-EnvOrDefault "HUMAN_BATCH_POSITIONS" "64")
$KatagoResponseTimeout = [int](Get-EnvOrDefault "KATAGO_RESPONSE_TIMEOUT" "900")
$Rules = Get-EnvOrDefault "RULES" "Chinese"
$PushResults = Get-EnvOrDefault "PUSH_RESULTS" "1"
$PushRemote = Get-EnvOrDefault "PUSH_REMOTE" "origin"
$PushRef = Get-EnvOrDefault "PUSH_REF" "main:main"
$Profiles = Get-EnvOrDefault "PROFILES" "rank_18k,rank_17k,rank_16k,rank_15k,rank_14k,rank_13k,rank_12k,rank_11k,rank_10k,rank_9k,rank_8k,rank_7k,rank_6k,rank_5k,rank_4k,rank_3k,rank_2k,rank_1k,rank_1d,rank_2d,rank_3d,rank_4d,rank_5d,rank_6d,rank_7d,rank_8d,rank_9d"
$LabelRanks = Get-EnvOrDefault "LABEL_RANKS" "18k,17k,16k,15k,14k,13k,12k,11k,10k,9k,8k,7k,6k,5k,4k,3k,2k,1k,1d,2d,3d,4d,5d,6d,7d,8d,9d,10d,11d"

Test-RequiredFile "KataGo" $Katago
Test-RequiredFile "model" $Model
Test-RequiredFile "analysis config" $Config
Test-RequiredFile "HumanSL model" $HumanModel

New-Item -ItemType Directory -Force -Path $Out | Out-Null
$script:RunLog = Join-Path $Out "run.log"
$PreparedSgf = Join-Path $Out "prepared-sgf"
$EvaluationJsonl = Join-Path $Out "evaluation.jsonl"
$MergedDir = Join-Path $Out "merged"
$AnalysisDir = Join-Path $Out "analysis"
$BundlePath = Join-Path $Out "humansl-results-$MachineId.zip"
$RunId = Get-Date -Format "yyyyMMdd-HHmmss"
$SyncDir = Join-Path $RepoRoot "humansl-run-results\$RunId-$MachineId"

"[run] starting HumanSL calibration" | Set-Content -Path $RunLog -Encoding UTF8
if ($OgsUrl -notmatch "^https?://") {
    Write-Log "warning: ignoring invalid OGS_URL '$OgsUrl'; using default $DefaultOgsUrl"
    $OgsUrl = $DefaultOgsUrl
}
Write-Log "repo=$RepoRoot"
Write-Log "katago=$Katago"
Write-Log "model=$Model"
Write-Log "config=$Config"
Write-Log "human_model=$HumanModel"
Write-Log "sgf_by_rank_root=$SgfByRankRoot"
Write-Log "ogs_url=$OgsUrl"
Write-Log "ogs_min_date=$OgsMinDate"
Write-Log "out=$Out"
Write-Log "settings per_rank=$PerRank max_visits=$MaxVisits parallel_engines=$ParallelEngines max_moves=$MaxMoves min_moves=$MinMoves"

if ($AutoFetchOpenSgfs -eq "1" -and $RefreshSgfs -eq "1" -and (Test-Path -LiteralPath $SgfByRankRoot -PathType Container)) {
    Write-Log "REFRESH_SGFS=1; removing existing SGF samples at $SgfByRankRoot"
    Remove-Item -LiteralPath $SgfByRankRoot -Recurse -Force
}

if (-not (Test-Path -LiteralPath $SgfByRankRoot -PathType Container)) {
    if ($AutoFetchOpenSgfs -eq "1") {
        $Args = @(
            "scripts\fetch_open_ranked_sgf_samples.py",
            "--out", $SgfByRankRoot,
            "--per-rank", "$PerRank",
            "--min-moves", "$MinMoves",
            "--ogs-url", $OgsUrl,
            "--ogs-min-date", $OgsMinDate,
            "--ranks", $LabelRanks
        )
        if ($AllowPartialSgfs -eq "1") { $Args += "--allow-partial" }
        Invoke-Logged "fetch open SGF samples" "python" $Args
    } else {
        throw "SGF_BY_RANK_ROOT not found and AUTO_FETCH_OPEN_SGFS is not 1: $SgfByRankRoot"
    }
}

Invoke-Logged "KataGo version" $Katago @("version")
$KatagoVersionPath = Join-Path $Out "katago-version.txt"
& $Katago version 2>&1 | Set-Content -Path $KatagoVersionPath -Encoding UTF8
$KatagoVersion = (Get-Content -Path $KatagoVersionPath -TotalCount 1)
$ModelSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $Model).Hash.ToLowerInvariant()

Invoke-Logged "prepare ranked SGFs" "python" @(
    "scripts\prepare_ranked_sgf_samples.py",
    "--input-root", $SgfByRankRoot,
    "--out", $PreparedSgf,
    "--per-rank", "$PerRank",
    "--ranks", $LabelRanks
)

Invoke-Logged "probe HumanSL support" "python" @(
    "scripts\probe_humansl_feasibility.py",
    "--katago", $Katago,
    "--config", $Config,
    "--model", $Model,
    "--human-model", $HumanModel,
    "--profiles", $Profiles,
    "--repeats", "2",
    "--max-queries", "32"
)

Invoke-Logged "evaluate SGFs with KataGo and HumanSL" "python" @(
    "scripts\evaluate_strength_samples.py", "$PreparedSgf\**\*.sgf",
    "--katago", $Katago,
    "--model", $Model,
    "--config", $Config,
    "--human-model", $HumanModel,
    "--human-profiles", $Profiles,
    "--max-games", "100000",
    "--min-moves", "$MinMoves",
    "--max-moves", "$MaxMoves",
    "--max-visits", "$MaxVisits",
    "--human-max-visits", "$HumanMaxVisits",
    "--batch-positions", "$BatchPositions",
    "--human-batch-positions", "$HumanBatchPositions",
    "--parallel-engines", "$ParallelEngines",
    "--katago-response-timeout", "$KatagoResponseTimeout",
    "--rules", $Rules,
    "--resume-jsonl",
    "--jsonl", $EvaluationJsonl
)

Invoke-Logged "package result bundle" "python" @(
    "scripts\humansl_results.py", "package",
    "--evaluation-jsonl", $EvaluationJsonl,
    "--out", $BundlePath,
    "--machine-id", $MachineId,
    "--operator", $Operator,
    "--katago-version", $KatagoVersion,
    "--katago-binary", $Katago,
    "--main-model-sha256", $ModelSha,
    "--profiles", $Profiles,
    "--max-visits", "$MaxVisits",
    "--human-max-visits", "$HumanMaxVisits",
    "--rules", $Rules,
    "--run-log", $RunLog,
    "--sgf-dir", $PreparedSgf,
    "--note", "Windows OpenCL GPU run, 25 games per label rank, labels 18k-11d, HumanSL profiles 18k-9d."
)

Invoke-Logged "validate result bundle" "python" @("scripts\humansl_results.py", "validate", $BundlePath)
Invoke-Logged "merge result bundle" "python" @("scripts\humansl_results.py", "merge", $BundlePath, "--out-dir", $MergedDir)
Invoke-Logged "analyze calibration output" "python" @(
    "scripts\analyze_strength_calibration.py",
    (Join-Path $MergedDir "evaluation.jsonl"),
    "--out", $AnalysisDir,
    "--min-samples", "40",
    "--outlier-z", "3.5"
)

Write-Log "copying result artifacts to $SyncDir"
New-Item -ItemType Directory -Force -Path $SyncDir | Out-Null
Copy-Item -LiteralPath $BundlePath -Destination $SyncDir -Force
Copy-Item -LiteralPath $RunLog -Destination $SyncDir -Force
Copy-Item -LiteralPath $KatagoVersionPath -Destination $SyncDir -Force
if (Test-Path -LiteralPath (Join-Path $AnalysisDir "analysis.md")) {
    Copy-Item -LiteralPath (Join-Path $AnalysisDir "analysis.md") -Destination $SyncDir -Force
}
New-Sha256File -Path (Join-Path $SyncDir (Split-Path -Leaf $BundlePath)) -OutPath (Join-Path $SyncDir "checksums.sha256") | Out-Null
@"
run_id=$RunId
machine_id=$MachineId
operator=$Operator
max_visits=$MaxVisits
parallel_engines=$ParallelEngines
per_rank=$PerRank
label_ranks=$LabelRanks
human_sl_profiles=$Profiles
bundle=$(Split-Path -Leaf $BundlePath)
"@ | Set-Content -Path (Join-Path $SyncDir "summary.txt") -Encoding UTF8

if ($PushResults -eq "1") {
    Invoke-Logged "git add result artifacts" "git" @("add", "--", $SyncDir)
    Invoke-Logged "git commit result artifacts" "git" @("commit", "-m", "Add HumanSL GPU run results $RunId")
    Invoke-Logged "git push result artifacts" "git" @("push", $PushRemote, $PushRef)
} else {
    Write-Log "PUSH_RESULTS is not 1; result artifacts were not committed or pushed."
}

Write-Log "OK bundle=$BundlePath"
Write-Log "OK sync_dir=$SyncDir"
Write-Log "OK analysis=$(Join-Path $AnalysisDir "analysis.md")"
