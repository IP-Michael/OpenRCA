"""
Adapter: convert the Aurora 5G-RAN fault-injection dataset (aurora_output/) into
the OpenRCA on-disk format (dataset/{SYSTEM}/record.csv, query.csv, telemetry/).

This does NOT call an LLM. query.csv's "instruction" field is built from a plain
deterministic template instead of OpenRCA's usual LLM-humanized issue text, because
this repo has no scripts/utils.py or configured API key for main/generate.py's
get_chat_completion() call. See the printed compromises for full details.

Usage:
    python -m main.adapt_aurora --src /home/michael/rca/aurora_output
"""
import argparse
import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
# OAI log lines start with CLOCK_MONOTONIC seconds since host boot (~11.6 days
# uptime for this campaign => 7-digit integer part). Anchored strictly so that
# glued/corrupted lines don't yield bogus timestamps.
LOG_TS_RE = re.compile(r"^(\d{7}\.\d{6})\s")
LOG_MONO_BAND = (0.9e6, 1.2e6)

random.seed(42)  # match main/generate.py's seeding for reproducibility

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "main" / "task_specification.json"
SYSTEM_NAME = "Aurora"


def load_ground_truth(src: Path) -> pd.DataFrame:
    gt = pd.read_csv(src / "ground_truth.csv")

    # Some ground_truth rows reference a run_id whose telemetry folder was never
    # written to disk (e.g. the run crashed before Aurora flushed its output).
    # Drop those runs entirely rather than emit unanswerable queries.
    def run_dir_exists(row) -> bool:
        return (src / row["anomaly_name"] / f"run_{row['run_id']}").exists()

    exists_mask = gt.apply(run_dir_exists, axis=1)
    missing_runs = sorted(gt.loc[~exists_mask, "run_id"].unique().tolist())
    if missing_runs:
        print(f"  [warn] dropping {len(missing_runs)} run(s) with no telemetry on disk: {missing_runs}")
    return gt[exists_mask].reset_index(drop=True)


def build_record_csv(gt: pd.DataFrame) -> pd.DataFrame:
    fault_rows = gt[gt["phase"] != "Normal"].copy()
    fault_rows["timestamp"] = fault_rows["phase_start_epoch_ms"] / 1000.0
    fault_rows["datetime"] = fault_rows["phase_start_iso"].str.replace("T", " ", regex=False)
    fault_rows["reason"] = fault_rows["anomaly_name"]
    fault_rows["level"] = fault_rows["category"]
    record = fault_rows[
        ["level", "component", "timestamp", "datetime", "reason",
         "run_id", "category", "anomaly_name", "phase_start_epoch_ms", "phase_end_epoch_ms"]
    ].sort_values("timestamp").reset_index(drop=True)
    return record


def derive_log_clock_offset(gt: pd.DataFrame, src: Path) -> float:
    """
    The .log files use CLOCK_MONOTONIC (seconds since host boot) while metrics
    and ground truth use epoch time; nothing in the dataset records the boot
    epoch. Recover the offset (epoch_seconds = monotonic_seconds + offset) from
    causality: in every link-fault run, the first fault-related SCTP teardown
    line in F1AP.log can only occur AFTER that run's first injection event, so
    each run yields a lower bound (injection_epoch - sctp_monotonic). All runs
    share one host boot, so the max over runs is the tightest bound; the fastest
    run's SCTP teardown fires within ~1s of injection, making the result
    accurate to a few seconds (validated: bounds from both campaign days agree,
    and implied per-run detection delays all land in the plausible 0-7s range).
    """
    bounds = []
    link_fault_runs = gt[gt["anomaly_name"].str.startswith(("PortFlap", "LinkFailure"))]
    for _, row in link_fault_runs.drop_duplicates("run_id").iterrows():
        run_dir = src / row["anomaly_name"] / f"run_{row['run_id']}"
        topo, f1ap = run_dir / "topology.jsonl", run_dir / "F1AP.log"
        if not topo.exists() or not f1ap.exists():
            continue
        starts = [json.loads(l)["ts_epoch_ms"] / 1000.0 for l in topo.open()
                  if json.loads(l).get("event") == "anomaly_start"]
        if not starts:
            continue
        entries = []
        for line in f1ap.open(errors="replace"):
            m = LOG_TS_RE.match(line)
            if not m:
                continue
            mono = float(m.group(1))
            if not (LOG_MONO_BAND[0] < mono < LOG_MONO_BAND[1]):
                continue
            entries.append((mono, "Starting F1AP" in line,
                            "removing endpoint" in line or "unsuccessful result" in line))
        # ignore SCTP lines within 120s after an F1AP (re)start: those are
        # startup/teardown noise, not reactions to the injected fault
        fault_sctp = [mono for mono, _, is_sctp in entries if is_sctp
                      and not any(is_st and 0 <= mono - m2 <= 120 for m2, is_st, _ in entries)]
        if fault_sctp:
            bounds.append(min(starts) - min(fault_sctp))

    if not bounds:
        raise RuntimeError("no link-fault anchor pairs found; cannot derive log clock offset")
    spread = max(bounds) - min(bounds)
    if spread > 30:
        raise RuntimeError(f"log clock offset bounds spread {spread:.1f}s is too wide; "
                           "constant-offset assumption looks violated")
    return max(bounds)


