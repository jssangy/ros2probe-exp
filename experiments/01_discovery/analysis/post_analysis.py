#!/usr/bin/env python3
"""
사후 분석: topic_endpoint_count와 discovery_capture_param_names를 읽어
'디스커버리 완료 시각' 이전 패킷만 추출한 결과 CSV를 생성한다.

1. topic_endpoint_count에서 pub+sub == 2*N_P*N_E 가 되는 첫 시각을 찾는다.
2. discovery_capture_param_names에서 그 시각 이전 행만 추출한다.
3. 시각, frame.len, rtps.sm.id, rtps.param.id 만 남긴다.
4. 결과 CSV: N_P, N_E, frame.time_epoch, frame.len, rtps.sm.id, rtps.param.id
   파일명에 N_P, N_E가 있는 CSV들을 구분해 (N_P, N_E)별로 처리한다.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from typing import Optional


def _parse_n_p_n_e_from_filename(filename: str, pattern: str) -> tuple:
    """파일명에서 N_P, N_E 추출. (n_p, n_e) 또는 실패 시 (None, None)."""
    m = re.search(pattern, os.path.basename(filename))
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (None, None)


def _find_first_completion_time(topic_endpoint_path: str, target_sum: int) -> Optional[float]:
    """
    topic_endpoint_count CSV에서 total_pub + total_sub >= target_sum 인
    첫 행의 time_epoch(첫 번째 열)를 반환. 없으면 None.
    """
    with open(topic_endpoint_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return None
        # 마지막 두 열이 total_pub, total_sub
        for row in reader:
            if len(row) < 2:
                continue
            try:
                t = float(row[0])
                total_pub = int(row[-2])
                total_sub = int(row[-1])
                if total_pub + total_sub >= target_sum:
                    return t
            except (ValueError, IndexError):
                continue
    return None


def _airtime(frame_len_str: str, traffic_type: str) -> float:
    """frame.len과 traffic_type으로 Airtime(μs) 계산. 멀티캐스트: len*(4/3)+120, 유니캐스트: len*(8/144.4)+180."""
    try:
        length = float(frame_len_str or 0)
    except (ValueError, TypeError):
        length = 0.0
    if traffic_type == "multicast":
        return length * (4 / 3) + 120
    return length * (8 / 144.4) + 180


# 이론 채널 점유율 상수 (MATLAB 기준, 단위: μs, 1초 구간 기준 μs/s)
_C_SPDP = 875
_C_SEDP_Data = 412
_C_SEDP_Ctrl = 1488
_C_Scout_Multi = 411
_C_Scout_Uni = 376
_C_Decl_Data = 380
_C_Decl_Ctrl = 740


def _theory_utilization_dds(n_h: int, n_p: int, n_e: int) -> tuple:
    """
    DDS SPDP+SEDP 이론 airtime (μs/s) 및 활용률(비율).
    MATLAB 식: U_SPDP = C_SPDP*N_H*N_P, U_SEDP = N_H*(N_H-1)*N_P^2*(C_SEDP_Data*N_E + C_SEDP_Ctrl).
    반환: (utilization_ratio, spdp_pct, sedp_pct)
    """
    u_spdp_us = _C_SPDP * n_h * n_p
    u_sedp_us = (n_h * (n_h - 1) * (n_p ** 2)) * (_C_SEDP_Data * n_e + _C_SEDP_Ctrl)
    total_us = u_spdp_us + u_sedp_us
    # 활용률 비율 = (μs/s) / 1e6
    utilization_ratio = total_us / 1e6
    # 퍼센트 = (μs/s)/1e4
    spdp_pct = u_spdp_us / 10000.0
    sedp_pct = u_sedp_us / 10000.0
    return (utilization_ratio, spdp_pct, sedp_pct)


def _traffic_type(dst: str) -> str:
    """ip.dst가 IPv4 멀티캐스트(224.0.0.0/4)면 'multicast', 아니면 'unicast'."""
    if not dst:
        return "unicast"
    parts = dst.strip().split(".")
    if len(parts) >= 1:
        try:
            first = int(parts[0])
            if 224 <= first <= 239:
                return "multicast"
        except ValueError:
            pass
    return "unicast"


def _extract_packets_before_time(param_names_path: str, time_threshold: float):
    """
    discovery_capture_param_names (탭 구분)에서 frame.time_epoch < time_threshold 인 행만
    추출하고, frame.time_epoch, frame.len, rtps.sm.id, rtps.param.id 만 반환.
    ip.src 또는 ip.dst에 127. 이 포함된 루프백 패킷은 제외한다.
    """
    out = []
    with open(param_names_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            try:
                t = float(row.get("frame.time_epoch", ""))
            except (ValueError, TypeError):
                continue
            if t >= time_threshold:
                continue
            src = row.get("ip.src", "")
            dst = row.get("ip.dst", "")
            if "127." in src or "127." in dst:
                continue
            out.append({
                "frame.time_epoch": row.get("frame.time_epoch", ""),
                "frame.time_delta": row.get("frame.time_delta", ""),
                "ip.src": src,
                "ip.dst": dst,
                "frame.len": row.get("frame.len", ""),
                "rtps.sm.id": row.get("rtps.sm.id", ""),
                "rtps.param.id": row.get("rtps.param.id", ""),
            })
    return out


def run_analysis(data_dir: str, output_path: str, n_h: int = 2) -> int:
    """
    data_dir 안의 *N_P_*_N_E_* 패턴 CSV를 찾아 (N_P, N_E)별로 분석하고
    결과를 output_path 하나의 CSV로 쓴다.
    n_h: 호스트 수 (이론값 계산용, 2로 고정).
    """
    data_dir = os.path.abspath(data_dir)
    if not os.path.isdir(data_dir):
        print(f"[post_analysis] Not a directory: {data_dir}", file=sys.stderr)
        return 1

    # topic_endpoint_count_N_P_3_N_E_63.csv 형태
    endpoint_pattern = re.compile(r"topic_endpoint_count_N_P_(\d+)_N_E_(\d+)\.csv")
    # *_N_P_3_N_E_63_param_names.csv 형태 (discovery_capture 등 base 이름 다양)
    param_names_pattern = re.compile(r"_N_P_(\d+)_N_E_(\d+)_param_names\.csv$")

    endpoint_files = {}
    for name in os.listdir(data_dir):
        n_p, n_e = _parse_n_p_n_e_from_filename(name, endpoint_pattern)
        if n_p is not None and n_e is not None:
            endpoint_files[(n_p, n_e)] = os.path.join(data_dir, name)

    param_names_files = {}
    for name in os.listdir(data_dir):
        full = os.path.join(data_dir, name)
        if not name.endswith("_param_names.csv"):
            continue
        n_p, n_e = _parse_n_p_n_e_from_filename(name, param_names_pattern)
        if n_p is not None and n_e is not None:
            param_names_files[(n_p, n_e)] = full

    print(f"[post_analysis] data_dir={data_dir}")
    print(f"[post_analysis] topic_endpoint_count files: {len(endpoint_files)} {list(endpoint_files.keys())}")
    print(f"[post_analysis] param_names files: {len(param_names_files)} {list(param_names_files.keys())}")
    if not endpoint_files:
        print("[post_analysis] No topic_endpoint_count_N_P_*_N_E_*.csv found. Pass the directory where CSV files are.", file=sys.stderr)
        return 1

    result_rows = []
    for (n_p, n_e) in sorted(endpoint_files.keys()):
        ep_path = endpoint_files[(n_p, n_e)]
        pn_path = param_names_files.get((n_p, n_e))
        if not pn_path or not os.path.isfile(pn_path):
            print(f"[post_analysis] Skip (N_P={n_p}, N_E={n_e}): param_names file not found", file=sys.stderr)
            continue

        target_sum = 2 * n_p * n_e
        completion_time = _find_first_completion_time(ep_path, target_sum)
        if completion_time is None:
            print(f"[post_analysis] Skip (N_P={n_p}, N_E={n_e}): no row with pub+sub>={target_sum}", file=sys.stderr)
            continue

        packets = _extract_packets_before_time(pn_path, completion_time)
        for p in packets:
            tt = _traffic_type(p["ip.dst"])
            result_rows.append({
                "N_P": n_p,
                "N_E": n_e,
                "frame.time_epoch": p["frame.time_epoch"],
                "frame.time_delta": p["frame.time_delta"],
                "traffic_type": tt,
                "ip.src": p["ip.src"],
                "ip.dst": p["ip.dst"],
                "frame.len": p["frame.len"],
                "Airtime": _airtime(p["frame.len"], tt),
                "rtps.sm.id": p["rtps.sm.id"],
                "rtps.param.id": p["rtps.param.id"],
            })
        print(f"[post_analysis] (N_P={n_p}, N_E={n_e}) completion_time={completion_time:.3f}, packets={len(packets)}")

    if not result_rows:
        print("[post_analysis] No rows to write.", file=sys.stderr)
        return 1

    # 추출한 패킷 행을 frame.time_epoch 기준 시간순 정렬
    def _epoch_key(row):
        try:
            return float(row.get("frame.time_epoch") or 0)
        except (ValueError, TypeError):
            return 0.0
    result_rows.sort(key=_epoch_key)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    fieldnames = ["N_P", "N_E", "frame.time_epoch", "frame.time_delta", "traffic_type", "ip.src", "ip.dst", "frame.len", "Airtime", "rtps.sm.id", "rtps.param.id"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(result_rows)
    print(f"[post_analysis] Result saved: {output_path} ({len(result_rows)} rows)")

    # (N_P, N_E)별로 Airtime(μs) 합·시간 구간 구한 뒤 평균 airtime(활용률) 계산하여 요약 CSV 기록
    by_group = defaultdict(list)
    for row in result_rows:
        by_group[(row["N_P"], row["N_E"])].append(row)
    summary_path = os.path.splitext(output_path)[0] + "_summary.csv"
    summary_rows = []
    for (n_p, n_e) in sorted(by_group.keys()):
        rows = by_group[(n_p, n_e)]
        try:
            first_epoch = float(rows[0]["frame.time_epoch"])
            last_epoch = float(rows[-1]["frame.time_epoch"])
        except (ValueError, TypeError, IndexError):
            continue
        time_span_sec = last_epoch - first_epoch
        if time_span_sec <= 0:
            continue
        total_airtime_us = sum(float(r["Airtime"]) for r in rows)
        # Airtime 단위 μs → 총 airtime(초) / 구간(초) = 활용률(비율). 평균 airtime(μs/초) = total_airtime_us / time_span_sec
        total_airtime_sec = total_airtime_us / 1e6
        average_airtime_utilization = total_airtime_sec / time_span_sec
        average_airtime_us_per_sec = total_airtime_us / time_span_sec

        # Multicast만 / Unicast만 동일 구간(time_span_sec) 기준으로 합·활용률·μs/s 계산
        multicast_rows = [r for r in rows if (r.get("traffic_type") or "").strip().lower() == "multicast"]
        unicast_rows = [r for r in rows if (r.get("traffic_type") or "").strip().lower() == "unicast"]
        total_airtime_multicast_us = sum(float(r["Airtime"]) for r in multicast_rows)
        total_airtime_unicast_us = sum(float(r["Airtime"]) for r in unicast_rows)
        avg_util_multicast = (total_airtime_multicast_us / 1e6) / time_span_sec if time_span_sec > 0 else 0.0
        avg_util_unicast = (total_airtime_unicast_us / 1e6) / time_span_sec if time_span_sec > 0 else 0.0
        avg_us_per_sec_multicast = total_airtime_multicast_us / time_span_sec if time_span_sec > 0 else 0.0
        avg_us_per_sec_unicast = total_airtime_unicast_us / time_span_sec if time_span_sec > 0 else 0.0

        # 이론값 (DDS SPDP+SEDP, 단위: 활용률 비율 및 %)
        theory_util, theory_spdp_pct, theory_sedp_pct = _theory_utilization_dds(n_h, n_p, n_e)

        summary_rows.append({
            "N_P": n_p,
            "N_E": n_e,
            "time_span_sec": round(time_span_sec, 6),
            "total_airtime_us": round(total_airtime_us, 2),
            "average_airtime_utilization": round(average_airtime_utilization, 6),
            "theory_utilization_DDS": round(theory_util, 6),
            "theory_SPDP_pct": round(theory_spdp_pct, 4),
            "theory_SEDP_pct": round(theory_sedp_pct, 4),
            "average_airtime_us_per_sec": round(average_airtime_us_per_sec, 2),
            "total_airtime_multicast_us": round(total_airtime_multicast_us, 2),
            "average_airtime_utilization_multicast": round(avg_util_multicast, 6),
            "average_airtime_us_per_sec_multicast": round(avg_us_per_sec_multicast, 2),
            "total_airtime_unicast_us": round(total_airtime_unicast_us, 2),
            "average_airtime_utilization_unicast": round(avg_util_unicast, 6),
            "average_airtime_us_per_sec_unicast": round(avg_us_per_sec_unicast, 2),
        })
    if summary_rows:
        summary_fieldnames = [
            "N_P", "N_E", "time_span_sec", "total_airtime_us", "average_airtime_utilization",
            "theory_utilization_DDS", "theory_SPDP_pct", "theory_SEDP_pct",
            "average_airtime_us_per_sec",
            "total_airtime_multicast_us", "average_airtime_utilization_multicast", "average_airtime_us_per_sec_multicast",
            "total_airtime_unicast_us", "average_airtime_utilization_unicast", "average_airtime_us_per_sec_unicast",
        ]
        with open(summary_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=summary_fieldnames)
            writer.writeheader()
            writer.writerows(summary_rows)
        print(f"[post_analysis] Summary saved: {summary_path}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="topic_endpoint_count와 discovery_capture_param_names로 디스커버리 완료 이전 패킷만 추출한 결과 CSV 생성"
    )
    parser.add_argument(
        "data_dir",
        nargs="?",
        default=None,
        help="N_P, N_E가 포함된 CSV 파일들이 있는 디렉터리 (기본: 이 스크립트 기준 패키지 루트)",
    )
    parser.add_argument(
        "-o", "--output",
        default="discovery_analysis_result.csv",
        help="출력 결과 CSV 경로 (기본: discovery_analysis_result.csv)",
    )
    args = parser.parse_args()
    if args.data_dir is None:
        args.data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../results/01_discovery"))
    return run_analysis(args.data_dir, args.output, n_h=2)


if __name__ == "__main__":
    sys.exit(main())
