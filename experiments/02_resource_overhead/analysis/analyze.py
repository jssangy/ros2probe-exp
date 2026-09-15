#!/usr/bin/env python3
"""Extract Experiment 2 CPU, PSS and subscriber measurements as CSV.

Outputs:
  results/02_resource_overhead/rate/analysis/runs.csv
  results/02_resource_overhead/rate/analysis/summary.csv
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
import sys
sys.path.insert(0, str(REPO_ROOT))
from common import metrics as windows
from common.check_measurements import check as check_measurements
from statistics import mean, stdev



SCENARIOS = ["ST100", "ST500", "ST1000"]
CONDITIONS = ["baseline", "rp_hz", "topic_hz", "rp_bag", "rosbag2"]
HZ_CONDITIONS = ["baseline", "rp_hz", "topic_hz"]
BAG_CONDITIONS = ["rp_bag", "rosbag2"]
OBSERVER_CONDITIONS = ["rp_hz", "topic_hz", "rp_bag", "rosbag2"]
PLATFORM_ORDER = ["pc", "jetson", "rpi"]
CONDITION_LABELS = {
    "baseline": "Baseline",
    "rp_hz": "rp topic hz",
    "topic_hz": "ros2 topic hz",
    "rp_bag": "rp bag",
    "rosbag2": "ros2 bag",
}
COLORS = {
    "baseline": "#6b7280",
    "rp_hz": "#1f77b4",
    "topic_hz": "#d62728",
    "rp_bag": "#17becf",
    "rosbag2": "#ff7f0e",
}


def parse_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(errors="ignore").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def parse_sub_log(path: Path) -> tuple[int | None, int | None, float | None]:
    if not path.exists():
        return None, None, None
    text = path.read_text(errors="ignore")
    match = re.search(
        r"FINAL \[\d+s\]: recv (\d+) / expected (\d+) -> drop ([0-9.]+)%",
        text,
    )
    if not match:
        return None, None, None
    recv, expected = int(match.group(1)), int(match.group(2))
    return recv, expected, 100 * max(expected - recv, 0) / expected if expected else None


def parse_netdev(path: Path) -> tuple[int, float | None, float | None, int | None]:
    samples: list[tuple[int, int]] = []
    if not path.exists():
        return 0, None, None, None
    for line in path.read_text(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            samples.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    if len(samples) < 2:
        return len(samples), None, None, None
    duration_s = (samples[-1][0] - samples[0][0]) / 1000.0
    delta_bytes = samples[-1][1] - samples[0][1]
    if duration_s <= 0 or delta_bytes < 0:
        return len(samples), None, duration_s, delta_bytes
    rx_mbps = delta_bytes * 8.0 / duration_s / 1e6
    return len(samples), rx_mbps, duration_s, delta_bytes


def parse_observer_log(path: Path, start=None, end=None) -> dict[str, float | int | None]:
    cpu: list[float] = []
    mem: list[int] = []
    if path.exists():
        for line in path.read_text(errors="ignore").splitlines():
            if line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                if start is not None and not start <= float(parts[0]) <= end:
                    continue
                cpu.append(float(parts[1]))
                mem.append(int(parts[2]))
            except ValueError:
                continue
    if not cpu:
        return {
            "observer_samples": 0,
            "observer_cpu_avg": None,
            "observer_cpu_max": None,
            "observer_mem_avg_kb": None,
            "observer_mem_max_kb": None,
        }
    return {
        "observer_samples": len(cpu),
        "observer_cpu_avg": mean(cpu),
        "observer_cpu_max": max(cpu),
        "observer_mem_avg_kb": mean(mem),
        "observer_mem_max_kb": max(mem),
    }


def parse_recorder_cache_loss(path: Path) -> int | None:
    if not path.exists():
        return None
    text = path.read_text(errors="ignore")
    match = re.search(r"Total lost:\s*(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def classify_validity(
    platform: str,
    scenario: str,
    condition: str,
    drop_pct: float | None,
    netdev_samples: int,
    rx_mbps: float | None,
) -> str:
    if drop_pct is None:
        return "invalid_missing_final"
    if netdev_samples < 2 or rx_mbps is None:
        return "invalid_missing_netdev"
    if netdev_samples < 50:
        return "suspect_low_netdev_samples"
    if drop_pct > 1.0:
        return "valid_with_subscriber_loss"
    return "valid"


def collect_condition_runs(results_dir: Path, conditions: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    windows.require_complete_runs(results_dir)
    if not results_dir.exists():
        return rows
    for platform_dir in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        if platform_dir.name == "analysis":
            continue
        platform = platform_dir.name
        for scenario in SCENARIOS:
            for condition in conditions:
                condition_dir = platform_dir / scenario / condition
                if not condition_dir.exists():
                    continue
                for run_dir in sorted(condition_dir.glob("run*")):
                    if not run_dir.is_dir():
                        continue
                    protocol = windows.require_run_metadata(run_dir)
                    check_measurements(run_dir)
                    platform_values = parse_key_values(run_dir / "platform.log")
                    recv, expected, drop_pct = parse_sub_log(run_dir / "sub.log")
                    start, end = windows.interval(run_dir / "sub.log")
                    netdev_samples, rx_mbps, netdev_duration_s, rx_delta_bytes = windows.bandwidth(run_dir / "netdev.log", start, end)
                    observer = windows.resource_usage(run_dir / "observer_cpu.log", start, end, condition == "baseline")
                    logical_cores = int(platform_values.get("logical_cores", "0") or 0)
                    cpu_avg = observer["observer_cpu_avg"]
                    cpu_max = observer["observer_cpu_max"]
                    cache_lost = (
                        parse_recorder_cache_loss(run_dir / "obs.log")
                        if condition in BAG_CONDITIONS
                        else None
                    )
                    row: dict[str, object] = {
                        "protocol": protocol,
                        "execution_profile": (run_dir / 'execution_profile.txt').read_text().strip() if (run_dir / 'execution_profile.txt').exists() else 'unspecified',
                        "measure_start_ms": start,
                        "measure_end_ms": end,
                        "platform": platform,
                        "scenario": scenario,
                        "condition": condition,
                        "run": run_dir.name,
                        "path": str(run_dir),
                        "date": platform_values.get("date", ""),
                        "hz": int(platform_values.get("hz", "0") or 0),
                        "payload_bytes": int(platform_values.get("payload_bytes", "0") or 0),
                        "logical_cores": logical_cores,
                        "nic": platform_values.get("nic", ""),
                        "cpu0_governor": platform_values.get("cpu0_governor", ""),
                        "recv": recv,
                        "expected": expected,
                        "drop_pct": drop_pct,
                        "netdev_samples": netdev_samples,
                        "netdev_duration_s": netdev_duration_s,
                        "rx_delta_bytes": rx_delta_bytes,
                        "rx_mbps": rx_mbps,
                        **observer,
                        "observer_cpu_norm_avg": (
                            cpu_avg / logical_cores if cpu_avg is not None and logical_cores else None
                        ),
                        "observer_cpu_norm_max": (
                            cpu_max / logical_cores if cpu_max is not None and logical_cores else None
                        ),
                        "recorder_cache_lost_msgs": cache_lost,
                    }
                    row["validity"] = classify_validity(
                        platform, scenario, condition, drop_pct, netdev_samples, rx_mbps
                    )
                    if condition != "baseline" and (not logical_cores or not observer["observer_samples"] or not observer["observer_mem_avg_kb"]):
                        row["validity"] = "invalid_missing_observer_samples"
                    if not str(row["validity"]).startswith("invalid") and condition in BAG_CONDITIONS and cache_lost and cache_lost > 0:
                        row["validity"] = "valid_with_recorder_cache_loss"
                    rows.append(row)
    return rows


def collect_runs(results_dir: Path, bag_results_dir: Path | None = None) -> list[dict[str, object]]:
    rows = collect_condition_runs(results_dir, HZ_CONDITIONS)
    if bag_results_dir is not None:
        rows.extend(collect_condition_runs(bag_results_dir, BAG_CONDITIONS))
    return rows


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
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        if str(row.get("validity", "")).startswith("invalid"):
            continue
        grouped[(str(row["platform"]), str(row["scenario"]), str(row["condition"]))].append(row)

    summary_rows: list[dict[str, object]] = []
    metrics = [
        "rx_mbps",
        "drop_pct",
        "observer_cpu_avg",
        "observer_cpu_max",
        "observer_cpu_norm_avg",
        "observer_mem_avg_kb",
        "observer_mem_max_kb",
        "recorder_cache_lost_msgs",
    ]
    for key in sorted(grouped):
        platform, scenario, condition = key
        group = grouped[key]
        out: dict[str, object] = {
            "platform": platform,
            "scenario": scenario,
            "condition": condition,
            "n_runs": len(group),
            "validity_values": ";".join(sorted({str(row["validity"]) for row in group})),
        }
        for metric in metrics:
            values = [float(row[metric]) for row in group if row.get(metric) is not None]
            out[f"{metric}_n"] = len(values)
            if values:
                out[f"{metric}_mean"] = mean(values)
                out[f"{metric}_std"] = stdev(values) if len(values) > 1 else None
                out[f"{metric}_min"] = min(values)
                out[f"{metric}_max"] = max(values)
            else:
                out[f"{metric}_mean"] = None
                out[f"{metric}_std"] = None
                out[f"{metric}_min"] = None
                out[f"{metric}_max"] = None
        summary_rows.append(out)
    return summary_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", default=REPO_ROOT / "results/02_resource_overhead/rate", type=Path)
    parser.add_argument("--bag-results-dir", default=REPO_ROOT / "results/02_resource_overhead/recording", type=Path)
    parser.add_argument("--out-dir", default=None, type=Path)
    args = parser.parse_args()

    results_dir = args.results_dir
    out_dir = args.out_dir or results_dir / "analysis"

    rows = collect_runs(results_dir, args.bag_results_dir)
    if not rows:
        raise SystemExit(f"No Experiment 2 runs found under {results_dir}")

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
