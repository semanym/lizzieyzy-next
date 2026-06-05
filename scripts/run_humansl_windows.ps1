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

function Start-LoggedJob {
    param(
        [string]$Title,
        [string]$File,
        [string[]]$Arguments,
        [string]$LogPath
    )
    Write-Log "BEGIN background $Title"
    Write-Log "CMD $File $($Arguments -join ' ')"
    Write-Log "background_log=$LogPath"
    return Start-Job -Name $Title -ScriptBlock {
        param(
            [string]$RepoRoot,
            [string]$Title,
            [string]$File,
            [string[]]$Arguments,
            [string]$LogPath
        )
        Set-Location $RepoRoot
        $Started = Get-Date
        $Stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        "[$Stamp] BEGIN $Title" | Add-Content -Path $LogPath -Encoding UTF8
        "[$Stamp] CMD $File $($Arguments -join ' ')" | Add-Content -Path $LogPath -Encoding UTF8
        & $File @Arguments 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $Text = "$($_.Exception.Message)"
            } else {
                $Text = "$_"
            }
            Add-Content -Path $LogPath -Value $Text -Encoding UTF8
        }
        $ExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
        $Elapsed = [int]((Get-Date) - $Started).TotalSeconds
        $Stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        if ($ExitCode -eq 0) {
            "[$Stamp] END $Title elapsed=${Elapsed}s" | Add-Content -Path $LogPath -Encoding UTF8
        } else {
            "[$Stamp] FAIL $Title exit=$ExitCode elapsed=${Elapsed}s" | Add-Content -Path $LogPath -Encoding UTF8
        }
        [pscustomobject]@{
            ExitCode = $ExitCode
            LogPath = $LogPath
        }
    } -ArgumentList $RepoRoot, $Title, $File, $Arguments, $LogPath
}

