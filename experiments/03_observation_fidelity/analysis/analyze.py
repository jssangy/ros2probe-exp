#!/usr/bin/env python3
"""Analyze Experiment 3 observer fidelity from sub.log and bag files."""

from __future__ import annotations

import argparse
import csv
import re
import struct
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

import sys
sys.path.insert(0, str(REPO_ROOT))
from common.metrics import require_complete_runs, require_run_metadata, fidelity_subscriber
from common.check_measurements import check as check_measurements
from common.sequence_data import read_sequences


SEQ_RE = re.compile(r"^SEQ\s+(\d+)\s*$")
CONFIG_RE = re.compile(r"\bexpected=(\d+)\b")
TOPIC = "/drop_image"


def parse_sub_log(path: Path) -> tuple[set[int], int | None]:
    seqs: set[int] = set()
    expected: int | None = None
    if not path.exists():
        return seqs, expected
    for line in path.read_text(errors="ignore").splitlines():
        if expected is None:
            match = CONFIG_RE.search(line)
            if match:
                expected = int(match.group(1))
        match = SEQ_RE.match(line.strip())
        if match:
            seqs.add(int(match.group(1)))
    return seqs, expected


def open_reader(path: Path):
    import rosbag2_py
    storage_id = "sqlite3" if path.is_dir() and list(path.glob("*.db3")) else "mcap"
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(path), storage_id=storage_id),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )
    return reader


def extract_bag_seqs(path: Path, topic: str = TOPIC) -> set[int]:
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import Image
    if not path.exists():
        raise FileNotFoundError(f"Missing recording required for fidelity analysis: {path}")
    reader = open_reader(path)
    topic_types = {meta.name: meta.type for meta in reader.get_all_topics_and_types()}
    if topic not in topic_types:
        raise ValueError(f"Missing target topic in recording: {path}")
    if topic_types[topic] != "sensor_msgs/msg/Image":
        raise ValueError(f"{path}: topic {topic} has type {topic_types[topic]}")

    seqs: set[int] = set()
    while reader.has_next():
        msg_topic, data, _bag_time_ns = reader.read_next()
        if msg_topic != topic:
            continue
        msg = deserialize_message(data, Image)
        if len(msg.data) < 8:
            raise ValueError(f'Malformed sequence payload in recording: {path}')
        seq = struct.unpack_from("<Q", bytes(msg.data), 0)[0]
        if seq == 0:
            raise ValueError(f'Invalid zero sequence in recording: {path}')
        seqs.add(seq)
    return seqs


