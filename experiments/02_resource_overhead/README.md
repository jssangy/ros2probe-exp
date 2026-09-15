# Experiment 2 — Resource overhead

[Setup](../../README.md)

The paper keeps **slave A as a fixed publisher laptop** and tests three platforms
separately as **slave B, the receiver: PC (laptop), Jetson Orin NX and Raspberry
Pi 4B**, connected over Gigabit Ethernet.
Each condition runs ten times. Raspberry Pi ST1000 recording is excluded because
of the SD-card write limit; its ST1000 rate measurement is included.

Compare **baseline, rp_hz, topic_hz, rp_bag and rosbag2** at ST100, ST500 and
ST1000: **150 trials for the PC receiver**. Slave A publishes a 65,536-byte
Image on `/stress` at 100, 500 or 1,000 Hz. Slave B runs the original subscriber,
observer and CPU/PSS/NIC samplers.

## Run

On the master, using wired SSH:

```bash
./run_exp2.sh
```

Allow approximately 3.5 hours or more, depending on the testbed and recording storage.

## Procedure

1. Start B's observer, original subscriber and samplers; verify readiness.
2. Start A's publisher at the master-assigned time.
3. Warm up for 10 s from the first callback, then measure for 60 s.
4. Retain a final sample, stop and validate processes/recordings, then wait 10 s.
5. After all rate and recording trials, collect and analyze on the master.

CPU is normalized by logical-core count; memory is summed observer PSS, including
its runtime/CLI descendants. CPU and NIC deltas use the exact subscriber interval;
PSS uses samples inside it. Both recorders use the same storage device.
Missing samples, rate output or readable recordings fail validation.

## Result

Open `results/experiment2.csv`: **15 PC rows** (3 rates × 5 conditions), with
CPU, PSS and subscriber drop means/SD. [Column meanings](../../docs/RESULTS.md).
B keeps 60 original recordings under `bags/<run-id>/02_resource_overhead/`.