function Wait-LoggedJob {
    param(
        [object]$Job,
        [string]$Title,
        [int]$MaxWaitSeconds = -1,
        [bool]$StopOnTimeout = $false
    )
    if ($null -eq $Job) {
        return
    }
    $Started = Get-Date
    $StoppedOnTimeout = $false
    while ($Job.State -eq "Running") {
        Wait-Job -Job $Job -Timeout 30 | Out-Null
        $Elapsed = [int]((Get-Date) - $Started).TotalSeconds
        if ($Job.State -eq "Running") {
            Write-Log "background $Title still running elapsed=${Elapsed}s"
            if ($MaxWaitSeconds -ge 0 -and $Elapsed -ge $MaxWaitSeconds) {
                if ($StopOnTimeout) {
                    Write-Log "stopping background $Title after ${Elapsed}s; continuing with SGFs fetched so far"
                    Stop-Job -Job $Job -ErrorAction SilentlyContinue
                    $StoppedOnTimeout = $true
                    break
                }
                Write-Log "leaving background $Title running after ${Elapsed}s"
                return
            }
        }
    }
    $Result = Receive-Job -Job $Job
    Remove-Job -Job $Job
    $ElapsedTotal = [int]((Get-Date) - $Started).TotalSeconds
    $ExitCode = 0
    if ($Result -and $null -ne $Result.ExitCode) {
        $ExitCode = [int]$Result.ExitCode
    } elseif ($StoppedOnTimeout) {
        $ExitCode = 0
    } elseif ($Job.State -ne "Completed") {
        $ExitCode = 1
    }
    if ($ExitCode -ne 0) {
        Write-Log "FAIL background $Title exit=$ExitCode elapsed=${ElapsedTotal}s"
        if ($Result -and $Result.LogPath) {
            Write-Log "background failure log: $($Result.LogPath)"
        }
        throw "$Title failed with exit code $ExitCode"
    }
    if ($StoppedOnTimeout) {
        Write-Log "STOPPED background $Title elapsed=${ElapsedTotal}s"
    } else {
        Write-Log "END background $Title elapsed=${ElapsedTotal}s"
    }
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

function New-PreflightSgf {
    param([string]$Path)
    $Dir = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    @"
(;FF[4]CA[UTF-8]GM[1]DT[2025-01-01]PB[HumanSL preflight black]PW[HumanSL preflight white]BR[2k]WR[2k]RE[B+R]SZ[19]KM[6.5]RU[Chinese]
;B[pd];W[dd];B[pp];W[dp];B[fq];W[cn];B[nq];W[fc];B[cf];W[qc];B[qd];W[pc];B[oc];W[od];B[nd];W[oe];B[pe];W[ne];B[md];W[of])
"@ | Set-Content -Path $Path -Encoding UTF8
}

function Get-SgfCount {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return 0
    }
    return @(Get-ChildItem -LiteralPath $Path -Recurse -Filter "*.sgf" -File).Count
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
$RefreshSgfs = Get-EnvOrDefault "REFRESH_SGFS" "0"
$OgsUrl = Normalize-ExternalValue (Get-EnvOrDefault "OGS_URL" $DefaultOgsUrl)
$OgsMinDate = Normalize-ExternalValue (Get-EnvOrDefault "OGS_MIN_DATE" "2025-01-01")
$OgsHttpRetries = [int](Get-EnvOrDefault "OGS_HTTP_RETRIES" "2")
$OgsRetryDelay = [int](Get-EnvOrDefault "OGS_RETRY_DELAY" "10")
$OgsTimeout = [int](Get-EnvOrDefault "OGS_TIMEOUT" "30")
$OgsApiFallback = Get-EnvOrDefault "OGS_API_FALLBACK" "1"
$OgsApiSleep = Get-EnvOrDefault "OGS_API_SLEEP" "0.5"
$OgsApiMaxRequests = Get-EnvOrDefault "OGS_API_MAX_REQUESTS" "250000"
$OgsApiProgressInterval = Get-EnvOrDefault "OGS_API_PROGRESS_INTERVAL" "25"
$IncrementalFirstBatch = [int](Get-EnvOrDefault "INCREMENTAL_FIRST_BATCH" "8")
$IncrementalMaxRequests = Get-EnvOrDefault "INCREMENTAL_OGS_API_MAX_REQUESTS" "1000"
$FetchAfterFirstBatchWaitSeconds = [int](Get-EnvOrDefault "FETCH_AFTER_FIRST_BATCH_WAIT_SECONDS" "60")
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
$Preflight = Get-EnvOrDefault "PREFLIGHT" "1"
$PreflightMaxVisits = [int](Get-EnvOrDefault "PREFLIGHT_MAX_VISITS" "4")
$PreflightTimeout = [int](Get-EnvOrDefault "PREFLIGHT_KATAGO_RESPONSE_TIMEOUT" "180")
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
$MoveEvaluationJsonl = Join-Path $Out "move-evaluation.jsonl"
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
Write-Log "ogs_http_retries=$OgsHttpRetries"
Write-Log "ogs_retry_delay=$OgsRetryDelay"
Write-Log "ogs_timeout=$OgsTimeout"
Write-Log "ogs_api_fallback=$OgsApiFallback"
Write-Log "ogs_api_sleep=$OgsApiSleep"
Write-Log "ogs_api_max_requests=$OgsApiMaxRequests"
Write-Log "ogs_api_progress_interval=$OgsApiProgressInterval"
Write-Log "incremental_first_batch=$IncrementalFirstBatch"
Write-Log "incremental_ogs_api_max_requests=$IncrementalMaxRequests"
Write-Log "fetch_after_first_batch_wait_seconds=$FetchAfterFirstBatchWaitSeconds"
Write-Log "out=$Out"
Write-Log "settings per_rank=$PerRank max_visits=$MaxVisits parallel_engines=$ParallelEngines max_moves=$MaxMoves min_moves=$MinMoves"

Invoke-Logged "KataGo version" $Katago @("version")
$KatagoVersionPath = Join-Path $Out "katago-version.txt"
& $Katago version 2>&1 | Set-Content -Path $KatagoVersionPath -Encoding UTF8
$KatagoVersion = (Get-Content -Path $KatagoVersionPath -TotalCount 1)
$ModelSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $Model).Hash.ToLowerInvariant()