def annotate_log(src_file: Path, dest_file: Path, offset: float) -> None:
    """Write a copy of the log with each timestamped line prefixed by IST epoch time."""
    with open(src_file, "r", encoding="utf8", errors="replace") as fin, \
         open(dest_file, "w", encoding="utf8") as fout:
        for line in fin:
            m = LOG_TS_RE.match(line)
            if m:
                mono = float(m.group(1))
                if LOG_MONO_BAND[0] < mono < LOG_MONO_BAND[1]:
                    ts = datetime.fromtimestamp(mono + offset, IST)
                    fout.write(f"{ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} | {line}")
                    continue
            fout.write(line)


TIMEBASE_NOTE = """\
LOG TIME BASE
=============
Raw log lines begin with a monotonic clock value: seconds since host boot
(e.g. "1007666.550010"). To convert to wall-clock time:

    epoch_seconds = monotonic_seconds + {offset:.3f}

Each timestamped line has additionally been prefixed with the converted local
time ("YYYY-MM-DD HH:MM:SS.mmm | <original line>"), timezone Asia/Kolkata
(IST, UTC+5:30) -- the same timezone used by all metric timestamps and issue
descriptions. Lines without the prefix carry no parseable timestamp.

The conversion offset was calibrated by cross-referencing epoch-stamped
orchestration records with corresponding log entries across the collection
campaign. Estimated accuracy: converted times may lag true wall-clock time by
up to ~3 seconds. Treat log timestamps as approximate; prefer metric
timestamps for precise event timing.
"""


# topology.jsonl is written by the fault injector itself, so each event carries
# the injected anomaly's name/category/component and explicit anomaly_start/end
# markers -- i.e. the ground truth. Keep only the run_start/run_end topology
# snapshots and scrub the label fields from them.
_TOPOLOGY_KEEP_EVENTS = {"run_start", "run_end"}
_TOPOLOGY_LEAK_KEYS = {"anomaly", "category", "component"}


