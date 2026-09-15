#!/usr/bin/env python3
"""Extract Experiment 4 network/drop measurements as CSV.

Outputs:
  results/04_probe_effect/wired_best_effort/analysis/runs.csv
  results/04_probe_effect/wired_best_effort/analysis/summary.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
import sys
sys.path.insert(0, str(REPO_ROOT))
from common import metrics as windows
from common.check_measurements import check as check_measurements
from statistics import mean, stdev



SCENARIOS = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
CONDITIONS = [
    "baseline",
    "rp_hz",
    "topic_hz",
    "rp_bag_one",
    "rp_bag_all",
    "rosbag2_one",
    "rosbag2_all",
]
DELTA_CONDITIONS = ["rp_hz", "topic_hz", "rp_bag_one", "rp_bag_all", "rosbag2_one", "rosbag2_all"]
CONDITION_LABELS = {
    "baseline": "Baseline",
    "rp_hz": "rp topic hz",
    "topic_hz": "ros2 topic hz",
    "rp_bag_one": "rp bag one",
    "rp_bag_all": "rp bag all",
    "rosbag2_one": "ros2 bag one",
    "rosbag2_all": "ros2 bag all",
}

# Payload sizes used only to compute traffic-weighted drop for the composite S7 run.
# They match the paper workload definitions.
S7_PAYLOAD_BYTES = {
    "S7": {
        "s1_sub": 72,
        "s2_sub": 320,
        "s3_points_sub-3": 2_720_000,
        "s3_points_sub-4": 644_000,
        "s4a_sub-5": 150 * 1024,
        "s4a_sub-6": 150 * 1024,
        "s4a_sub-7": 150 * 1024,
        "s4a_sub-8": 150 * 1024,
        "s4_image_sub": 600 * 1024,
    },
}
COLORS = {
    "baseline": "#6b7280",
    "rp_hz": "#1f77b4",
    "topic_hz": "#ff7f0e",
    "rp_bag_one": "#17becf",
    "rp_bag_all": "#0e7490",
    "rosbag2_one": "#d62728",
    "rosbag2_all": "#991b1b",
}


FINAL_RE = re.compile(r"FINAL \[(\d+)s\]: recv (\d+) / expected (\d+) -> drop ([0-9.]+)%")
RECV_RE = re.compile(r"\[INFO\]\s+\[(\d+)\.(\d+)\].*?: recv (\d+) msgs \| rate ([0-9.]+) Hz")
MEASURE_START_RE = re.compile(r"MEASURE_START_MS\s+(\d+)")
MEASURE_END_RE = re.compile(r"MEASURE_END_MS\s+(\d+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def rolling_window_mbps(samples: list[tuple[int, int]], window_ms: int = 60_000) -> float | None:
    values: list[float] = []
    end = 0
    for start, (t0, b0) in enumerate(samples):
        while end < len(samples) and samples[end][0] - t0 < window_ms:
            end += 1
        if end >= len(samples):
            break
        t1, b1 = samples[end]
        dt_s = (t1 - t0) / 1000.0
        delta_bytes = b1 - b0
        if dt_s > 0 and delta_bytes >= 0:
            values.append(delta_bytes * 8.0 / dt_s / 1e6)
    if not values:
        return None
    return mean(values)


def infer_measure_duration_ms(path: Path) -> int:
    if path.exists():
        for raw_line in path.read_text(errors="ignore").splitlines():
            line = ANSI_RE.sub("", raw_line)
            match = FINAL_RE.search(line)
            if match:
                return int(match.group(1)) * 1000
    return 30_000


def infer_subscriber_cutoff_ms(path: Path) -> float | None:
    if not path.exists():
        return None
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = ANSI_RE.sub("", raw_line)
        match = RECV_RE.search(line)
        if not match:
            continue
        sec = int(match.group(1))
        nsec = int(match.group(2))
        recv = int(match.group(3))
        rate_hz = float(match.group(4))
        if rate_hz <= 0:
            continue
        log_ms = sec * 1000 + nsec / 1_000_000
        first_msg_ms = log_ms - (recv / rate_hz * 1000)
        return first_msg_ms + infer_measure_duration_ms(path)
    return None


def parse_measure_interval_ms(path: Path) -> tuple[int, int] | None:
    if not path.exists():
        return None
    start_ms: int | None = None
    end_ms: int | None = None
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = ANSI_RE.sub("", raw_line)
        if start_ms is None:
            match = MEASURE_START_RE.search(line)
            if match:
                start_ms = int(match.group(1))
        if end_ms is None:
            match = MEASURE_END_RE.search(line)
            if match:
                end_ms = int(match.group(1))
        if start_ms is not None and end_ms is not None:
            break
    if start_ms is None or end_ms is None or end_ms <= start_ms:
        return None
    return start_ms, end_ms


def interpolate_bytes(samples: list[tuple[int, int]], timestamp_ms: float) -> float | None:
    if not samples:
        return None
    if timestamp_ms < samples[0][0] or timestamp_ms > samples[-1][0]:
        return None
    if timestamp_ms == samples[0][0]:
        return float(samples[0][1])
    for (t0, b0), (t1, b1) in zip(samples, samples[1:]):
        if t0 <= timestamp_ms <= t1:
            if t1 == t0:
                return float(b1)
            frac = (timestamp_ms - t0) / (t1 - t0)
            return b0 + (b1 - b0) * frac
    return None


def parse_netdev(
    path: Path,
    start_ms: float | None = None,
    end_ms: float | None = None,
) -> tuple[int, float | None, float | None, int | None, float | None]:
    samples: list[tuple[int, int]] = []
    if not path.exists():
        return 0, None, None, None, None
    for line in path.read_text(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            samples.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    if len(samples) < 2:
        return len(samples), None, None, None, None
    start = float(start_ms) if start_ms is not None else float(samples[0][0])
    end = float(end_ms) if end_ms is not None and end_ms > start else float(samples[-1][0])
    start_bytes = interpolate_bytes(samples, start)
    end_bytes = interpolate_bytes(samples, end)
    if start_bytes is None or end_bytes is None:
        return len(samples), None, None, None, rolling_window_mbps(samples)
    duration_s = (end - start) / 1000.0
    delta_bytes_f = end_bytes - start_bytes
    delta_bytes = int(round(delta_bytes_f))
    if duration_s <= 0 or delta_bytes < 0:
        return len(samples), None, duration_s, delta_bytes, None
    return (
        len(samples),
        delta_bytes_f * 8.0 / duration_s / 1e6,
        duration_s,
        delta_bytes,
        rolling_window_mbps(samples),
    )


def final_label(line: str) -> str:
    prefix = line.split("FINAL", 1)[0].strip()
    prefix = ANSI_RE.sub("", prefix)
    prefix = prefix.strip("[] ")
    return prefix or "single"


def payload_for_final(scenario: str, label: str) -> int | None:
    payloads = S7_PAYLOAD_BYTES.get(scenario)
    if not payloads:
        return None
    for prefix, payload_bytes in payloads.items():
        if label.startswith(prefix):
            return payload_bytes
    return None


def weighted_drop_pct(scenario: str, finals: list[dict[str, object]]) -> float | None:
    if scenario not in S7_PAYLOAD_BYTES:
        return None
    lost_weighted = 0.0
    total_weighted = 0.0
    for final in finals:
        payload = payload_for_final(scenario, str(final["label"]))
        if payload is None:
            continue
        expected = int(final["expected"])
        recv = int(final["recv"])
        lost_weighted += max(expected - recv, 0) * payload
        total_weighted += expected * payload
    if total_weighted <= 0:
        return None
    return lost_weighted / total_weighted * 100.0


def parse_sub_log(path: Path, scenario: str) -> dict[str, object]:
    finals: list[dict[str, object]] = []
    if path.exists():
        for raw_line in path.read_text(errors="ignore").splitlines():
            line = ANSI_RE.sub("", raw_line)
            match = FINAL_RE.search(line)
            if not match:
                continue
            finals.append(
                {
                    "label": final_label(line),
                    "recv": int(match.group(2)),
                    "expected": int(match.group(3)),
                    "drop_pct": 100 * max(int(match.group(3)) - int(match.group(2)), 0) / int(match.group(3)),
                }
            )
    if not finals:
        return {
            "recv": None,
            "expected": None,
            "drop_pct": None,
            "plot_drop_pct": None,
            "weighted_drop_pct": None,
            "drop_label": "",
            "final_count": 0,
            "max_drop_pct": None,
        }

    selected = finals[0]
    if scenario == "S7":
        selected = next((f for f in finals if str(f["label"]).startswith("s3_points_sub-3")), finals[0])

    weighted = weighted_drop_pct(scenario, finals)
    plot_drop = weighted if weighted is not None else selected["drop_pct"]

    return {
        "recv": selected["recv"],
        "expected": selected["expected"],
        "drop_pct": selected["drop_pct"],
        "plot_drop_pct": plot_drop,
        "weighted_drop_pct": weighted,
        "drop_label": selected["label"],
        "final_count": len(finals),
        "max_drop_pct": max(float(f["drop_pct"]) for f in finals),
    }


def parse_cpu_mem(path: Path) -> dict[str, float | int | None]:
    cpu: list[float] = []
    mem: list[int] = []
    if path.exists():
        for line in path.read_text(errors="ignore").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                cpu.append(float(parts[1]))
                mem.append(int(parts[2]))
            except ValueError:
                continue
    if not cpu:
        return {
            "cpu_mem_samples": 0,
            "cpu_avg": None,
            "cpu_max": None,
            "mem_avg_kb": None,
            "mem_max_kb": None,
        }
    return {
        "cpu_mem_samples": len(cpu),
        "cpu_avg": mean(cpu),
        "cpu_max": max(cpu),
        "mem_avg_kb": mean(mem),
        "mem_max_kb": max(mem),
    }


def classify(row: dict[str, object]) -> str:
    if row["drop_pct"] is None:
        return "invalid_missing_final"
    if int(row["netdev_samples"]) < 2 or row["rx_mbps"] is None:
        return "invalid_missing_netdev"
    if int(row["netdev_samples"]) < 25:
        return "suspect_low_netdev_samples"
    return "valid"


def collect_runs(results_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    windows.require_complete_runs(results_dir)
    for scenario in SCENARIOS:
        scenario_dir = results_dir / scenario
        if not scenario_dir.exists():
            continue
        for condition in CONDITIONS:
            condition_dir = scenario_dir / condition
            if not condition_dir.exists():
                continue
            for run_dir in sorted(condition_dir.glob("run*")):
                if not run_dir.is_dir():
                    continue
                protocol = windows.require_run_metadata(run_dir)
                check_measurements(run_dir)
                sub_log_path = run_dir / "sub.log"
                if scenario == "S7":
                    sub_log_path = run_dir / "sub_points_front.log"
                measure_start_ms, measure_end_ms = windows.interval(sub_log_path)
                netdev_samples, rx_mbps, netdev_duration_s, rx_delta_bytes = windows.bandwidth(run_dir / "netdev.log", measure_start_ms, measure_end_ms)
                sub = parse_sub_log(sub_log_path, scenario)
                cpu_mem = parse_cpu_mem(run_dir / "cpu_mem.log")
                row: dict[str, object] = {
                    "protocol": protocol,
                    "execution_profile": (run_dir / 'execution_profile.txt').read_text().strip() if (run_dir / 'execution_profile.txt').exists() else 'unspecified',
                    "link": results_dir.name.split("_", 1)[0],
                    "qos": results_dir.name.split("_", 1)[1] if "_" in results_dir.name else "",
                    "scenario": scenario,
                    "condition": condition,
                    "run": run_dir.name,
                    "path": str(run_dir),
                    "recv": sub["recv"],
                    "expected": sub["expected"],
                    "drop_pct": sub["drop_pct"],
                    "plot_drop_pct": sub["plot_drop_pct"],
                    "weighted_drop_pct": sub["weighted_drop_pct"],
                    "drop_label": sub["drop_label"],
                    "final_count": sub["final_count"],
                    "max_drop_pct": sub["max_drop_pct"],
                    "measure_start_ms": measure_start_ms,
                    "measure_end_ms": measure_end_ms,
                    "netdev_samples": netdev_samples,
                    "netdev_duration_s": netdev_duration_s,
                    "rx_delta_bytes": rx_delta_bytes,
                    "rx_mbps": rx_mbps,
                    **cpu_mem,
                }
                row["validity"] = classify(row)
                rows.append(row)
    add_delta_rx(rows)
    return rows


def add_delta_rx(rows: list[dict[str, object]]) -> None:
    baseline: dict[tuple[str, str], float] = {}
    by_scenario_run: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        if row["condition"] == "baseline" and row["rx_mbps"] is not None:
            by_scenario_run[(str(row["scenario"]), str(row["run"]))].append(float(row["rx_mbps"]))
    for key, values in by_scenario_run.items():
        baseline[key] = mean(values)

    baseline_mean: dict[str, float] = {}
    for scenario in SCENARIOS:
        values = [
            float(row["rx_mbps"])
            for row in rows
            if row["scenario"] == scenario and row["condition"] == "baseline" and row["rx_mbps"] is not None
        ]
        if values:
            baseline_mean[scenario] = mean(values)

    for row in rows:
        rx = row["rx_mbps"]
        if rx is None:
            row["delta_rx_mbps"] = None
            continue
        same_run = baseline.get((str(row["scenario"]), str(row["run"])))
        base = same_run if same_run is not None else baseline_mean.get(str(row["scenario"]))
        row["delta_rx_mbps"] = float(rx) - base if base is not None else None


def csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}"
    return value


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})


def summarize(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        if str(row.get("validity", "")).startswith("invalid"):
            continue
        grouped[(str(row["scenario"]), str(row["condition"]))].append(row)

    metrics = [
        "rx_mbps",
        "delta_rx_mbps",
        "drop_pct",
        "plot_drop_pct",
        "weighted_drop_pct",
        "max_drop_pct",
    ]
    out_rows: list[dict[str, object]] = []
    for scenario, condition in sorted(grouped):
        group = grouped[(scenario, condition)]
        out: dict[str, object] = {
            "scenario": scenario,
            "condition": condition,
            "n_runs": len(group),
            "validity_values": ";".join(sorted({str(row["validity"]) for row in group})),
        }
        for metric in metrics:
            values = [float(row[metric]) for row in group if row.get(metric) is not None]
            out[f"{metric}_mean"] = mean(values) if values else None
            out[f"{metric}_std"] = stdev(values) if len(values) > 1 else None
            out[f"{metric}_n"] = len(values)
            out[f"{metric}_min"] = min(values) if values else None
            out[f"{metric}_max"] = max(values) if values else None
        out_rows.append(out)
    return out_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", default=REPO_ROOT / "results/04_probe_effect/wired_best_effort", type=Path)
    parser.add_argument("--out-dir", default=None, type=Path)
    args = parser.parse_args()

    out_dir = args.out_dir or args.results_dir / "analysis"
    rows = collect_runs(args.results_dir)
    if not rows:
        raise SystemExit(f"No Experiment 4 runs found under {args.results_dir}")
    topic_rows = []
    for row in rows:
        for log in Path(row['path']).glob('sub*.log'):
            for match in re.finditer(r'TOPIC_RESULT topic=(\S+) recv=(\d+) expected=(\d+) duration=(\d+)', log.read_text()):
                topic, recv, expected, duration = match.groups()
                topic_rows.append({'scenario': row['scenario'], 'condition': row['condition'],
                                   'run': row['run'], 'topic': topic, 'recv': int(recv),
                                   'expected': int(expected), 'duration_s': int(duration),
                                   'drop_pct': 100 * max(int(expected) - int(recv), 0) / int(expected)})
    write_csv(out_dir / "topic_results.csv", topic_rows)
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
