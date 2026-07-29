"""
LLM-as-judge post-mortem over stored RCA-agent trajectories.

For every query in a benchmark result CSV, reconstruct a compact digest of the
agent's investigation from the saved prompt JSON (controller conversation),
then ask the configured LLM (rca/api_router.py -> config.json) to explain WHY
the agent succeeded or failed and to assign one primary reason code from a
fixed taxonomy, usable as a metric.

Usage:
    python -m main.diagnose \
        --result "test/result/Aurora/agent-baseline-qwen3:32b.csv" \
        --monitor "test/monitor/Aurora/agent-baseline-qwen3:32b" \
        --query dataset/Aurora/query.csv \
        --out test/analysis/diagnosis.csv \
        [--sample 5] [--report-only]

Resumable: already-judged row_ids are skipped on re-run.
"""
import argparse
import glob
import json
import os
import re
import sys

import pandas as pd

parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, parent_dir)

from rca.api_router import get_chat_completion  # noqa: E402

FAILURE_CODES = {
    "wrong_localization": "The agent identified the correct fault family but attributed it to the wrong component (e.g., du0 instead of du1, cu instead of a DU, wrong F1 link).",
    "sibling_confusion": "The agent identified the correct target area but confused two similar fault types on it (e.g., PortFlap vs LinkFailure on the same link, Memory_Leak vs Memory_Contention on the same node).",
    "default_bias_guess": "The agent fell back to a generic/default diagnosis (commonly CPU contention) without discriminating evidence separating it from alternatives.",
    "insufficient_exploration": "The agent never examined the telemetry that contained the decisive signal (e.g., never opened the logs, never compared both DUs, never checked the relevant KPI family).",
    "misread_evidence": "The agent examined the right data but drew an unsupported or wrong conclusion from it (e.g., treating an always-zero counter as a 'drop to zero', misapplying a threshold).",
    "anchoring_no_verification": "The agent formed a hypothesis early and only sought confirming evidence, skipping checks that could have refuted it.",
    "time_localization_error": "The agent identified the cause correctly but reported an onset time outside the accepted tolerance.",
    "execution_failure": "Executor code errors, output truncation, context overflow, or malformed responses materially blocked the investigation.",
    "ambiguous_telemetry": "The stored telemetry genuinely lacked a decisive signal for this fault; a well-executed investigation could plausibly still miss it.",
}
SUCCESS_CODES = {
    "correct_with_evidence": "The final answer is supported by a decisive, correctly-interpreted signal that the agent explicitly found in the telemetry.",
    "correct_lucky_guess": "The final answer is right, but the trajectory does not contain a sound evidence chain leading to it (guess, coincidence, or flawed reasoning that happened to land on the truth).",
}
ALL_CODES = {**FAILURE_CODES, **SUCCESS_CODES}

