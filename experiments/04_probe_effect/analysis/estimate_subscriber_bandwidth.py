#!/usr/bin/env python3
"""Estimate Experiment 4 received payload bandwidth from subscriber logs.

This is a fallback for runs with missing/empty netdev.log. It reports
application payload Mbps from sub.log FINAL lines, not NIC RX Mbps.
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


SCENARIOS = {"S1", "S2", "S3", "S4", "S5", "S6", "S7"}
CONDITIONS = {
    "baseline",
    "rp_hz",
    "topic_hz",
    "rp_bag_one",
    "rp_bag_all",
    "rosbag2_one",
    "rosbag2_all",
}

# Payload bytes from the current exp1 workload implementation.
SCENARIO_PAYLOAD_BYTES = {
    "S1": 320,              # /imu, documented workload payload
    "S2": 1080 * 4,         # /scan ranges
    "S3": 30_000 * 22,      # 16ch PointCloud2 data
    "S4": 130_000 * 22,     # 64ch PointCloud2 data
    "S5": 150 * 1024,       # compressed image payload
    "S6": 640 * 480 * 2,    # 16UC1 depth image data
}

S7_PAYLOAD_BYTES = {
    "s1_sub": 72,               # /cmd_vel background topic
    "s2_sub": 320,              # /imu
    "s3_points_sub-3": 30_000 * 22,   # /points/rear; /points/front is sub_points_front.log
    "s4a_sub-4": 150 * 1024,    # /camera/front/compressed
    "s4a_sub-5": 150 * 1024,    # /camera/left/compressed
    "s4a_sub-6": 150 * 1024,    # /camera/right/compressed
    "s4a_sub-7": 150 * 1024,    # /camera/rear/compressed
    "s4_image_sub": 640 * 480 * 2,    # /depth/image_raw
    "sub_points_front": 130_000 * 22,  # /points/front
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
FINAL_RE = re.compile(r"FINAL \[(\d+)s\]: recv (\d+) / expected (\d+) -> drop ([0-9.]+)%")
RUN_RE = re.compile(r"run(\d+)$")


def clean(line: str) -> str:
    return ANSI_RE.sub("", line)


def final_label(line: str, path: Path) -> str:
    if path.name == "sub_points_front.log":
        return "sub_points_front"
    prefix = clean(line).split("FINAL", 1)[0].strip().strip("[] ")
    return prefix or "single"


def infer_run_info(path: Path) -> tuple[str, str, str] | None:
    parts = path.parts
    scenario = condition = run = None
    for i, part in enumerate(parts):
        if part in SCENARIOS:
            scenario = part
            if i + 2 < len(parts):
                condition = parts[i + 1]
                run_part = parts[i + 2]
                match = RUN_RE.match(run_part)
                if match:
                    run = match.group(1)
            break
    if scenario and condition in CONDITIONS and run:
        return scenario, condition, run
    return None


def payload_for(scenario: str, label: str) -> int | None:
    if scenario != "S7":
        return SCENARIO_PAYLOAD_BYTES.get(scenario)
    for prefix, payload_bytes in S7_PAYLOAD_BYTES.items():
        if label.startswith(prefix):
            return payload_bytes
    return None


def parse_log(path: Path, scenario: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in path.read_text(errors="ignore").splitlines():
        line = clean(raw)
        match = FINAL_RE.search(line)
        if not match:
            continue
        label = final_label(line, path)
        payload_bytes = payload_for(scenario, label)
        duration_s = int(match.group(1))
        recv = int(match.group(2))
        expected = int(match.group(3))
        drop_pct = float(match.group(4))
        mbps = None
        if payload_bytes is not None and duration_s > 0:
            mbps = recv * payload_bytes * 8.0 / duration_s / 1e6
        rows.append(
            {
                "source_log": str(path),
                "label": label,
                "duration_s": duration_s,
                "recv": recv,
                "expected": expected,
                "drop_pct": drop_pct,
                "payload_bytes": payload_bytes,
                "payload_mbps": mbps,
            }
        )
    return rows


def collect(root: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    detail_rows: list[dict[str, object]] = []
    for path in sorted(root.rglob("sub*.log")):
        info = infer_run_info(path)
        if info is None:
            continue
        scenario, condition, run = info
        for row in parse_log(path, scenario):
            detail_rows.append({"scenario": scenario, "condition": condition, "run": run, **row})

    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for row in detail_rows:
        key = (str(row["scenario"]), str(row["condition"]), str(row["run"]))
        grouped.setdefault(key, []).append(row)

    run_rows: list[dict[str, object]] = []
    for (scenario, condition, run), rows in sorted(grouped.items()):
        known = [float(r["payload_mbps"]) for r in rows if r["payload_mbps"] is not None]
        run_rows.append(
            {
                "scenario": scenario,
                "condition": condition,
                "run": run,
                "final_count": len(rows),
                "payload_mbps": sum(known) if known else None,
                "unknown_payload_count": len(rows) - len(known),
                "recv_total": sum(int(r["recv"]) for r in rows),
                "expected_total": sum(int(r["expected"]) for r in rows),
            }
        )
    return run_rows, detail_rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=REPO_ROOT / "results/04_probe_effect", type=Path)
    parser.add_argument("--out-dir", default=REPO_ROOT / "results/04_probe_effect/analysis", type=Path)
    args = parser.parse_args()

    run_rows, detail_rows = collect(args.root)
    write_csv(args.out_dir / "subscriber_payload_bandwidth_runs.csv", run_rows)
    write_csv(args.out_dir / "subscriber_payload_bandwidth_details.csv", detail_rows)
    print(f"Wrote {len(run_rows)} run rows to {args.out_dir / 'subscriber_payload_bandwidth_runs.csv'}")
    print(f"Wrote {len(detail_rows)} detail rows to {args.out_dir / 'subscriber_payload_bandwidth_details.csv'}")


if __name__ == "__main__":
    main()
