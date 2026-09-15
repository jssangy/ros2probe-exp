#!/usr/bin/env python3
"""
Build discovery discovery count CSVs from per-run tshark CSV files.

The output schema intentionally matches the existing figure_metrics CSVs, but
SPDP/SEDP/control counts are submessage counts, not packet-row counts.
"""

import argparse
import json
from pathlib import Path
import csv
import os
import re
import statistics
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from common.metrics import require_complete_runs, require_run_metadata
from common.discovery import PROTOCOL, paired_window, read_run


SPDP_WRITER = "0x000100c2"
SEDP_WRITERS = {"0x000003c2", "0x000004c2"}
DISCOVERY_CONTROL_ENTITIES = {
    "0x000003c2", "0x000003c7",
    "0x000004c2", "0x000004c7",
    "0x000200c2", "0x000200c7",
}
DATA_SM = "0x15"
HEARTBEAT_SM = "0x07"
ACKNACK_SM = "0x06"
WRITER_BEARING_SMS = {DATA_SM, HEARTBEAT_SM, ACKNACK_SM, "0x08", "0x12", "0x13", "0x16"}


def _split_field(value: str) -> List[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _parse_run_path(path: str) -> Optional[Tuple[str, str, str, int]]:
    parts = os.path.normpath(path).split(os.sep)
    if len(parts) < 4:
        return None
    run_name = parts[-1]
    condition = parts[-2]
    scale = parts[-3]
    root = parts[-4]
    m = re.fullmatch(r"run_(\d+)", run_name)
    if not m:
        return None
    return root, scale, condition, int(m.group(1))


def _parse_n_p(csv_name: str) -> Optional[int]:
    m = re.search(r"_N_P_(\d+)_N_E_\d+\.csv$", csv_name)
    if not m:
        return None
    return int(m.group(1))


def _iter_writer_data_entities(sm_ids: Sequence[str], writer_entities: Sequence[str]) -> Iterable[str]:
    """Yield writer entity IDs for DATA submessages.

    tshark emits repeated field values as comma-separated lists. Fields from
    submessages without writer IDs, such as INFO_DST/INFO_TS, are omitted from
    rtps.sm.wrEntityId, so align writer IDs only with writer-bearing submessages.
    """
    writer_index = 0
    for sm_id in sm_ids:
        if sm_id not in WRITER_BEARING_SMS:
            continue
        writer = writer_entities[writer_index] if writer_index < len(writer_entities) else ""
        writer_index += 1
        if sm_id == DATA_SM and writer:
            yield writer


def _load_completion_times(base_dir: str) -> Dict[str, float]:
    path = os.path.join(base_dir, "analysis_results1_results2_discovery_time", "per_run_discovery_time.csv")
    completion_times: Dict[str, float] = {}
    if not os.path.isfile(path):
        return completion_times

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            run_dir = row.get("run_dir", "")
            matched_time = row.get("matched_time", "")
            try:
                completion_times[os.path.abspath(os.path.join(os.getcwd(), run_dir))] = float(matched_time)
            except (TypeError, ValueError):
                continue
    return completion_times


def _timestamp(value: object, context: str) -> Decimal:
    """Keep the decimal precision of pcap epoch timestamps at interval edges."""
    try:
        timestamp = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Missing/invalid timestamp ({context}): {value!r}") from exc
    if not timestamp.is_finite():
        raise ValueError(f"Nonfinite timestamp ({context}): {value!r}")
    return timestamp


def _measurement_window(start: object, end: object, context: str) -> Tuple[Decimal, Decimal]:
    if start is None or end is None:
        raise ValueError(f"Explicit measurement start and end are required: {context}")
    start_time = _timestamp(start, context + " start")
    end_time = _timestamp(end, context + " end")
    if end_time <= start_time:
        raise ValueError(f"Measurement end must follow start: {context}")
    return start_time, end_time


def _count_run(csv_path: str, completion_time: object, start_time: object = None) -> Dict[str, object]:
    start, end = _measurement_window(start_time, completion_time, csv_path)
    spdp_count = 0
    sedp_pub_count = 0
    sedp_sub_count = 0
    control_count = 0
    participant_guids = set()
    rtps_packet_count = 0
    discovery_packet_count = 0
    first_discovery_time = None
    last_discovery_time = None

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            timestamp = _timestamp(row.get("frame.time_epoch"),
                                   f"{csv_path} frame {row.get('frame.number')}")
            if not start <= timestamp < end:
                continue

            src = row.get("ip.src", "")
            dst = row.get("ip.dst", "")
            if "127." in src or "127." in dst:
                continue

            rtps_packet_count += 1
            messages_before = spdp_count + sedp_pub_count + sedp_sub_count + control_count

            sm_ids = _split_field(row.get("rtps.sm.id", ""))
            writer_entities = _split_field(row.get("rtps.sm.wrEntityId", ""))
            reader_entities = _split_field(row.get("rtps.sm.rdEntityId", ""))

            expected_entities = sum(sm in WRITER_BEARING_SMS for sm in sm_ids)
            if len(writer_entities) != expected_entities or len(reader_entities) != expected_entities:
                raise ValueError(f"Ambiguous RTPS field alignment at frame {row.get('frame.number')}: {csv_path}")
            writer_index = 0
            reader_index = 0
            for sm_id in sm_ids:
                if sm_id in WRITER_BEARING_SMS:
                    writer = writer_entities[writer_index] if writer_index < len(writer_entities) else ""
                    writer_index += 1
                else:
                    writer = ""

                if sm_id == ACKNACK_SM:
                    reader = reader_entities[reader_index] if reader_index < len(reader_entities) else ""
                    reader_index += 1
                elif sm_id in WRITER_BEARING_SMS:
                    reader = reader_entities[reader_index] if reader_index < len(reader_entities) else ""
                    reader_index += 1
                else:
                    reader = ""

                if sm_id in {HEARTBEAT_SM, ACKNACK_SM} and (
                    writer in DISCOVERY_CONTROL_ENTITIES or reader in DISCOVERY_CONTROL_ENTITIES
                ):
                    control_count += 1

            for writer in _iter_writer_data_entities(sm_ids, writer_entities):
                if writer == SPDP_WRITER:
                    spdp_count += 1
                    guid = row.get("rtps.guidPrefix.src", "").strip()
                    if guid:
                        participant_guids.add(guid)
                elif writer == "0x000003c2":
                    sedp_pub_count += 1
                elif writer == "0x000004c2":
                    sedp_sub_count += 1

            if spdp_count + sedp_pub_count + sedp_sub_count + control_count > messages_before:
                discovery_packet_count += 1
                first_discovery_time = timestamp if first_discovery_time is None else min(first_discovery_time, timestamp)
                last_discovery_time = timestamp if last_discovery_time is None else max(last_discovery_time, timestamp)

    sedp_count = sedp_pub_count + sedp_sub_count
    return {
        "participant_count": len(participant_guids),
        "rtps_packet_count": rtps_packet_count,
        "discovery_packet_count": discovery_packet_count,
        "first_discovery_packet_epoch": str(first_discovery_time) if first_discovery_time is not None else None,
        "last_discovery_packet_epoch": str(last_discovery_time) if last_discovery_time is not None else None,
        # Preserve the old column name, but store the classified discovery
        # submessage total under the existing schema.
        "discovery_packets": spdp_count + sedp_count + control_count,
        "spdp_count": spdp_count,
        "sedp_pub_count": sedp_pub_count,
        "sedp_sub_count": sedp_sub_count,
        "sedp_count": sedp_count,
        "control_count": control_count,
    }


def _find_capture_csvs(base_dir: str) -> Iterable[str]:
    for current, _dirs, files in os.walk(base_dir):
        parsed = _parse_run_path(current)
        if not parsed:
            continue
        for name in files:
            if re.fullmatch(r"discovery_capture_N_P_\d+_N_E_\d+\.csv", name):
                yield os.path.join(current, name)


def _stdev(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    return statistics.stdev(values)


def _summarize(rows: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    metrics = ["control_count", "discovery_packets", "participant_count", "sedp_count", "spdp_count"]
    grouped: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["root"]), str(row["scale"]), str(row["condition"]))].append(row)

    summary_rows = []
    for (root, scale, condition), group in sorted(grouped.items()):
        out: Dict[str, object] = {"root": root, "condition": condition}
        for metric in metrics:
            values = [float(row[metric]) for row in group]
            out[f"{metric}_mean"] = statistics.mean(values)
            out[f"{metric}_median"] = statistics.median(values)
            out[f"{metric}_std"] = _stdev(values)
        out["runs"] = len(group)
        out["scale"] = scale
        summary_rows.append(out)
    return summary_rows


