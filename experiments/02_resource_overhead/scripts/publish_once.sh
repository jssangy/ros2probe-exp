#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: publish_once.sh <scenario>
Experiment: 02_resource_overhead
Called by the corresponding experiment runner.
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Laptop A - Experiment 2 (resource overhead) stress publisher runner
# usage: ./publish_once.sh <scenario>
# scenarios: ST100 | ST500 | ST1000

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

PAYLOAD_BYTES=${STRESS_PAYLOAD_BYTES:-65536}
TOPIC=${STRESS_TOPIC:-/stress}

case ${SCENARIO} in
  ST100)  HZ=100 ;;
  ST500)  HZ=500 ;;
  ST1000) HZ=1000 ;;
  *) echo "[ERROR] unknown scenario: ${SCENARIO}"; exit 1 ;;
esac

cleanup() {
  normalize_tty
  echo "[overhead_publisher] stopped"
}

handle_signal() {
  trap - INT TERM
  normalize_tty
  exit 130
}

trap cleanup EXIT
trap handle_signal INT TERM

echo "[overhead_publisher] starting ${SCENARIO}: ${HZ} Hz, payload=${PAYLOAD_BYTES}, topic=${TOPIC}"
ros2 run rp_exp_overhead stress_pub "${HZ}" "${PAYLOAD_BYTES}" 0 "${TOPIC}"
