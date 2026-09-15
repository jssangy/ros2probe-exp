# Results

After an experiment finishes, open **its CSV directly in the master's `results/`**:

```text
results/
  experiment1.csv
  experiment2.csv
  experiment3.csv
  experiment4-wire.csv
  experiment4-wireless.csv
```

Each file contains the complete experiment, including all default conditions and
ten repetitions. Each row summarizes one condition. Collection and processing
are automatic; only these five files are needed for the paper comparison.

| CSV | Rows | Grouping | Values |
| --- | ---: | --- | --- |
| `experiment1.csv` | 18 | Slave, NP, condition | SPDP, SEDP, Control, total discovery messages, qualifying packet count, discovery duration |
| `experiment2.csv` | 15 | Platform, ST100/ST500/ST1000, condition | Observer CPU, PSS memory, original-subscriber drop |
| `experiment3.csv` | 6 | Injected loss, recorder | Subscriber drop, recorder drop, loss recall, extra/missed recorded sequences |
| `experiment4-wire.csv` | 70 | Link, QoS, S1–S7, condition | NIC received bandwidth and original-subscriber drop |
| `experiment4-wireless.csv` | 70 | Link, QoS, S1–S7, condition | The same metrics on Wi-Fi |

`n_runs` is the number of trials in the row. Columns ending in `_mean` and `_sd`
are the mean and sample standard deviation. N/A means undefined, not zero.

- **Discovery:** `spdp`, `sedp`, `control` and `discovery_messages` count RTPS
  submessages. `discovery_packet_count` counts packets; `discovery_duration_ms`
  is milliseconds. Both slaves use the same interval from scheduled creation to
  completion of all workload endpoint matches. Slave results remain separate.
- **Resources:** `cpu_pct` is percent of total logical-core capacity; `pss_mib`
  is PSS in MiB. Compare rp_hz with topic_hz and rp_bag with rosbag2 at the same rate.
- **Fidelity:** drop values are percentages; `loss_recall` is a fraction from 0
  to 1 and is N/A when there is no subscriber loss. `loss_recall_n` counts the
  trials with defined recall. `extra_received` and `missed_received` count sequence
  differences; both are zero for identical recordings.
- **Probe effect:** `rx_mbps` is total NIC RX in Mbps, including protocol/SSH
  traffic. `subscriber_drop_pct` is delivery shortfall, also affected by publisher
  backpressure. Compare each condition with baseline at the same link, QoS and
  scenario. S7 uses the original `/points/front` subscriber.

The final CSV is updated only after all selected conditions and repetitions have
valid evidence. A successful rerun replaces that experiment's file; a failed run
keeps the previous successful file. `run_id` identifies its source directory,
`results/<run-id>/`, containing the detailed measurements and logs. High measured
loss remains in the CSV.

[Setup and execution](../README.md)