def _float_or_none(value: object) -> Optional[float]:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _format_stat(value: float) -> object:
    if value.is_integer():
        return int(value)
    return value


def _rewrite_detailed_metrics(base_dir: str, counts_by_run_dir: Dict[str, Dict[str, int]]) -> None:
    metrics_dir = os.path.join(base_dir, "analysis_results1_results2")
    per_run_path = os.path.join(metrics_dir, "per_run_metrics.csv")
    summary_path = os.path.join(metrics_dir, "summary_metrics.csv")
    if not os.path.isfile(per_run_path):
        return

    with open(per_run_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    for row in rows:
        run_dir = os.path.abspath(os.path.join(os.getcwd(), row.get("run_dir", "")))
        counts = counts_by_run_dir.get(run_dir)
        if not counts:
            continue
        row["packets"] = str(counts["discovery_packets"])
        row["spdp"] = str(counts["spdp_count"])
        row["sedp_pub"] = str(counts["sedp_pub_count"])
        row["sedp_sub"] = str(counts["sedp_sub_count"])
        row["control"] = str(counts["control_count"])
        row["participant_guids"] = str(counts["participant_count"])
        row["spdp_guids"] = str(counts["participant_count"])

    with open(per_run_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    if not os.path.isfile(summary_path):
        print(f"[discovery] Updated {per_run_path} ({len(rows)} rows)")
        return

    with open(summary_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        summary_fields = reader.fieldnames or []

    grouped: Dict[Tuple[str, str, str], List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row.get("root", ""), row.get("scale", ""), row.get("condition", ""))].append(row)

    metric_names = []
    for name in summary_fields:
        suffix = None
        for candidate in ("_max", "_mean", "_median", "_min", "_std"):
            if name.endswith(candidate):
                suffix = candidate
                break
        if suffix:
            metric = name[: -len(suffix)]
            if metric not in metric_names:
                metric_names.append(metric)

    summary_rows: List[Dict[str, object]] = []
    for (root, scale, condition), group in sorted(grouped.items()):
        out: Dict[str, object] = {"condition": condition, "root": root, "runs": len(group), "scale": scale}
        for metric in metric_names:
            values = [_float_or_none(row.get(metric)) for row in group]
            numeric = [value for value in values if value is not None]
            if not numeric:
                continue
            out[f"{metric}_max"] = _format_stat(max(numeric))
            out[f"{metric}_mean"] = statistics.mean(numeric)
            out[f"{metric}_median"] = statistics.median(numeric)
            out[f"{metric}_min"] = _format_stat(min(numeric))
            out[f"{metric}_std"] = _stdev(numeric)
        summary_rows.append(out)

    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"[discovery] Updated {per_run_path} ({len(rows)} rows)")
    print(f"[discovery] Updated {summary_path} ({len(summary_rows)} rows)")


def run_analysis(base_dir: str, output_dir: str, peer_base_dir: str = None) -> int:
    base_dir = os.path.abspath(base_dir)
    output_dir = os.path.abspath(output_dir)
    rows: List[Dict[str, object]] = []
    counts_by_run_dir: Dict[str, Dict[str, int]] = {}
    windows = []
    require_complete_runs(base_dir)
    peers = {}
    if peer_base_dir:
        require_complete_runs(peer_base_dir)
        for path in _find_capture_csvs(peer_base_dir):
            _root, scale, condition, rep = _parse_run_path(os.path.dirname(path))
            key = (scale, condition, rep)
            if key in peers:
                raise ValueError(f'Duplicate peer discovery trial: {key}')
            peers[key] = os.path.dirname(path)

    for csv_path in sorted(_find_capture_csvs(base_dir)):
        parsed = _parse_run_path(os.path.dirname(csv_path))
        n_p = _parse_n_p(os.path.basename(csv_path))
        if parsed is None or n_p is None:
            continue
        root, scale, condition, rep = parsed
        run_dir = os.path.abspath(os.path.dirname(csv_path))
        metadata, _start, _deadline, _times = read_run(run_dir)
        if metadata['N_P'] != n_p or scale != f'G{n_p}' or metadata['condition'] != condition:
            raise ValueError(f'Discovery path/metadata mismatch: {run_dir}')
        peer = peers.get((scale, condition, rep))
        if peer is None:
            raise ValueError(f'Missing peer matching evidence; supply --peer-base-dir: {run_dir}')
        window = paired_window(run_dir, peer)
        start, end = _measurement_window(window['measure_start'], window['measure_end'], run_dir)
        windows.append(dict(root=root, scale=scale, condition=condition, rep=rep,
                            source_run=run_dir, peer_run=peer, **window))
        counts = _count_run(csv_path, end, start)
        if counts["discovery_packets"] == 0:
            raise ValueError(f"No discovery messages in the measurement window: {run_dir}")
        counts_by_run_dir[run_dir] = counts
        rows.append({
            **{key: value for key, value in window.items() if not key.endswith('participant_completion')},
            "total_processes": metadata.get("total_processes"),
            "measure_start": str(start),
            "measure_end": str(end),
            "root": root,
            "scale": scale,
            "condition": condition,
            "rep": rep,
            "N_P": n_p,
            **counts,
        })

    if not rows:
        print(f"[discovery] No discovery_capture_N_P_*_N_E_*.csv files found under {base_dir}", file=sys.stderr)
        return 1

    os.makedirs(output_dir, exist_ok=True)
    per_run_path = os.path.join(output_dir, "runs.csv")
    per_run_fields = [
        "protocol", "capture_protocol", "total_processes", "measure_start", "measure_end",
        "measurement_end_basis", "discovery_duration_ms", "local_discovery_complete", "peer_discovery_complete",
        "root", "scale", "condition", "rep", "N_P",
        "rtps_packet_count", "discovery_packet_count",
        "first_discovery_packet_epoch", "last_discovery_packet_epoch",
        "participant_count", "discovery_packets", "spdp_count", "sedp_count", "control_count",
    ]
    with open(per_run_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=per_run_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    (Path(output_dir) / 'measurement_windows.json').write_text(json.dumps(windows, indent=2) + '\n')

    summary_rows = _summarize(rows)
    summary_path = os.path.join(output_dir, "summary.csv")
    summary_fields = [
        "root", "condition",
        "control_count_mean", "control_count_median", "control_count_std",
        "discovery_packets_mean", "discovery_packets_median", "discovery_packets_std",
        "participant_count_mean", "participant_count_median", "participant_count_std",
        "runs", "scale",
        "sedp_count_mean", "sedp_count_median", "sedp_count_std",
        "spdp_count_mean", "spdp_count_median", "spdp_count_std",
    ]
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"[discovery] Wrote {per_run_path} ({len(rows)} rows)")
    print(f"[discovery] Wrote {summary_path} ({len(summary_rows)} rows)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate discovery discovery metrics with submessage-based SPDP/SEDP/control counts."
    )
    parser.add_argument(
        "--base-dir",
        default=os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../results/01_discovery")),
        help="Directory containing result trees for the observation hosts.",
    )
    parser.add_argument(
        "--peer-base-dir", required=True,
        help="Other slave's capture/matching tree; both hosts define the common completion time.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory. Default: <base-dir>/analysis",
    )
    args = parser.parse_args()
    output_dir = args.output_dir
    if output_dir is None:
        output_dir = os.path.join(os.path.abspath(args.base_dir), "analysis")
    return run_analysis(args.base_dir, output_dir, args.peer_base_dir)


if __name__ == "__main__":
    sys.exit(main())
