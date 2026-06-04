# HumanSL 跑谱结果格式与使用流程

本文定义外部机器跑 HumanSL 棋谱分析后的交换格式，以及结果同步、校验、合并和校准分析流程。

## 总体流程

1. 跑谱机器使用 `scripts/evaluate_strength_samples.py` 对 SGF 批量分析，输出 `evaluation.jsonl`。
2. 跑谱机器使用 `scripts/humansl_results.py package` 打成标准结果包。
3. 同步结果包 zip 给维护者，不直接提交大批量原始结果到仓库。
4. 维护者使用 `scripts/humansl_results.py validate` 校验每个包。
5. 维护者使用 `scripts/humansl_results.py merge` 合并多个包。
6. 合并后的 `evaluation.jsonl` 直接输入 `scripts/analyze_strength_calibration.py` 做校准分析。

## 标准结果包

文件名建议：

```text
humansl-results-YYYYMMDD-HHMM-{machine_id}.zip
```

zip 根目录必须包含：

```text
manifest.json
evaluation.jsonl
evaluation_summary_rows.csv
checksums.sha256
run.log
```

可选包含：

```text
sgf/
```

## manifest.json

`manifest.json` 描述一次跑谱任务的环境和模型版本，最低要求：

```json
{
  "schema_version": "lizzieyzy-human-sl-results-v1",
  "bundle_id": "runner-a-0d4c5e6f7a8b",
  "created_at": "2026-06-03T12:00:00Z",
  "machine_id": "runner-a",
  "operator": "name-or-account",
  "katago_version": "KataGo v1.15.0",
  "katago_binary": "/path/to/katago",
  "main_model_sha256": "normal-katago-model-sha256",
  "human_model": {
    "name": "b18c384nbt-humanv0.bin.gz",
    "sha256": "637746e44f0efe00ad1245a50aa9bbf0716efe364c43965ead97bd6835d84ab5",
    "bytes": 99066230
  },
  "profiles": ["rank_18k", "rank_17k", "rank_16k", "rank_15k", "rank_14k", "rank_13k", "rank_12k", "rank_11k", "rank_10k", "rank_9k", "rank_8k", "rank_7k", "rank_6k", "rank_5k", "rank_4k", "rank_3k", "rank_2k", "rank_1k", "rank_1d", "rank_2d", "rank_3d", "rank_4d", "rank_5d", "rank_6d", "rank_7d", "rank_8d", "rank_9d"],
  "max_visits": 32,
  "human_max_visits": 1,
  "rules": "Chinese",
  "row_count": 200,
  "human_sl_row_count": 200,
  "note": ""
}
```

## evaluation.jsonl

`evaluation.jsonl` 是主数据源，一行代表“一盘棋的一方”。它必须保持 JSON Lines 格式，即每行一个 JSON 对象。

必填基础字段：

```json
{
  "path": "/samples/game001.sgf",
  "side": "B",
  "player": "black-player",
  "fox_rank": "3d",
  "analyzed_moves": 80,
  "samples": 80
}
```

HumanSL 必填字段：

```json
{
  "human_sl_profiles": ["rank_18k", "rank_17k", "rank_16k", "rank_15k", "rank_14k", "rank_13k", "rank_12k", "rank_11k", "rank_10k", "rank_9k", "rank_8k", "rank_7k", "rank_6k", "rank_5k", "rank_4k", "rank_3k", "rank_2k", "rank_1k", "rank_1d", "rank_2d", "rank_3d", "rank_4d", "rank_5d", "rank_6d", "rank_7d", "rank_8d", "rank_9d"],
  "human_sl_sample_count": 2160,
  "human_sl_move_count": 80,
  "human_sl_anomalous_sample_count": 0,
  "human_sl_best_profile": "rank_3d",
  "human_sl_best_second_gap": 0.123,
  "human_sl_high_low_trend": 0.456,
  "human_sl_avg_logp_rank_18k": -4.8,
  "human_sl_avg_logp_rank_10k": -4.2,
  "human_sl_avg_logp_rank_5k": -3.9,
  "human_sl_avg_logp_rank_1k": -3.5,
  "human_sl_avg_logp_rank_1d": -3.2,
  "human_sl_avg_logp_rank_3d": -3.1,
  "human_sl_avg_logp_rank_5d": -3.3,
  "human_sl_avg_logp_rank_7d": -3.6,
  "human_sl_avg_logp_rank_9d": -3.8
}
```

推荐同时保留棋力评估字段：

