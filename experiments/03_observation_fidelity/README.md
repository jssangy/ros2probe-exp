# Experiment 3 — Observation fidelity

[Setup](../../README.md)

Compare **rp_bag and rosbag2** at **0%, 10% and 20% injected loss**, with ten
repetitions: **60 trials**. A publishes 1,800 messages on `/drop_image` at 30 Hz,
with 1,024-byte payloads and BEST_EFFORT QoS. B runs the subscriber and recorder.

## Run

On the master, using wired SSH:

```bash
./run_exp3.sh
```

Allow approximately 1.5 hours or more, depending on the testbed.

## Procedure

1. Start B's recorder and subscriber, then create A's publisher endpoints.
2. Match the required readers (one for rp_bag, two for rosbag2) and stabilize for 3 s.
3. Apply the selected loss to A's outgoing UDP toward B and confirm it is active.
   TCP control is unaffected; 0% leaves injection disabled.
4. Release publication at the master-assigned time and send all 1,800 messages.
5. Finalize the recording and remove the experiment's loss configuration.
6. B exports recorded sequence IDs. The master compares expected, subscriber and
   recorder sequence sets and writes the final CSV.

Loss is applied before capture on B so both observation paths see the same
network impairment. For expected sequences E, subscriber sequences S and recorder
sequences O, loss recall is |(E−S)∩(E−O)|/|E−S|. It is undefined when S lost no
messages. Missing recordings or inconsistent sequence counts fail analysis.

## Result

Open `results/experiment3.csv`: **6 rows** (3 loss levels × 2 recorders),
with subscriber/recorder drop, loss recall and sequence differences.
[Column meanings](../../docs/RESULTS.md).
B keeps original recordings under `bags/<run-id>/03_observation_fidelity/`.