def ratio(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return num / den


def pct(num: int, den: int) -> float | None:
    value = ratio(num, den)
    return None if value is None else value * 100.0


def jaccard(a: set[int], b: set[int]) -> float | None:
    union = a | b
    if not union:
        return None
    return len(a & b) / len(union)


def find_bag(base: Path, loss_dir: str, condition: str, run: str) -> Path:
    run_dir = base / loss_dir / condition / run
    if condition == "rp_bag":
        return run_dir / "rp.mcap"
    if condition == "rosbag2":
        return run_dir / "rosbag2"
    raise ValueError(condition)


def collect(results_dir: Path, bags_dir: Path, sequence_csv=False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    require_complete_runs(results_dir)
    for loss_dir in sorted(results_dir.glob("loss*")):
        if not loss_dir.is_dir():
            continue
        loss_pct = int(loss_dir.name.replace("loss", ""))
        for condition_dir in sorted(loss_dir.iterdir()):
            if not condition_dir.is_dir():
                continue
            condition = condition_dir.name
            for run_dir in sorted(condition_dir.glob("run*")):
                protocol = require_run_metadata(run_dir)
                sub_recv, expected = fidelity_subscriber(run_dir / "sub.log")
                control = (run_dir / "control.log").read_text()
                if f"loss_acknowledged={loss_pct}\n" not in control or f"publisher_completed={expected}\n" not in control:
                    raise ValueError(f"Missing loss/publisher acknowledgement: {run_dir}")
                check_measurements(run_dir)
                expected_set = set(range(1, expected + 1))
                sub_lost = expected_set - sub_recv

                bag_path = find_bag(bags_dir, loss_dir.name, condition, run_dir.name)
                observer_recv = read_sequences(run_dir, expected) if sequence_csv else extract_bag_seqs(bag_path)
                if observer_recv - expected_set:
                    raise ValueError(f'Out-of-range recorder sequence: {bag_path}')
                if not observer_recv:
                    raise ValueError(f"Recording has no target sequence data: {bag_path}")
                observer_lost = expected_set - observer_recv

                lost_intersection = observer_lost & sub_lost
                recv_intersection = observer_recv & sub_recv
                rows.append({
                    "protocol": protocol,
                    "execution_profile": (run_dir / 'execution_profile.txt').read_text().strip() if (run_dir / 'execution_profile.txt').exists() else 'unspecified',
                    "loss_pct": loss_pct,
                    "condition": condition,
                    "run": run_dir.name,
                    "sub_log": str(run_dir / "sub.log"),
                    "bag": "" if sequence_csv else str(bag_path),
                    "sequence_csv": str(run_dir / 'observer_sequences.csv') if sequence_csv else "",
                    "expected": expected,
                    "actual_recv": len(sub_recv),
                    "observer_recv": len(observer_recv),
                    "actual_drop_pct": pct(len(sub_lost), expected),
                    "observer_drop_pct": pct(len(observer_lost), expected),
                    "recv_jaccard": jaccard(sub_recv, observer_recv),
                    "lost_jaccard": jaccard(sub_lost, observer_lost),
                    "lost_precision": ratio(len(lost_intersection), len(observer_lost)),
                    "lost_recall": ratio(len(lost_intersection), len(sub_lost)),
                    "recv_overlap_count": len(recv_intersection),
                    "lost_overlap_count": len(lost_intersection),
                    "observer_extra_recv_count": len(observer_recv - sub_recv),
                    "observer_missed_recv_count": len(sub_recv - observer_recv),
                })
    return rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[int, str], list[dict[str, object]]] = {}
    for row in rows:
        groups.setdefault((int(row["loss_pct"]), str(row["condition"])), []).append(row)

    from statistics import stdev
    summary: list[dict[str, object]] = []
    metrics = [
        "actual_drop_pct",
        "observer_drop_pct",
        "recv_jaccard",
        "lost_jaccard",
        "lost_precision",
        "lost_recall",
    ]
    for (loss_pct, condition), group in sorted(groups.items()):
        out: dict[str, object] = {
            "loss_pct": loss_pct,
            "condition": condition,
            "n_runs": len(group),
        }
        for metric in metrics:
            values = [float(row[metric]) for row in group if row[metric] is not None]
            out[f"{metric}_mean"] = sum(values) / len(values) if values else None
            out[f"{metric}_std"] = stdev(values) if len(values) > 1 else None
            out[f"{metric}_n"] = len(values)
        summary.append(out)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", default=REPO_ROOT / "results/03_observation_fidelity", type=Path)
    parser.add_argument("--bags-dir", default=REPO_ROOT / "bags/03_observation_fidelity", type=Path)
    parser.add_argument("--out-dir", default=None, type=Path)
    parser.add_argument('--sequence-csv', action='store_true', help='Use portable slave exports; no ROS bindings required')
    args = parser.parse_args()

    out_dir = args.out_dir or args.results_dir / "analysis"
    rows = collect(args.results_dir, args.bags_dir, args.sequence_csv)
    if not rows:
        raise SystemExit(f"No Experiment 3 runs found under {args.results_dir}")
    write_csv(out_dir / "runs.csv", rows)
    invalid = [row for row in rows if str(row.get("validity", "")).startswith("invalid")]
    if invalid:
        raise ValueError(f"{len(invalid)} invalid runs; inspect {out_dir / 'runs.csv'} before reporting")
    write_csv(out_dir / "summary.csv", summarize(rows))
    print(f"Wrote {len(rows)} run rows to {out_dir / 'runs.csv'}")
    print(f"Wrote summary to {out_dir / 'summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