```text
strength_band
quality_score
first_choice_rate
good_move_rate
match_rate
bad_move_rate
average_difficulty
weighted_point_loss
average_score_loss
average_score_equivalent_loss
median_score_loss
p75_score_equivalent_loss
p90_score_equivalent_loss
average_winrate_loss
mistake_rate
blunder_rate
```

## evaluation_summary_rows.csv

CSV 是给人工快速查看和表格软件使用的镜像数据，不作为主数据源。主数据源始终是 `evaluation.jsonl`。

要求：

- UTF-8 编码。
- 表头字段与 JSONL 字段一致或是其子集。
- 行数必须与 `evaluation.jsonl` 一致。
- 复杂字段如 list/dict 必须以 JSON 字符串写入单元格。

## run.log

`run.log` 用于复查，不参与校准。建议记录：

- 执行命令。
- 开始和结束时间。
- KataGo 版本。
- 普通模型路径和 SHA256。
- HumanSL 模型路径和 SHA256。
- 成功/失败棋谱数量。
- 超时、崩溃、重试信息。

## 生成结果包

先跑谱生成 JSONL：

```bash
python3 scripts/evaluate_strength_samples.py "samples/**/*.sgf" \
  --katago "/path/to/katago" \
  --model "/path/to/default.bin.gz" \
  --config "/path/to/analysis.cfg" \
  --human-model "human-sl-models/b18c384nbt-humanv0.bin.gz" \
  --human-profiles "rank_18k,rank_17k,rank_16k,rank_15k,rank_14k,rank_13k,rank_12k,rank_11k,rank_10k,rank_9k,rank_8k,rank_7k,rank_6k,rank_5k,rank_4k,rank_3k,rank_2k,rank_1k,rank_1d,rank_2d,rank_3d,rank_4d,rank_5d,rank_6d,rank_7d,rank_8d,rank_9d" \
  --jsonl target/humansl-run/evaluation.jsonl
```

再打包：

```bash
python3 scripts/humansl_results.py package \
  --evaluation-jsonl target/humansl-run/evaluation.jsonl \
  --out target/humansl-results-20260603-1200-runner-a.zip \
  --machine-id runner-a \
  --operator "your-name" \
  --katago-version "KataGo v1.15.0" \
  --katago-binary "/path/to/katago" \
  --main-model-sha256 "<normal-model-sha256>" \
  --profiles "rank_18k,rank_17k,rank_16k,rank_15k,rank_14k,rank_13k,rank_12k,rank_11k,rank_10k,rank_9k,rank_8k,rank_7k,rank_6k,rank_5k,rank_4k,rank_3k,rank_2k,rank_1k,rank_1d,rank_2d,rank_3d,rank_4d,rank_5d,rank_6d,rank_7d,rank_8d,rank_9d" \
  --max-visits 32 \
  --human-max-visits 1 \
  --rules Chinese \
  --run-log target/humansl-run/run.log
```

## 校验结果包

```bash
python3 scripts/humansl_results.py validate target/humansl-results-*.zip
```

校验内容包括：

- `manifest.json` schema 和 HumanSL 模型信息。
- `checksums.sha256` 完整性。
- `evaluation.jsonl` 每行字段。
- `evaluation_summary_rows.csv` 行数。
- 每行必须存在 HumanSL 样本。

## 合并并用于校准

```bash
python3 scripts/humansl_results.py merge target/humansl-results-*.zip \
  --out-dir target/humansl-merged
```

合并后会生成：

```text
target/humansl-merged/manifest.json
target/humansl-merged/evaluation.jsonl
target/humansl-merged/evaluation_summary_rows.csv
target/humansl-merged/checksums.sha256
```

然后直接做校准分析：

```bash
python3 scripts/analyze_strength_calibration.py \
  target/humansl-merged/evaluation.jsonl \
  --out target/humansl-merged/calibration-analysis
```

关键输出：

```text
calibration_rows.csv
metric_correlations.csv
metric_distribution_by_exact_rank.csv
humansl_linear_calibration_summary.csv
humansl_linear_calibration_coefficients.csv
analysis.md
```

## 同步方式

推荐同步 zip 包，不同步散文件：

```text
humansl-results-YYYYMMDD-HHMM-{machine_id}.zip
```

同步时同时提供：

```text
文件名
文件大小
SHA256
跑谱机器说明
棋谱来源说明
```

如果走 GitHub，不建议把大结果包直接提交到仓库。推荐在 Discussion 中贴：

- 结果包下载链接。
- 结果包 SHA256。
- `manifest.json` 摘要。
- 是否包含原始 SGF。
