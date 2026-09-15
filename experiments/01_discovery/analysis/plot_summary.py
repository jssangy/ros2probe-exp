#!/usr/bin/env python3
"""
요약 CSV(discovery_analysis_result_summary.csv)를 읽어
행 순서대로 (N_P, N_E) 조합을 x축, 채널 점유율(airtime 활용률)을 y축으로
측정값과 이론값을 한 그래프에 그린다.
"""

import argparse
import csv
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="요약 CSV에서 (N_P,N_E)별 측정/이론 채널 점유율 그래프 생성"
    )
    parser.add_argument(
        "summary_csv",
        nargs="?",
        default="discovery_analysis_result_summary.csv",
        help="요약 CSV 경로 (기본: discovery_analysis_result_summary.csv)",
    )
    parser.add_argument(
        "-o", "--output",
        default="",
        help="저장할 그림 파일 경로 (비면 표시만)",
    )
    args = parser.parse_args()

    path = args.summary_csv
    if not os.path.isfile(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.summary_csv)
    if not os.path.isfile(path):
        print(f"[plot_summary] File not found: {args.summary_csv}", file=sys.stderr)
        return 1

    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                n_p = int(row.get("N_P", 0))
                n_e = int(row.get("N_E", 0))
                meas = float(row.get("average_airtime_utilization", 0))
                theory = float(row.get("theory_utilization_DDS", 0))
            except (ValueError, TypeError):
                continue
            rows.append({"N_P": n_p, "N_E": n_e, "measured": meas, "theory": theory})

    if not rows:
        print("[plot_summary] No valid rows in CSV.", file=sys.stderr)
        return 1

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("[plot_summary] matplotlib required: pip install matplotlib", file=sys.stderr)
        return 1

    # x: 행 순서(0,1,2,...), 라벨은 (N_P, N_E)
    x = list(range(len(rows)))
    labels = [f"({r['N_P']},{r['N_E']})" for r in rows]
    measured_pct = [r["measured"] * 100 for r in rows]
    theory_pct = [r["theory"] * 100 for r in rows]

    fig, ax = plt.subplots(figsize=(max(8, len(rows) * 0.4), 6))
    ax.plot(x, measured_pct, "b-o", linewidth=2, markersize=8, label="Measured")
    ax.plot(x, theory_pct, "r--s", linewidth=2, markersize=8, label="Theory (DDS SPDP+SEDP)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_xlabel("(N_P, N_E)", fontsize=12, fontweight="bold")
    ax.set_ylabel("Channel utilization (airtime %) ", fontsize=12, fontweight="bold")
    ax.legend(loc="best", fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(bottom=0)
    fig.tight_layout()

    if args.output:
        out = args.output
        if not os.path.isabs(out):
            out = os.path.join(os.path.dirname(os.path.abspath(path)), out)
        fig.savefig(out, dpi=150, bbox_inches="tight")
        print(f"[plot_summary] Saved: {out}")
    else:
        plt.show()

    return 0


if __name__ == "__main__":
    sys.exit(main())
