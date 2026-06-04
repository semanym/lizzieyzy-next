@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem One-click Windows HumanSL calibration run.
rem Run this file from the repository root on the GPU Windows machine.
rem Put SGFs under %SGF_BY_RANK_ROOT%\18k ... %SGF_BY_RANK_ROOT%\11d first.

set "KATAGO=D:\katago\LizzieYzy Next OpenCL\app\engines\katago\windows-x64\katago.exe"
set "MODEL=D:\katago\LizzieYzy Next OpenCL\app\weights\default.bin.gz"
set "CONFIG=D:\katago\LizzieYzy Next OpenCL\app\engines\katago\configs\analysis.cfg"
set "HUMAN_MODEL=%CD%\human-sl-models\b18c384nbt-humanv0.bin.gz"
set "SGF_BY_RANK_ROOT=%CD%\target\humansl-input\sgf-by-rank"
set "AUTO_FETCH_OPEN_SGFS=1"
set "ALLOW_PARTIAL_SGFS=0"
set "OUT=%CD%\target\humansl-gpu-run"
set "MACHINE_ID=windows-opencl-gpu"
set "OPERATOR=semanym"
set "PER_RANK=25"
set "MAX_VISITS=100"
set "HUMAN_MAX_VISITS=1"
set "PARALLEL_ENGINES=4"
set "MAX_MOVES=180"
set "MIN_MOVES=80"
set "BATCH_POSITIONS=16"
set "HUMAN_BATCH_POSITIONS=64"
set "KATAGO_RESPONSE_TIMEOUT=900"
set "RULES=Chinese"
set "PROFILES=rank_18k,rank_17k,rank_16k,rank_15k,rank_14k,rank_13k,rank_12k,rank_11k,rank_10k,rank_9k,rank_8k,rank_7k,rank_6k,rank_5k,rank_4k,rank_3k,rank_2k,rank_1k,rank_1d,rank_2d,rank_3d,rank_4d,rank_5d,rank_6d,rank_7d,rank_8d,rank_9d"
set "LABEL_RANKS=18k,17k,16k,15k,14k,13k,12k,11k,10k,9k,8k,7k,6k,5k,4k,3k,2k,1k,1d,2d,3d,4d,5d,6d,7d,8d,9d,10d,11d"

if not exist "%KATAGO%" (
  echo [error] KataGo not found: "%KATAGO%"
  exit /b 1
)
if not exist "%MODEL%" (
  echo [error] model not found: "%MODEL%"
  exit /b 1
)
if not exist "%CONFIG%" (
  echo [error] analysis config not found: "%CONFIG%"
  exit /b 1
)
if not exist "%HUMAN_MODEL%" (
  echo [error] HumanSL model not found: "%HUMAN_MODEL%"
  exit /b 1
)
if not exist "%OUT%" mkdir "%OUT%"
set "PREPARED_SGF=%OUT%\prepared-sgf"
set "RUN_LOG=%OUT%\run.log"
set "EVALUATION_JSONL=%OUT%\evaluation.jsonl"
set "MERGED_DIR=%OUT%\merged"
set "ANALYSIS_DIR=%OUT%\analysis"

echo [run] starting HumanSL calibration > "%RUN_LOG%"
echo [run] date %DATE% %TIME% >> "%RUN_LOG%"
echo [run] katago "%KATAGO%" >> "%RUN_LOG%"
echo [run] model "%MODEL%" >> "%RUN_LOG%"
echo [run] config "%CONFIG%" >> "%RUN_LOG%"
echo [run] human model "%HUMAN_MODEL%" >> "%RUN_LOG%"
echo [run] sgf root "%SGF_BY_RANK_ROOT%" >> "%RUN_LOG%"

if not exist "%SGF_BY_RANK_ROOT%" (
  if "%AUTO_FETCH_OPEN_SGFS%"=="1" (
    echo [step] fetching open SGF samples into "%SGF_BY_RANK_ROOT%"
    set "PARTIAL_FLAG="
    if "%ALLOW_PARTIAL_SGFS%"=="1" set "PARTIAL_FLAG=--allow-partial"
    python scripts\fetch_open_ranked_sgf_samples.py ^
      --out "%SGF_BY_RANK_ROOT%" ^
      --per-rank %PER_RANK% ^
      --min-moves %MIN_MOVES% ^
      --ranks "%LABEL_RANKS%" ^
      !PARTIAL_FLAG! >> "%RUN_LOG%" 2>&1
    if errorlevel 1 (
      echo [error] fetch_open_ranked_sgf_samples failed. See "%RUN_LOG%"
      echo If only 10d/11d are missing, provide those SGFs manually or set ALLOW_PARTIAL_SGFS=1.
      exit /b 1
    )
  ) else (
    echo [error] SGF_BY_RANK_ROOT not found: "%SGF_BY_RANK_ROOT%"
    echo Put open-source SGFs into rank subdirectories or set AUTO_FETCH_OPEN_SGFS=1.
    exit /b 1
  )
)

