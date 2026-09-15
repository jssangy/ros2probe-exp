# Experiment 1 — Discovery transparency

[Setup](../../README.md)

Compare baseline, rp_run and ros2_daemon at **NP=1,3,5 per slave**, with ten
repetitions: 90 synchronized trials. Total workload participants are 2, 6 and 10;
observers are excluded from NP. Both slaves capture their wired interface.

## Run

On the master:

```bash
./run_exp1.sh
```

Allow approximately 1.5 hours or more, depending on the testbed.

## Procedure

1. Synchronize clocks and assign a common participant-creation time T.
2. Start the observer at T−17 s, require readiness by T−15 s, and start capture
   at T−5 s. Workload processes start at T−3 s and wait for T.
3. At T, each participant creates one publisher and one subscriber on
   `/stress_topic_0` with RELIABLE/VOLATILE/KEEP_ALL QoS. No application samples
   are published.
4. Determine C, the latest first complete endpoint match across all workload
   participants on both slaves, checked about every 1 ms. Count discovery in [T,C).
5. Keep workloads/capture alive until T+7 s for validation, exclude traffic from
   C onward, and wait 25 s before the next trial.
6. The master collects both slaves' records and exports their results separately
   in the same CSV.

SPDP DATA, SEDP publication/subscription DATA and discovery HEARTBEAT/ACKNACK are
classified by built-in entity ID. Application traffic is excluded; repeated
announcements before C are counted. Use a dedicated domain/network, since there
is no workload GUID whitelist. Missing matching evidence fails analysis.

## Result

Open `results/experiment1.csv` on the master: **18 rows** (2 slaves × 3 NP
values × 3 conditions), each summarizing ten trials. It includes discovery
submessage counts, packet count and completion time. [Column meanings](../../docs/RESULTS.md).