if ($Preflight -eq "1") {
    $PreflightDir = Join-Path $Out "preflight"
    $PreflightSgfDir = Join-Path $PreflightDir "sgf\2k"
    $PreflightSgf = Join-Path $PreflightSgfDir "preflight-2k.sgf"
    $PreflightJsonl = Join-Path $PreflightDir "evaluation.jsonl"
    $PreflightMoveJsonl = Join-Path $PreflightDir "move-evaluation.jsonl"
    $PreflightBundle = Join-Path $PreflightDir "humansl-preflight.zip"
    $PreflightMerged = Join-Path $PreflightDir "merged"
    $PreflightAnalysis = Join-Path $PreflightDir "analysis"
    if (Test-Path -LiteralPath $PreflightDir -PathType Container) {
        Remove-Item -LiteralPath $PreflightDir -Recurse -Force
    }
    New-PreflightSgf -Path $PreflightSgf
    Write-Log "preflight enabled: running tiny end-to-end pipeline before full SGF fetch"
    Invoke-Logged "preflight HumanSL probe" "python" @(
        "scripts\probe_humansl_feasibility.py",
        "--katago", $Katago,
        "--config", $Config,
        "--model", $Model,
        "--human-model", $HumanModel,
        "--profiles", "rank_2k",
        "--repeats", "1",
        "--max-queries", "2",
        "--timeout", "$PreflightTimeout"
    )
    Invoke-Logged "preflight evaluate one SGF" "python" @(
        "scripts\evaluate_strength_samples.py", "$PreflightSgfDir\*.sgf",
        "--katago", $Katago,
        "--model", $Model,
        "--config", $Config,
        "--human-model", $HumanModel,
        "--human-profiles", "rank_2k",
        "--max-games", "1",
        "--min-moves", "0",
        "--max-moves", "20",
        "--max-visits", "$PreflightMaxVisits",
        "--human-max-visits", "$HumanMaxVisits",
        "--batch-positions", "4",
        "--human-batch-positions", "4",
        "--parallel-engines", "1",
        "--katago-response-timeout", "$PreflightTimeout",
        "--rules", $Rules,
        "--jsonl", $PreflightJsonl,
        "--move-jsonl", $PreflightMoveJsonl
    )
    Invoke-Logged "preflight package result" "python" @(
        "scripts\humansl_results.py", "package",
        "--evaluation-jsonl", $PreflightJsonl,
        "--move-jsonl", $PreflightMoveJsonl,
        "--out", $PreflightBundle,
        "--machine-id", "${MachineId}-preflight",
        "--operator", $Operator,
        "--katago-version", $KatagoVersion,
        "--katago-binary", $Katago,
        "--main-model-sha256", $ModelSha,
        "--profiles", "rank_2k",
        "--max-visits", "$PreflightMaxVisits",
        "--human-max-visits", "$HumanMaxVisits",
        "--rules", $Rules,
        "--run-log", $RunLog,
        "--sgf-dir", $PreflightSgfDir,
        "--note", "Preflight smoke test before full Windows HumanSL calibration."
    )
    Invoke-Logged "preflight validate result" "python" @("scripts\humansl_results.py", "validate", $PreflightBundle)
    Invoke-Logged "preflight merge result" "python" @("scripts\humansl_results.py", "merge", $PreflightBundle, "--out-dir", $PreflightMerged)
    Invoke-Logged "preflight analyze result" "python" @(
        "scripts\analyze_strength_calibration.py",
        (Join-Path $PreflightMerged "evaluation.jsonl"),
        "--out", $PreflightAnalysis,
        "--min-samples", "1",
        "--outlier-z", "3.5"
    )
    Write-Log "preflight completed successfully; starting full run"
} else {
    Write-Log "preflight disabled by PREFLIGHT=$Preflight"
}

if ($AutoFetchOpenSgfs -eq "1" -and $RefreshSgfs -eq "1" -and (Test-Path -LiteralPath $SgfByRankRoot -PathType Container)) {
    Write-Log "REFRESH_SGFS=1; removing existing SGF samples at $SgfByRankRoot"
    Remove-Item -LiteralPath $SgfByRankRoot -Recurse -Force
}

$FetchRemainingJob = $null

