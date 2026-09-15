#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: publish_once.sh <scenario>
Experiment: 04_probe_effect
Called by the corresponding experiment runner.
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Laptop A - scenario publisher runner
# usage: ./publish_once.sh <scenario>
# e.g.:  ./publish_once.sh S3
#
# Publishers run continuously. Stop them with Ctrl-C after the experiment.

set -euo pipefail

normalize_tty() {
  [[ -t 1 ]] && stty sane opost onlcr 2>/dev/null || true
}

normalize_tty

SCENARIO=${1:?usage: $0 <scenario>}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/common/environment.sh"
rp_exp_load_environment

export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-77}
PROBE_QOS=${PROBE_QOS:-best_effort}
case ${PROBE_QOS} in
  best_effort) QOS_SUFFIX=""; LAUNCH_SUFFIX="" ;;
  reliable)    QOS_SUFFIX="_rel"; LAUNCH_SUFFIX="_rel" ;;
  *) echo "[ERROR] unknown PROBE_QOS: ${PROBE_QOS}"; exit 1 ;;
esac

cleanup() {
  normalize_tty
  echo "[probe_publisher] stopped"
}

handle_signal() {
  trap - INT TERM
  normalize_tty
  exit 130
}

trap cleanup EXIT
trap handle_signal INT TERM

echo "[probe_publisher] starting ${SCENARIO} publisher (${PROBE_QOS})"

case ${SCENARIO} in
  S1) ros2 run rp_exp_probe_effect "s2_pub${QOS_SUFFIX}" ;;
  S2) ros2 run rp_exp_probe_effect "s3a_pub${QOS_SUFFIX}" ;;
  S3) ros2 run rp_exp_probe_effect "s3_points_pub${QOS_SUFFIX}" 30000 ;;
  S4) ros2 run rp_exp_probe_effect "s3_points_pub${QOS_SUFFIX}" 130000 ;;
  S5) ros2 run rp_exp_probe_effect "s4a_pub${QOS_SUFFIX}" ;;
  S6) ros2 run rp_exp_probe_effect "s4_image_pub${QOS_SUFFIX}" ;;
  S7) ros2 launch rp_exp_probe_effect "s5b_pub${LAUNCH_SUFFIX}.launch.py" ;;
  *) echo "[ERROR] unknown scenario: ${SCENARIO}"; exit 1 ;;
esac
