# Aurora × OpenRCA: 5G RAN Root-Cause Analysis Benchmark

This document describes how the **Aurora** 5G RAN fault-injection dataset was adapted to the
[OpenRCA](https://github.com/microsoft/OpenRCA) benchmark framework, how the RCA-agent baseline was
run against it with a locally-served open-source LLM, and what the results show. It covers the full
pipeline: dataset conversion → leakage sanitization → log clock alignment → agent integration →
baseline evaluation → LLM-as-judge failure post-mortem.

---

## 1. The Aurora dataset

Aurora is a fault-injection telemetry campaign collected on a containerized 5G RAN
(OpenAirInterface, F1 split) deployed on Kubernetes:

- **Components**: one CU, two DUs (`du0`, `du1`), F1 links CU↔DU0 / CU↔DU1, shared network, with
  UE groups attached per DU via rfsimulator.
- **Runs**: 136 total — 13 fault-free baselines and 123 fault runs. Each fault run is ~10 minutes:
  a ~5-min `Normal` phase followed by a ~5-min fault phase with precisely recorded injection windows.
- **Fault types**: 21 anomalies in 3 categories:
  - *Hardware*: `PortFlap_CU_du{0,1}`, `LinkFailure_CU_du{0,1}`
  - *Infrastructure*: `CPU_Contention_{cu,du0,du1}`, `Memory_Contention_{cu,du0,du1}`, `Network_Contention`
  - *Application*: `L1_Contention_du{0,1}`, `MAC_Contention_du{0,1}`, `PDCP_Contention_cu`,
    `Memory_Leak_{cu,du0,du1}`, `QueueSize_Tuning_du{0,1}`
- **Telemetry per run**:
  - Wide-format metric CSVs at ~100 ms sampling: `cu_*` (~213 columns), `du_du0_*`/`du_du1_*`
    (~618 columns incl. per-thread scheduling stats). Timestamps in epoch microseconds.
  - Eight OpenAirInterface protocol logs (`F1AP`, `NGAP`, `RRC`, `PDCP`, `RLC`, `MAC`, `PHY`, `HW`).
  - `topology.jsonl`: deployment snapshots (pods, resource limits, IPs, F1 link inventory).
- **Ground truth**: `ground_truth.csv` — per run: anomaly name, category, affected component, and
  the fault phase's start/end timestamps.

All timestamps are in **Asia/Kolkata (IST, UTC+5:30)**.

## 2. Conversion to OpenRCA format (`main/adapt_aurora.py`)

```bash
python -m main.adapt_aurora --src /path/to/aurora_output
```

Produces `dataset/Aurora/`:

| Artifact | Content |
|---|---|
| `record.csv` | One row per fault run (OpenRCA schema: `level, component, timestamp, datetime, reason` + provenance columns). `reason` = the anomaly label; `datetime` = fault-phase start. |
| `query.csv` | One benchmark query per fault run: `task_index` (randomly drawn from OpenRCA's 7 task templates, seed 42), a natural-language `instruction`, and machine-checkable `scoring_points`. |
| `telemetry/run_N/{metric,log,topology}/` | The agent-visible telemetry (see sanitization below). |

**122 queries** result (one fault run had no telemetry on disk and was dropped; the 13 fault-free
baseline runs are excluded from queries since OpenRCA has no "no failure" task type, but their
telemetry is linked for reference).

### 2.1 Ground-truth leakage sanitization

The raw campaign output leaks its own answers; the adapter removes all of it:

1. **Folder names**: source telemetry lives under anomaly-named directories
   (`aurora_output/PortFlap_CU_du1/run_2/…`). Metric files are therefore **hardlinked** (not
   symlinked) into neutral `telemetry/run_N/` paths — a hardlink exposes no target path to
   `os.path.realpath()`.
2. **`topology.jsonl`**: the fault injector wrote its own actions into this file (`"anomaly":`,
   `"category":`, `"component":` fields and `anomaly_start`/`anomaly_end` events — literally the
   answer sheet). Sanitized copies keep only the `run_start`/`run_end` deployment snapshots with
   those fields stripped. Consequence: agents must infer link failures from logs/metrics, as in
   production.
3. Verified by sweeping every agent-visible file for all 21 anomaly strings: zero hits.

### 2.2 Log clock alignment

OAI logs carry `CLOCK_MONOTONIC` timestamps (seconds since host boot) with no epoch anchor anywhere
in the data — unusable against epoch-stamped metrics and query time ranges. The adapter recovers the
boot epoch from causality: in every link-fault run, the first fault-related SCTP teardown in
`F1AP.log` must occur *after* that run's first injection event, giving a per-run lower bound on the
offset. 23 runs across both campaign days yield consistent bounds (single host boot, constant
offset); the tightest bound is taken, with residual error ≈ **−0/+3 s** (vs. the benchmark's ±60 s
scoring tolerance). Each log line gets a wall-clock prefix:

```
2026-07-21 09:02:58.845 | 1007666.550351 [F1AP]   I Received SCTP state 1 ... removing endpoint
```

plus a per-run `log/TIMEBASE.txt` documenting the conversion (worded neutrally — no ground-truth
hints). Known limitation: the original log writer occasionally glued two records into one line;
embedded records keep only their raw monotonic stamp.

### 2.3 Design compromises vs. original OpenRCA (kept deliberately, documented honestly)

1. **Templated instructions** — OpenRCA paraphrases queries with an LLM; ours are deterministic
   templates (the repo's generator had a broken import and we avoided an extra LLM dependency).
2. **Location-embedded reason labels** — Aurora reasons include the component
   (`CPU_Contention_du0`), unlike OpenRCA's location-free reasons ("high memory usage"). With
   exact-match scoring this **double-penalizes** localization errors (wrong DU ⇒ both component
   *and* reason scored 0). See §5 for the measured impact.
3. **Single failure per query, ~10-min windows** — runs are isolated; OpenRCA's multi-failure
   disambiguation axis is not exercised. (OpenRCA's 30-min bucketing would falsely merge unrelated
   back-to-back runs and was bypassed.)
4. **No distributed traces** — cross-component reasoning relies on F1 counters on both ends,
   `F1AP.log` SCTP events, and topology snapshots.

## 3. Agent integration

- **`rca/baseline/rca_agent/prompt/basic_prompt_Aurora.py`** — the dataset briefing (schema of the
  wide metric CSVs by column family, log format incl. the TIMEBASE caveat, topology schema, IST
  timezone, RAN domain context, and the exact candidate lists for components/reasons that
  exact-match scoring requires).
- **`rca/run_agent_standard.py`** — added the `--dataset Aurora` branch.
- **`rca/baseline/rca_agent/executor.py`** — timezone rule generalized (was hardcoded UTC+8; now
  defers to the dataset briefing).
- **`rca/api_router.py` + `config.json`** — model entry selectable via `API_MODEL_KEY` env var or a
  `"default"` field; configured against a self-hosted **Ollama** server through its
  OpenAI-compatible `/v1` endpoint. Serving notes: models must fit fully in VRAM
  (`/api/ps`: `size_vram == size`) — a 70B spilling 37 GB to CPU ran at 0.02 tok/s vs 30 tok/s
  fully resident with a reduced `num_ctx`.

## 4. Setup & running

### 4.1 Prerequisites

- **Python ≥ 3.10**
- **An OpenAI-compatible LLM endpoint.** Any server exposing `/v1/chat/completions` works. For
  self-hosting, [Ollama](https://ollama.com) is what these results used (`qwen3:32b`); a frontier
  API (OpenAI/Anthropic-via-proxy/etc.) can be dropped in via config alone.
- **The Aurora source data** (`aurora_output/`): the raw fault-injection campaign output
  (per-anomaly run folders + `ground_truth.csv`). It is not distributed with this repository.
- ~6 GB free disk on the same filesystem as the source data (telemetry is hardlinked, not copied).

### 4.2 Environment

```bash
git clone https://github.com/IP-Michael/OpenRCA.git && cd OpenRCA
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

(`requirements.txt` pins `numpy<2` — the pinned pandas 1.5.3 is binary-incompatible with NumPy 2 —
and `httpx<0.28`, required by the pinned `openai` client.)

### 4.3 LLM endpoint configuration

```bash
cp config.json.example config.json   # then edit
```

`config.json` (gitignored — never commit real endpoints/keys) maps named model entries to
endpoints; the `"default"` field selects one, and `API_MODEL_KEY=<entry-name>` overrides it per
run. For Ollama use `"base_url": "http://<server>:11434/v1"` and any non-empty `api_key`.

**Ollama sizing note**: verify the model is fully GPU-resident before long runs —
`curl <server>:11434/api/ps` must show `size_vram == size`. A model that silently spills to CPU
RAM runs ~1000× slower (measured: 0.02 tok/s spilled vs 30 tok/s resident for a 70B; fix by
reducing `num_ctx` via a derived model so weights + KV cache fit in VRAM).

### 4.4 Build the benchmark dataset

```bash
.venv/bin/python3 -m main.adapt_aurora --src /path/to/aurora_output
```

Generates `dataset/Aurora/` (record.csv, query.csv, sanitized telemetry) as described in §2.
Deterministic (seed 42) — safe to delete and regenerate. Always run all commands from the repo
root: the code uses relative `dataset/…` and `test/…` paths.

### 4.5 Run the RCA-agent baseline

```bash
.venv/bin/python3 -m rca.run_agent_standard --dataset Aurora \
    --timeout 1800 --end_idx 121 --tag mybaseline
```

- `--timeout 1800` (seconds per query) is sized for slow self-hosted models; the default 600 suits
  fast APIs.
- Resumable: results append to `test/result/Aurora/agent-<tag>-<model>.csv`; after an interruption,
  continue with `--start_idx <last row_id + 1>`.
- Expect ~5–15 min/query on a 32B thinking model (~31 h for all 122 queries in our run).
- Per-query artifacts: `test/monitor/Aurora/agent-<tag>-<model>/<timestamp>/`
  (`history/*.log` play-by-play, `trajectory/*.ipynb` executor code, `prompt/*.json` full
  conversations).

### 4.6 Score

```bash
.venv/bin/python3 -m main.evaluate \
    -p "test/result/Aurora/agent-<tag>-<model>.csv" \
    -q dataset/Aurora/query.csv -r test/aurora_report.csv
```

Prints the strict/partial accuracy tables (per difficulty tier) and writes the per-query report.

### 4.7 LLM-as-judge post-mortem (optional)

```bash
.venv/bin/python3 -m main.diagnose \
    --result "test/result/Aurora/agent-<tag>-<model>.csv" \
    --monitor "test/monitor/Aurora/agent-<tag>-<model>" \
    --query dataset/Aurora/query.csv --out test/analysis/diagnosis.csv
```

Judges every trajectory (resumable; ~2–3 min/query). `--report-only` reprints aggregates from an
existing output; `--sample N` judges only the first N rows for a quality check. Failure-code
definitions live in `main/diagnose.py` (`FAILURE_CODES` / `SUCCESS_CODES`) and are injected into
the judge prompt verbatim.

### 4.8 Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Client.__init__() got an unexpected keyword argument 'proxies'` | httpx ≥ 0.28 with pinned openai client — `pip install "httpx<0.28"` |
| `numpy.dtype size changed` on import | NumPy 2 with pandas 1.5.3 — `pip install "numpy<2"` |
| Every query times out | Raise `--timeout`; check the model isn't CPU-spilled (§4.3) |
| Agent can't find telemetry | Not running from repo root, or `main.adapt_aurora` not run |
| All time answers fail scoring | Timezone mismatch — all Aurora timestamps are IST (Asia/Kolkata); predictions must be `%Y-%m-%d %H:%M:%S` |

## 5. Baseline results — qwen3:32b (Q4_K_M, self-hosted), 122 queries, ~31 h

### Official OpenRCA scoring

| Difficulty | Total | Strict (score = 1.0) | Partial |
|---|---|---|---|
| easy (task 1–3) | 57 | 14.0 % | 14.0 % |
| middle (task 4–6) | 49 | 8.2 % | 18.4 % |
| hard (task 7) | 16 | 6.3 % | 8.3 % |
| **Total** | **122** | **10.7 %** | **15.0 %** |

**Context vs. original OpenRCA**: frontier models (GPT-4o / Claude-class) with the same agent
scaffold scored ≈ 8–11 % strict on the original OpenRCA datasets. A 32B open model reaching 10.7 %
on Aurora indicates (a) the pipeline functions as a genuine benchmark and (b) Aurora's difficulty is
in the intended range — hard, but with measurable headroom (per-family spread 4–27 %, i.e., the
benchmark discriminates rather than flooring).

All 122 investigations completed without degenerate outputs (no timeouts, no format collapses, no
context overflows) — scores measure reasoning, not pipeline breakage.

### Per fault family (strict mean)

| Family | Score | Family | Score |
|---|---|---|---|
| LinkFailure | 0.269 | MAC_Contention | 0.152 |
| Memory_Contention | 0.250 | PDCP_Contention | 0.125 |
| QueueSize_Tuning | 0.167 | CPU_Contention | 0.083 |
| L1_Contention | 0.167 | Memory_Leak | 0.083 |
| PortFlap | 0.154 | Network_Contention | 0.042 |

### Two-axis analysis (what vs. where)

Because predictions are stored raw, correctness can be re-scored leniently post-hoc:

| Axis | Accuracy |
|---|---|
| Component correct ("where") | 21.3 % |
| Fault family correct ("what", location suffix ignored) | 13.1 % |
| Exact reason label (family + location) | 5.7 % |
| Family right but location wrong (the double penalty of §2.3-2) | 7.4 % |

### Systematic error patterns

1. **CPU-default bias**: the dominant confusion column — when lacking discriminating evidence, the
   model answers CPU contention (PDCP→CPU ×8, Memory_Contention→CPU ×6, LinkFailure→CPU ×6,
   MAC→CPU ×5, PortFlap→CPU ×5, Memory_Leak→CPU ×5).
2. **Localization beats identification**: components are easier than reasons for this model.
3. **Sibling confusion**: PortFlap→LinkFailure ×6 — the log signature is identical (the SCTP
   association dies on the first flap and never re-establishes); only the metric time-profile
   distinguishes them, a verification step the model rarely performs well.

## 6. LLM-as-judge post-mortem (`main/diagnose.py`)

For every query, the deployed LLM receives a compact digest of the stored trajectory (the agent's
per-step analysis/instructions + truncated execution results) together with ground truth, prediction
and score — and returns a structured verdict: a 2–5 sentence **explanation**, a verbatim
**evidence quote**, the **decisive step**, and one **primary reason code** from a fixed taxonomy
(usable as a metric):

*Failure codes*: `wrong_localization`, `sibling_confusion`, `default_bias_guess`,
`insufficient_exploration`, `misread_evidence`, `anchoring_no_verification`,
`time_localization_error`, `execution_failure`, `ambiguous_telemetry`.
*Success codes*: `correct_with_evidence`, `correct_lucky_guess` (separates real capability from
coin flips).

The judge never re-decides correctness (the score is given); enum-validated JSON with corrective
retries; resumable output in `test/analysis/diagnosis.csv`.

**Final distribution** (all 122 queries judged; zero `judge_error` rows; the judge's correct-count
of 13 independently matches the 13 strict-perfect scores from `main.evaluate`):

| primary_reason | n | share |
|---|---|---|
| insufficient_exploration | 49 | 40.2 % |
| wrong_localization | 39 | 32.0 % |
| correct_with_evidence | 11 | 9.0 % |
| default_bias_guess | 9 | 7.4 % |
| sibling_confusion | 7 | 5.7 % |
| misread_evidence | 4 | 3.3 % |
| correct_lucky_guess | 2 | 1.6 % |
| execution_failure | 1 | 0.8 % |

Headline findings:

1. **Failures are process failures, not knowledge failures.** 72 % of all outcomes
   (`insufficient_exploration` 40 % + `wrong_localization` 32 %) come from the investigation never
   reaching or never cross-checking the decisive telemetry — not from misreading data it had
   (`misread_evidence` is only 3 %). The judge frequently classifies the CPU-default answers as
   downstream symptoms of skipped exploration rather than as the primary defect
   (`default_bias_guess` primary in only 9 cases).
2. **Correct answers are earned**: 11 of 13 are `correct_with_evidence`; only 2 lucky guesses.
   Together with (1), this suggests score improvements should come from better *search policy*
   (which telemetry to open, comparing both DUs before committing) rather than better raw
   interpretation — a concrete, testable direction for prompt or scaffold changes.
3. **Family-level texture matches the score table**: PortFlap failures skew to `sibling_confusion`
   /`misread_evidence` (the LinkFailure look-alike problem), while Memory_Leak and Network_Contention
   failures are dominated by `insufficient_exploration` (their signals — slow RSS drift, distributed
   congestion — are never examined at the right granularity).
4. **Pipeline robustness**: exactly one `execution_failure` across 122 trajectories.

**Judge verdict × fault family** (primary reason only; `correct_*` merged, low-count codes omitted
for width — full table via `--report-only`):

| Family | correct | insuff_explor | wrong_local | sibling_conf | default_bias | misread |
|---|---|---|---|---|---|---|
| CPU_Contention | 1 | 6 | 4 | 0 | 1 | 0 |
| L1_Contention | 2 | 3 | 6 | 0 | 0 | 1 |
| LinkFailure | 3 | 5 | 4 | 0 | 0 | 0 |
| MAC_Contention | 0 | 3 | 6 | 2 | 1 | 0 |
| Memory_Contention | 2 | 4 | 5 | 1 | 0 | 0 |
| Memory_Leak | 1 | 7 | 3 | 0 | 1 | 0 |
| Network_Contention | 0 | 6 | 3 | 1 | 2 | 0 |
| PDCP_Contention | 1 | 6 | 4 | 0 | 1 | 0 |
| PortFlap | 1 | 4 | 1 | 3 | 2 | 2 |
| QueueSize_Tuning | 2 | 5 | 3 | 0 | 1 | 1 |

The two families the score table ranks hardest have distinct judged causes: Network_Contention
fails on exploration breadth (no single component to inspect), while PortFlap uniquely concentrates
`sibling_confusion` + `misread_evidence` — the agent reaches the right neighborhood and stumbles on
the final discrimination.

## 7. Known limitations

- Reason labels embed fault location (see §2.3-2) — report the two-axis numbers alongside strict.
- Log wall-clock times are accurate to ≈ −0/+3 s; glued log lines keep raw monotonic stamps only.
- Instructions are templated, not LLM-naturalized.
- The judge is the same model family as the agent (self-judging); spot-check judgments against raw
  trajectories before treating the distribution as final, and prefer a stronger judge if available.
- Fault-free baseline runs are not part of the query set.

## 8. File map (added/modified for Aurora)

```
main/adapt_aurora.py                            dataset conversion (record/query/telemetry + sanitization + log alignment)
main/diagnose.py                                LLM-as-judge post-mortem pipeline
rca/baseline/rca_agent/prompt/basic_prompt_Aurora.py   agent dataset briefing
rca/run_agent_standard.py                       + Aurora branch
rca/baseline/rca_agent/executor.py              timezone rule generalized
rca/api_router.py                               model-entry selection (env / config default)
config.json                                     (gitignored) Ollama endpoint + model entries
dataset/Aurora/                                 (generated) record.csv, query.csv, telemetry/
test/result/Aurora/…                            per-query predictions + scores
test/monitor/Aurora/…                           full trajectories (logs / notebooks / prompts)
test/analysis/diagnosis.csv                     judge verdicts per query
```