JUDGE_SYSTEM = """You are an expert reviewer of automated root-cause-analysis (RCA) investigations on a 5G RAN testbed (CU, two DUs, F1 links). You are given: the task, the ground-truth fault, the agent's final answer, the evaluation score, and a step-by-step digest of the agent's investigation (its stated analysis, the instructions it issued, and truncated execution results).

The correctness verdict is ALREADY decided by the score; do not re-litigate it. Your job is to explain WHY the investigation ended where it did, and to classify the outcome with exactly one primary reason code.

Reason codes for FAILED or PARTIAL outcomes (score < 1.0):
{failure_defs}

Reason codes for CORRECT outcomes (score == 1.0):
{success_defs}

Rules:
- primary_reason must be exactly one code, drawn from the failure list if score < 1.0, or the success list if score == 1.0.
- secondary_reasons: zero or more additional contributing codes (may mix lists only when score is partial).
- decisive_step: the step number where the outcome was effectively determined (the step containing the decisive evidence for successes, or the pivotal mistake for failures). Use 0 if no single step qualifies.
- evidence_quote: a short verbatim quote (<=200 chars) from the digest supporting your classification.
- explanation: 2-5 sentences, concrete, referencing what the agent actually did. No hedging boilerplate.

Respond with ONLY a JSON object:
{{"explanation": "...", "primary_reason": "...", "secondary_reasons": [...], "decisive_step": <int>, "evidence_quote": "..."}}

Example. For a failed case where ground truth was PortFlap_CU_du1 / CU-du1-link, the agent found an SCTP teardown in F1AP.log, asserted without evidence that the association belonged to du0's link, "verified" via a du0 counter that was zero both before and during the fault, and answered LinkFailure_CU_du0 / CU-du0-link:

{{"explanation": "The agent found the genuine fault footprint (the SCTP association teardown at 09:02:58) but attributed it to the wrong link by assuming assoc_id 541 mapped to du0, with no supporting lookup. Its verification step only examined du0 counters and accepted a counter that was zero before AND during the fault as confirmation, which carries no information. A comparison of both DUs' midhaul traffic would have refuted the hypothesis. It also inferred a permanent failure from the absence of further SCTP events, which cannot distinguish a flap whose association never re-established.", "primary_reason": "wrong_localization", "secondary_reasons": ["anchoring_no_verification", "misread_evidence", "sibling_confusion"], "decisive_step": 3, "evidence_quote": "assoc_id 541 likely maps to the CU-du0-link (as CU-du1-link would have a distinct association ID)"}}"""

JUDGE_USER = """## TASK GIVEN TO THE AGENT
{instruction}
(task type: {task_index})

## GROUND TRUTH
{groundtruth}

## AGENT'S FINAL ANSWER
{prediction}

## EVALUATION
score: {score}
passed criteria: {passed}
failed criteria: {failed}

## INVESTIGATION DIGEST
{digest}

Classify this investigation. Respond with the JSON object only."""

OBS_HEAD, OBS_TAIL = 400, 150
DIGEST_CHAR_BUDGET = 30000


