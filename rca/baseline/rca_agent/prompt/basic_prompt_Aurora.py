cand = """## POSSIBLE ROOT CAUSE REASONS:

- CPU_Contention_cu
- CPU_Contention_du0
- CPU_Contention_du1
- L1_Contention_du0
- L1_Contention_du1
- LinkFailure_CU_du0
- LinkFailure_CU_du1
- MAC_Contention_du0
- MAC_Contention_du1
- Memory_Contention_cu
- Memory_Contention_du0
- Memory_Contention_du1
- Memory_Leak_cu
- Memory_Leak_du0
- Memory_Leak_du1
- Network_Contention
- PDCP_Contention_cu
- PortFlap_CU_du0
- PortFlap_CU_du1
- QueueSize_Tuning_du0
- QueueSize_Tuning_du1

## POSSIBLE ROOT CAUSE COMPONENTS:

- cu
- du0
- du1
- CU-du0-link
- CU-du1-link
- Network"""

schema = f"""## SYSTEM UNDER DIAGNOSIS:

The target system is a containerized 5G radio access network (OpenAirInterface) with an F1 functional split:

- `cu`: the Centralized Unit (hosts RRC / PDCP / NGAP toward the 5G core).
- `du0`, `du1`: two Distributed Units (each hosts RLC / MAC / PHY (L1) and serves its own group of UEs).
- `CU-du0-link`, `CU-du1-link`: the F1 transport links between the CU and each DU (F1-C control plane over SCTP, F1-U user plane over GTP-U).
- `Network`: the shared network infrastructure connecting all components.

Failures on a DU or its F1 link only degrade that DU's users; CU or Network failures can affect everything.

## TELEMETRY DIRECTORY STRUCTURE:

- Each issue names a specific run (e.g., `Aurora/run_42`). Its telemetry directory is `dataset/Aurora/telemetry/run_42/`, containing three subdirectories: `metric/`, `log/`, and `topology/`.
- `metric/` holds one wide-format CSV per component: `cu_aurora_metrics_*.csv`, `du_du0_aurora_metrics_*.csv`, `du_du1_aurora_metrics_*.csv`. Occasionally a component has more than one file (the process restarted); read every file matching the component's pattern, concatenate, and sort by `timestamp_us`.
- `log/` holds plain-text OpenAirInterface protocol logs: `F1AP.log`, `NGAP.log`, `RRC.log`, `PDCP.log`, `RLC.log`, `MAC.log`, `PHY.log`, `HW.log`, plus `TIMEBASE.txt` describing the log clock.
- `topology/topology.jsonl` holds JSON-lines snapshots (at run start and run end) of the deployment: host, pods/containers with CPU/memory limits and IPs, and the F1 link inventory.

## DATA SCHEMA

1. **Metric files** (wide format, one row per ~100 ms sample, one file per component -- unlike datasets keyed by a `cmdb_id` column, here the component is identified by the FILE name):

    - `timestamp_us`: epoch time in MICROSECONDS.
    - Sampling period is ~100 ms, so a 10-minute run has ~6,000 rows. A CU file has ~200 columns; a DU file has ~600.
    - Column families (suffixes `_min`/`_max`/`_avg` are per-sample aggregates):
        - `bhtx_*`, `bhrx_*`: backhaul (toward core) bytes/packets per sample.
        - `mhtx_*`, `mhrx_*`: midhaul (F1, CU<->DU) bytes/packets per sample.
        - `f1u_rlc_*`, `rlc_f1_*`, `rlc_mac_*`, `mac_rlc_*`: inter-layer traffic counters.
        - `mac_*`: MAC scheduler stats (active UEs, TBS/PRB/MCS, BLER, CQI, PHR, BSR, PUSCH/PUCCH SNR, `mac_dl_bo*` = downlink buffer occupancy).
        - `dl_harq_*`, `ul_crc_*`: downlink HARQ ACK/NACK and uplink CRC error counts/rates (radio-link quality).
        - `sinr_*`, `csi_*`, `bsr_*`: radio signal quality and buffer status reports.
        - `fapi_*`, `dl_fapi_*`, `ul_fapi_*`: PHY-MAC interface (PDSCH/PUSCH scheduling counts, MCS/PRB/TBS).
        - `kpm_*`: 3GPP KPM service-level measurements (`kpm_RRC.ConnNumber` = connected UEs, `kpm_DRB.UEThpDl/Ul` = user throughput, `kpm_CARR.PRBUsageDl/Ul`, `kpm_CARR.AverageCQI`, ...).
        - `rlc_*`, `pdcp_*`: per-layer bearer counts, PDU/SDU byte rates, lost packets, `rlc_hol_us_*` = head-of-line delay in us.
        - `gtp_*`: GTP-U tunnel counts, byte rates, tx/rx errors.
        - `tc_*`: traffic-control (qdisc) queue stats on the F1 path: queue length, drops, drop rate, scheduling delay in us.
        - `rc_cell_count`, `rc0_*`: radio-cell stats (RSRP, SINR, cell load %, interference level, handover counts).
        - `ru_utime_us`, `ru_stime_us`, `ru_maxrss_kb`, `ru_minflt`, `ru_majflt`, `ru_nvcsw`, `ru_nivcsw`, ...: OS resource usage of the component's process (CPU time, resident memory, page faults, context switches) -- useful for CPU/memory-related diagnosis.
        - DU files only -- `THREAD_<name>_*` (e.g. `THREAD_L1`, `THREAD_MAC_STATS`, `THREAD_TASK_DU_F1`, `THREAD_TASK_SCTP`, `THREAD_TASK_GTPV1_U`, `THREAD_Tpool0__1`...): per-thread scheduling statistics (runtime mean/max/std-dev/skewness/kurtosis in ns, involuntary context switches, CPU migrations) -- useful for diagnosing processing contention in a specific protocol layer.

2. **Log files** (plain text, NOT csv):

    - Annotated line format: `2026-07-21 09:02:58.845 | 1007666.550351 [F1AP]   I Received SCTP state 1 for assoc_id 541, removing endpoint`.
    - The first field is local wall-clock time (accurate to a few seconds; see `TIMEBASE.txt`); the second is the raw monotonic timestamp (seconds since host boot); then `[LAYER]`, severity (I/W/E/A), and the message.
    - Some lines are glued together by the log writer (a second record embedded mid-line, without its own wall-clock prefix); parse defensively.
    - Logs may include events from before the issue's time range (process startup, earlier UE attachments). Filter by the wall-clock prefix.

3. **Topology file**: JSON lines with `event` = `run_start`/`run_end`, `ts_iso`/`ts_epoch_ms`, `host`, `containers` (pod, container, cpu_request, mem_limit_bytes, network IPs), and `links` (name `F1_CU_du0`/`F1_CU_du1`, endpoints, status). Snapshots only -- it does NOT record status changes during the run.

{cand}

## CLARIFICATION OF TELEMETRY DATA:

1. All timestamps and all issue descriptions use the **Asia/Kolkata (IST, UTC+5:30)** timezone. Use `pytz.timezone('Asia/Kolkata')` when converting `timestamp_us` (divide by 1e6 first) or comparing with issue time ranges.

2. This dataset contains **no distributed traces**. Cross-component reasoning uses: the F1 byte counters on both ends (`mhtx_*`/`mhrx_*` on CU vs `f1u_rlc_*` on DU), `F1AP.log` SCTP events for control-plane link health, and the topology file for deployment structure.

3. The component in a metric file name is the metric's SOURCE. A fault on one component is usually visible in other components' metrics too (e.g., an F1 link failure collapses midhaul counters on BOTH the CU and the affected DU); attribute the root cause to the origin, not every affected component.

4. Each run contains a single failure whose onset lies strictly inside the issue's stated time range; telemetry before the onset within the range reflects normal behavior and is a good baseline for thresholding.

5. Root cause reasons are the platform's canonical fault labels (e.g. `CPU_Contention_du0` = CPU starvation on du0's host container; `L1_Contention_du1` = PHY-layer processing contention on du1; `QueueSize_Tuning_du0` = misconfigured F1 queue size for du0; `PortFlap_CU_du0` = the CU-du0 F1 link repeatedly toggling down/up; `LinkFailure_CU_du0` = the CU-du0 F1 link going down and staying down). Answer with the exact label string, including its component suffix where present, consistent with your identified root cause component.

6. Logs and metrics are complementary; cross-validate before concluding. A protocol log line proves an event happened but usually does NOT identify which component or link it belongs to, nor whether the condition was transient or sustained -- do not infer component identity from log-internal identifiers (such as an SCTP assoc_id) alone. Instead, verify against per-component metrics over the failure window: e.g., compare EACH DU's F1/midhaul traffic counters to see whose traffic was actually disrupted, and examine the disruption's time profile (a sustained flat drop vs. repeated bouncing vs. gradual drift) to distinguish between fault types affecting the same interface."""