"%KATAGO%" version > "%OUT%\katago-version.txt" 2>&1
if errorlevel 1 (
  echo [error] katago version failed. See "%OUT%\katago-version.txt"
  exit /b 1
)
set /p KATAGO_VERSION=<"%OUT%\katago-version.txt"

for /f "tokens=1" %%H in ('certutil -hashfile "%MODEL%" SHA256 ^| findstr /R "^[0-9A-Fa-f][0-9A-Fa-f]*$"') do set "MODEL_SHA=%%H"

echo [step] preparing ranked SGFs
python scripts\prepare_ranked_sgf_samples.py ^
  --input-root "%SGF_BY_RANK_ROOT%" ^
  --out "%PREPARED_SGF%" ^
  --per-rank %PER_RANK% ^
  --ranks "%LABEL_RANKS%" >> "%RUN_LOG%" 2>&1
if errorlevel 1 (
  echo [error] prepare_ranked_sgf_samples failed. See "%RUN_LOG%"
  exit /b 1
)

echo [step] probing HumanSL support
python scripts\probe_humansl_feasibility.py ^
  --katago "%KATAGO%" ^
  --config "%CONFIG%" ^
  --model "%MODEL%" ^
  --human-model "%HUMAN_MODEL%" ^
  --profiles "%PROFILES%" ^
  --repeats 2 ^
  --max-queries 32 >> "%RUN_LOG%" 2>&1
if errorlevel 1 (
  echo [error] probe_humansl_feasibility failed. See "%RUN_LOG%"
  exit /b 1
)

echo [step] running KataGo %MAX_VISITS% visits, %PARALLEL_ENGINES% parallel engines
python scripts\evaluate_strength_samples.py "%PREPARED_SGF%\**\*.sgf" ^
  --katago "%KATAGO%" ^
  --model "%MODEL%" ^
  --config "%CONFIG%" ^
  --human-model "%HUMAN_MODEL%" ^
  --human-profiles "%PROFILES%" ^
  --max-games 100000 ^
  --min-moves %MIN_MOVES% ^
  --max-moves %MAX_MOVES% ^
  --max-visits %MAX_VISITS% ^
  --human-max-visits %HUMAN_MAX_VISITS% ^
  --batch-positions %BATCH_POSITIONS% ^
  --human-batch-positions %HUMAN_BATCH_POSITIONS% ^
  --parallel-engines %PARALLEL_ENGINES% ^
  --katago-response-timeout %KATAGO_RESPONSE_TIMEOUT% ^
  --rules %RULES% ^
  --resume-jsonl ^
  --jsonl "%EVALUATION_JSONL%" >> "%RUN_LOG%" 2>&1
if errorlevel 1 (
  echo [error] evaluate_strength_samples failed. See "%RUN_LOG%"
  exit /b 1
)

echo [step] packaging result bundle
python scripts\humansl_results.py package ^
  --evaluation-jsonl "%EVALUATION_JSONL%" ^
  --out "%OUT%\humansl-results-%MACHINE_ID%.zip" ^
  --machine-id "%MACHINE_ID%" ^
  --operator "%OPERATOR%" ^
  --katago-version "%KATAGO_VERSION%" ^
  --katago-binary "%KATAGO%" ^
  --main-model-sha256 "%MODEL_SHA%" ^
  --profiles "%PROFILES%" ^
  --max-visits %MAX_VISITS% ^
  --human-max-visits %HUMAN_MAX_VISITS% ^
  --rules %RULES% ^
  --run-log "%RUN_LOG%" ^
  --sgf-dir "%PREPARED_SGF%" ^
  --note "Windows OpenCL GPU run, 25 games per label rank, labels 18k-11d, HumanSL profiles 18k-9d." >> "%RUN_LOG%" 2>&1
if errorlevel 1 (
  echo [error] humansl_results package failed. See "%RUN_LOG%"
  exit /b 1
)

echo [step] validating, merging, and analyzing
python scripts\humansl_results.py validate "%OUT%\humansl-results-%MACHINE_ID%.zip" >> "%RUN_LOG%" 2>&1
if errorlevel 1 exit /b 1

python scripts\humansl_results.py merge "%OUT%\humansl-results-%MACHINE_ID%.zip" --out-dir "%MERGED_DIR%" >> "%RUN_LOG%" 2>&1
if errorlevel 1 exit /b 1

python scripts\analyze_strength_calibration.py "%MERGED_DIR%\evaluation.jsonl" --out "%ANALYSIS_DIR%" --min-samples 40 --outlier-z 3.5 >> "%RUN_LOG%" 2>&1
if errorlevel 1 exit /b 1

echo [ok] bundle: "%OUT%\humansl-results-%MACHINE_ID%.zip"
echo [ok] log: "%RUN_LOG%"
echo [ok] analysis: "%ANALYSIS_DIR%\analysis.md"
endlocal