def build_digest(prompt_file: str) -> str:
    with open(prompt_file, "r", encoding="utf8") as f:
        messages = json.load(f)["messages"]

    def clip(text, head=OBS_HEAD, tail=OBS_TAIL):
        text = text.strip()
        if len(text) <= head + tail + 20:
            return text
        return f"{text[:head]}\n[... truncated ...]\n{text[-tail:]}"

    lines, step = [], 0
    for msg in messages[2:]:  # skip system prompt and the "Let's begin." kick-off
        content = msg["content"]
        if msg["role"] == "assistant":
            try:
                d = json.loads(content)
                if "analysis" in d and "instruction" in d:
                    step += 1
                    lines.append(f"### Step {step}")
                    lines.append(f"[agent analysis] {clip(str(d['analysis']), 700, 0)}")
                    lines.append(f"[agent instruction] {clip(str(d['instruction']), 400, 0)}")
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            lines.append(f"[agent final response] {clip(content, 900, 200)}")
        else:
            if content.startswith("Continue your reasoning") or content.startswith("Now, you have decided") \
               or content.startswith("Now, the maximum steps"):
                continue
            lines.append(f"[execution result] {clip(content)}")

    digest = "\n".join(lines)
    if len(digest) > DIGEST_CHAR_BUDGET:  # rare: clip mid-trajectory steps harder
        keep_head = digest[: DIGEST_CHAR_BUDGET // 2]
        keep_tail = digest[-DIGEST_CHAR_BUDGET // 2:]
        digest = f"{keep_head}\n[... middle of trajectory omitted for length ...]\n{keep_tail}"
    return digest


def judge_one(row, digest, max_try=3):
    system = JUDGE_SYSTEM.format(
        failure_defs="\n".join(f"- {k}: {v}" for k, v in FAILURE_CODES.items()),
        success_defs="\n".join(f"- {k}: {v}" for k, v in SUCCESS_CODES.items()),
    )
    user = JUDGE_USER.format(
        instruction=row["instruction"], task_index=row["task_index"],
        groundtruth=row["groundtruth"], prediction=row["prediction"],
        score=row["score"], passed=row.get("passed", ""), failed=row.get("failed", ""),
        digest=digest,
    )
    score = float(row["score"])
    valid = set(SUCCESS_CODES) if score == 1.0 else set(FAILURE_CODES)

    prompt = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_err = None
    for _ in range(max_try):
        raw = get_chat_completion(messages=prompt, temperature=0.0)
        try:
            m = re.search(r"\{.*\}", raw, re.S)
            d = json.loads(m.group(0))
            assert d.get("primary_reason") in valid, \
                f"primary_reason must be one of {sorted(valid)} for score={score}"
            d["secondary_reasons"] = [c for c in d.get("secondary_reasons", []) if c in ALL_CODES]
            d["decisive_step"] = int(d.get("decisive_step", 0))
            return d
        except Exception as e:  # noqa: BLE001
            last_err = e
            prompt += [{"role": "assistant", "content": raw},
                       {"role": "user", "content": f"Invalid response ({e}). Respond with only the JSON object in the required format."}]
    return {"explanation": f"JUDGE_ERROR: {last_err}", "primary_reason": "judge_error",
            "secondary_reasons": [], "decisive_step": 0, "evidence_quote": ""}


def report(out_csv: str, result_df: pd.DataFrame):
    d = pd.read_csv(out_csv)
    d = d.merge(result_df[["row_id", "task_index"]], on="row_id", how="left", suffixes=("", "_r"))
    print("\n=== primary_reason distribution ===")
    print(d["primary_reason"].value_counts().to_string())
    if "gt_reason" in d.columns:
        d["family"] = d["gt_reason"].str.replace(r"_(cu|du0|du1|CU_du0|CU_du1)$", "", regex=True)
        print("\n=== primary_reason x fault family ===")
        print(pd.crosstab(d["family"], d["primary_reason"]).to_string())
    ok = d[d["score"] == 1.0]
    if len(ok):
        lucky = (ok["primary_reason"] == "correct_lucky_guess").sum()
        print(f"\ncorrect answers: {len(ok)}  with evidence: {len(ok)-lucky}  lucky guesses: {lucky}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True)
    ap.add_argument("--monitor", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=None, help="judge only the first N rows")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    res = pd.read_csv(args.result)
    res["row_id"] = res["row_id"].astype(int)
    if args.report_only:
        report(args.out, res)
        return

    # ground-truth reason for every row, from the result CSV's groundtruth field
    # (present for all tasks, unlike scoring_points which only mentions asked elements)
    gt_reason = {}
    for _, r in res.iterrows():
        m = re.search(r"^reason: (.+)$", str(r["groundtruth"]), re.M)
        gt_reason[int(r["row_id"])] = m.group(1).strip() if m else ""

    done = set()
    if os.path.exists(args.out):
        done = set(pd.read_csv(args.out)["row_id"].astype(int))
    else:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)

    rows = res.head(args.sample) if args.sample else res
    for _, row in rows.iterrows():
        rid = int(row["row_id"])
        if rid in done:
            continue
        hits = glob.glob(os.path.join(args.monitor, "*", "prompt", f"*_#{rid}-0.json"))
        if not hits:
            print(f"[warn] no prompt file for row {rid}, skipping")
            continue
        digest = build_digest(hits[0])
        verdict = judge_one(row, digest)
        out_row = pd.DataFrame([{
            "row_id": rid,
            "task_index": row["task_index"],
            "score": row["score"],
            "gt_reason": gt_reason.get(rid, ""),
            "groundtruth": row["groundtruth"],
            "prediction": row["prediction"],
            "primary_reason": verdict["primary_reason"],
            "secondary_reasons": ";".join(verdict["secondary_reasons"]),
            "decisive_step": verdict["decisive_step"],
            "evidence_quote": verdict.get("evidence_quote", ""),
            "explanation": verdict["explanation"],
        }])
        header = not os.path.exists(args.out)
        out_row.to_csv(args.out, mode="a", header=header, index=False)
        print(f"row {rid}: score={row['score']} -> {verdict['primary_reason']}")

    report(args.out, res)


if __name__ == "__main__":
    main()
