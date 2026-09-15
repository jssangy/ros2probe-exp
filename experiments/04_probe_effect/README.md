# Experiment 4 — Probe effect

[Setup](../../README.md)

Measure observer impact on subscriber delivery and received traffic. A runs the
publishers; B runs the original subscribers and observer. Each link tests **S1–S7,
BEST_EFFORT and RELIABLE**, with ten repetitions of **baseline, rp_hz, topic_hz,
rp_bag_all and rosbag2_all**: **700 trials per link**.

| Scenario | Topic | Payload | Rate |
| --- | --- | --- | --- |
| S1 | `/imu` | 320 B | 200 Hz |
| S2 | `/scan` | 4.3 KB | 40 Hz |
| S3 | `/points` | 644 KB | 20 Hz |
| S4 | `/points` | 2.72 MB | 20 Hz |
| S5 | `/image_raw/compressed` | 150 KB | 30 Hz |
| S6 | `/depth/image_raw` | 600 KB | 30 Hz |
| S7 | Nine topics: command, IMU, points, cameras and depth | About 830 Mbps offered | Mixed |

## Run

On the master, for Ethernet:

```bash
./run_exp4_wired.sh
```

After following the [Wi-Fi switching steps](../../README.md#switch-to-experiment-wi-fi):

```bash
./run_exp4_wireless.sh
```

Each command runs both QoS profiles and every scenario. SSH and data use the
named link; the opposite interface is disabled during measurement and restored
afterward. Allow approximately **15 hours or more per link**, depending on the
testbed and recording storage.

## Procedure

1. Start B's observer, original subscribers and NIC sampler; verify readiness.
2. Start A's publishers at the master-assigned time.
3. Measure arrivals in [start,end) for 60 s from the first callback, ending at the
   deadline even if traffic stops.
4. Retain a final NIC sample, stop and validate the measurements/recordings, and
   wait 10 s before the next trial.
5. Run the second QoS profile, then collect and analyze both profiles on the master.

S7 retains all nine original subscribers. Rate tools watch `/points/front`; bag
tools record all nine topics. Its main comparison uses the front-points subscriber.
NIC RX includes protocol/SSH traffic. Delivery shortfall also reflects publisher
backpressure under RELIABLE. Invalid timing, samples or recordings fail validation.

## Result

Open `results/experiment4-wire.csv` for wired or
`results/experiment4-wireless.csv` for wireless: **70 rows per file** (2 QoS
profiles × 7 scenarios × 5 conditions), with received Mbps and subscriber drop means/SD.
[Column meanings](../../docs/RESULTS.md).
B keeps 280 recordings per link under `bags/<run-id>/04_probe_effect/`.