def sanitize_topology(src_file: Path, dest_file: Path) -> None:
    with open(src_file, "r", encoding="utf8") as fin, \
         open(dest_file, "w", encoding="utf8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("event") not in _TOPOLOGY_KEEP_EVENTS:
                continue
            for key in _TOPOLOGY_LEAK_KEYS:
                event.pop(key, None)
            fout.write(json.dumps(event) + "\n")


def link_telemetry(gt: pd.DataFrame, src: Path, dataset_out: Path, log_offset: float) -> None:
    # Metrics are hardlinked, not symlinked: a symlink's target path contains the
    # anomaly-named source folder (aurora_output/PortFlap_CU_du1/run_2/...), which
    # any agent could read back via os.path.realpath() and skip the diagnosis
    # entirely. Logs are small, so they get rewritten copies with wall-clock
    # annotations instead.
    tel_root = dataset_out / "telemetry"
    tel_root.mkdir(parents=True, exist_ok=True)

    for _, row in gt.drop_duplicates("run_id").iterrows():
        run_id = row["run_id"]
        anomaly = row["anomaly_name"]
        run_dir = src / anomaly / f"run_{run_id}"
        if not run_dir.exists():
            print(f"  [warn] missing source run dir: {run_dir}")
            continue

        dest = tel_root / f"run_{run_id}"
        (dest / "metric").mkdir(parents=True, exist_ok=True)
        (dest / "log").mkdir(parents=True, exist_ok=True)
        (dest / "topology").mkdir(parents=True, exist_ok=True)

        for f in run_dir.glob("*aurora_metrics_*.csv"):
            link = dest / "metric" / f.name
            if not link.exists():
                os.link(f, link)

        for f in run_dir.glob("*.log"):
            annotate_log(f, dest / "log" / f.name, log_offset)
        with open(dest / "log" / "TIMEBASE.txt", "w", encoding="utf8") as fout:
            fout.write(TIMEBASE_NOTE.format(offset=log_offset))

        topo = run_dir / "topology.jsonl"
        if topo.exists():
            sanitize_topology(topo, dest / "topology" / topo.name)


def build_query_csv(record: pd.DataFrame, gt: pd.DataFrame) -> pd.DataFrame:
    with open(SPEC_PATH, "r", encoding="utf8") as f:
        task_templates = json.load(f)
    full_task_ID_list = list(task_templates.keys())

    # Per-run observed window: Normal-phase start -> Fault-phase end (~10 min),
    # not OpenRCA's usual 30-min bucket, since each run's telemetry only covers
    # its own short window (see compromise #3 in the printed summary).
    windows = gt.groupby("run_id").agg(
        win_start=("phase_start_iso", "min"),
        win_end=("phase_end_iso", "max"),
    )

    rows = []
    for _, rec in record.iterrows():
        run_id = rec["run_id"]
        task_index = random.choice(full_task_ID_list)

        w = windows.loc[run_id]
        time_period = f"{w['win_start'].replace('T', ' ')} to {w['win_end'].replace('T', ' ')}"

        scoring_points = ""
        for point in task_templates[task_index]["scoring_points"]:
            scoring_points += point.format(
                idx="only",
                datetime=rec["datetime"],
                component=rec["component"],
                reason=rec["reason"],
            )
            scoring_points += "\n"

        asked_fields = [
            spec.split(":")[0].strip()
            for spec in task_templates[task_index]["output"]
        ]

        instruction = (
            f"During the time range {time_period} (Asia/Kolkata), exactly 1 failure "
            f"was detected on Aurora/run_{run_id}. The {', '.join(asked_fields)} "
            f"{'is' if len(asked_fields) == 1 else 'are'} currently unknown. "
            f"Please identify the {' and '.join(asked_fields)}."
        )

        rows.append({
            "task_index": task_index,
            "instruction": instruction,
            "scoring_points": scoring_points.strip() + "\n",
            "run_id": run_id,
        })

    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=str, default="/home/michael/rca/aurora_output")
    args = parser.parse_args()

    src = Path(args.src)
    dataset_out = REPO_ROOT / "dataset" / SYSTEM_NAME
    dataset_out.mkdir(parents=True, exist_ok=True)

    print(f"Loading ground truth from {src / 'ground_truth.csv'}")
    gt = load_ground_truth(src)

    print("Building record.csv ...")
    record = build_record_csv(gt)
    record.to_csv(dataset_out / "record.csv", index=False)
    print(f"  wrote {dataset_out / 'record.csv'} ({len(record)} rows)")

    print("Building query.csv ...")
    query = build_query_csv(record, gt)
    query[["task_index", "instruction", "scoring_points"]].to_csv(dataset_out / "query.csv", index=False)
    query.to_csv(dataset_out / "query_with_run_id.csv", index=False)  # debug copy w/ run_id kept
    print(f"  wrote {dataset_out / 'query.csv'} ({len(query)} rows)")

    print("Deriving log clock offset (monotonic -> epoch) ...")
    log_offset = derive_log_clock_offset(gt, src)
    print(f"  offset = {log_offset:.3f} s  (epoch = monotonic + offset)")

    print("Linking telemetry (hardlinks, annotated logs, sanitized topology) ...")
    link_telemetry(gt, src, dataset_out, log_offset)
    print(f"  linked telemetry under {dataset_out / 'telemetry'}")

    print("\nDone.")


if __name__ == "__main__":
    main()