if ($AutoFetchOpenSgfs -eq "1") {
    $RemainingFetchArgs = @(
        "scripts\fetch_open_ranked_sgf_samples.py",
        "--out", $SgfByRankRoot,
        "--per-rank", "$PerRank",
        "--min-moves", "$MinMoves",
        "--ogs-url", $OgsUrl,
        "--ogs-min-date", $OgsMinDate,
        "--http-retries", "$OgsHttpRetries",
        "--retry-delay", "$OgsRetryDelay",
        "--timeout", "$OgsTimeout",
        "--ogs-api-sleep", "$OgsApiSleep",
        "--ogs-api-max-requests", "$OgsApiMaxRequests",
        "--ogs-api-progress-interval", "$OgsApiProgressInterval",
        "--ranks", $LabelRanks,
        "--append"
    )
    if ($OgsApiFallback -ne "1") { $RemainingFetchArgs += "--no-ogs-api-fallback" }
    if ($AllowPartialSgfs -eq "1") { $RemainingFetchArgs += "--allow-partial" }

    if ($IncrementalFirstBatch -gt 0) {
        $IncrementalRanks = "18k,17k,16k,15k,14k,13k,12k,11k,10k,9k,8k,7k,6k,5k,4k,3k,2k,1k,1d,2d,3d,4d,5d,6d,7d,8d,9d"
        $Args = @(
            "scripts\fetch_open_ranked_sgf_samples.py",
            "--out", $SgfByRankRoot,
            "--per-rank", "$PerRank",
            "--min-moves", "$MinMoves",
            "--ogs-url", $OgsUrl,
            "--ogs-min-date", $OgsMinDate,
            "--http-retries", "$OgsHttpRetries",
            "--retry-delay", "$OgsRetryDelay",
            "--timeout", "$OgsTimeout",
            "--ogs-api-sleep", "$OgsApiSleep",
            "--ogs-api-max-requests", "$IncrementalMaxRequests",
            "--ogs-api-progress-interval", "$OgsApiProgressInterval",
            "--ranks", $IncrementalRanks,
            "--append",
            "--prefer-ogs-api",
            "--stop-after-accepted", "$IncrementalFirstBatch",
            "--skip-jgdb",
            "--allow-partial"
        )
        if ($OgsApiFallback -ne "1") { $Args += "--no-ogs-api-fallback" }
        Invoke-Logged "fetch first incremental SGF batch" "python" $Args

        if ((Get-SgfCount $SgfByRankRoot) -gt 0) {
            Invoke-Logged "prepare first incremental SGF batch" "python" @(
                "scripts\prepare_ranked_sgf_samples.py",
                "--input-root", $SgfByRankRoot,
                "--out", $PreparedSgf,
                "--per-rank", "$PerRank",
                "--ranks", $LabelRanks,
                "--allow-partial"
            )
            if ((Get-SgfCount $PreparedSgf) -gt 0) {
                $FetchRemainingJob = Start-LoggedJob `
                    -Title "fetch remaining SGF samples" `
                    -File "python" `
                    -Arguments $RemainingFetchArgs `
                    -LogPath (Join-Path $Out "fetch-remaining.log")
                Invoke-Logged "evaluate first incremental SGF batch" "python" @(
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
                    "--jsonl", $EvaluationJsonl,
                    "--move-jsonl", $MoveEvaluationJsonl
                )
            } else {
                Write-Log "first incremental prepare produced no SGFs; continuing to full fetch"
            }
        } else {
            Write-Log "first incremental fetch produced no SGFs; continuing to full fetch"
        }
    }

    if ($null -eq $FetchRemainingJob) {
        Invoke-Logged "fetch remaining SGF samples" "python" $RemainingFetchArgs
    } else {
        Wait-LoggedJob `
            -Job $FetchRemainingJob `
            -Title "fetch remaining SGF samples" `
            -MaxWaitSeconds $FetchAfterFirstBatchWaitSeconds `
            -StopOnTimeout $true
    }
} elseif (-not (Test-Path -LiteralPath $SgfByRankRoot -PathType Container)) {
    throw "SGF_BY_RANK_ROOT not found and AUTO_FETCH_OPEN_SGFS is not 1: $SgfByRankRoot"
}

if ((Get-SgfCount $SgfByRankRoot) -le 0) {
    throw "No SGF samples were collected under $SgfByRankRoot"
}

if ($AutoFetchOpenSgfs -ne "1") {
    Invoke-Logged "prepare ranked SGFs" "python" @(
        "scripts\prepare_ranked_sgf_samples.py",
        "--input-root", $SgfByRankRoot,
        "--out", $PreparedSgf,
        "--per-rank", "$PerRank",
        "--ranks", $LabelRanks
    )
} else {
    Invoke-Logged "prepare final ranked SGFs" "python" @(
        "scripts\prepare_ranked_sgf_samples.py",
        "--input-root", $SgfByRankRoot,
        "--out", $PreparedSgf,
        "--per-rank", "$PerRank",
        "--ranks", $LabelRanks,
        "--allow-partial"
    )
}

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
    "--jsonl", $EvaluationJsonl,
    "--move-jsonl", $MoveEvaluationJsonl
)

Invoke-Logged "package result bundle" "python" @(
    "scripts\humansl_results.py", "package",
    "--evaluation-jsonl", $EvaluationJsonl,
    "--move-jsonl", $MoveEvaluationJsonl,
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
