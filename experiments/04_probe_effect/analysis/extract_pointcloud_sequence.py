#!/usr/bin/env python3
"""Extract publisher sequence numbers embedded in PointCloud2.data[0:8]."""

from __future__ import annotations

import argparse
import csv
import struct
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

import rosbag2_py
from rclpy.serialization import deserialize_message
from sensor_msgs.msg import PointCloud2


def open_reader(path: Path) -> rosbag2_py.SequentialReader:
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


def extract(path: Path, topic: str) -> list[tuple[int, int, int]]:
    reader = open_reader(path)
    topic_types = {meta.name: meta.type for meta in reader.get_all_topics_and_types()}
    if topic not in topic_types:
      available = ", ".join(sorted(topic_types)) or "(none)"
      raise SystemExit(f"topic not found: {topic}; available topics: {available}")
    if topic_types[topic] != "sensor_msgs/msg/PointCloud2":
      raise SystemExit(f"topic {topic} has type {topic_types[topic]}, expected sensor_msgs/msg/PointCloud2")

    rows: list[tuple[int, int, int]] = []
    index = 0
    while reader.has_next():
        msg_topic, data, bag_time_ns = reader.read_next()
        if msg_topic != topic:
            continue
        msg = deserialize_message(data, PointCloud2)
        if len(msg.data) < 8:
            continue
        seq = struct.unpack_from("<Q", bytes(msg.data), 0)[0]
        stamp_ns = int(msg.header.stamp.sec) * 1_000_000_000 + int(msg.header.stamp.nanosec)
        rows.append((index, seq, stamp_ns if stamp_ns else int(bag_time_ns)))
        index += 1
    return rows


def print_summary(rows: list[tuple[int, int, int]]) -> None:
    if not rows:
        print("count=0")
        return

    seqs = [seq for _, seq, _ in rows]
    gaps = 0
    regressions = 0
    duplicates = 0
    prev = seqs[0]
    for seq in seqs[1:]:
        if seq > prev + 1:
            gaps += seq - prev - 1
        elif seq == prev:
            duplicates += 1
        elif seq < prev:
            regressions += 1
        prev = seq

    print(f"count={len(seqs)}")
    print(f"first={seqs[0]}")
    print(f"last={seqs[-1]}")
    print(f"min={min(seqs)}")
    print(f"max={max(seqs)}")
    print(f"gaps={gaps}")
    print(f"duplicates={duplicates}")
    print(f"regressions={regressions}")
    print(f"first_stamp_ns={rows[0][2]}")
    print(f"last_stamp_ns={rows[-1][2]}")


def write_csv(path: Path, rows: list[tuple[int, int, int]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["index", "seq", "stamp_ns"])
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bag", type=Path, help="rosbag2 directory or .mcap file")
    parser.add_argument("--topic", default="/points/front")
    parser.add_argument("--csv", type=Path, help="write index,seq,stamp_ns rows")
    parser.add_argument("--list", action="store_true", help="print every sequence")
    args = parser.parse_args()

    rows = extract(args.bag, args.topic)
    print_summary(rows)
    if args.list:
        for _, seq, _ in rows:
            print(seq)
    if args.csv:
        write_csv(args.csv, rows)


if __name__ == "__main__":
    main()
