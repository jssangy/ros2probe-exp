#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: run_rate_publishers.sh --sync <peer-IP> [options]
Experiment: 02_resource_overhead
Options: --sync, --platform, --port, --ack-port
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Laptop A - Experiment 2 (resource overhead) publisher controller
#
# Usage:
#   ./experiments/02_resource_overhead/scripts/run_rate_publishers.sh --sync <Receiver-IP> [--platform <pc|rpi|jetson>]
#
# B sends "START <scenario>" / "STOP" / "DONE" for each run.
# B -> A: port 55001 / A -> B: port 55002

set -euo pipefail

normalize_tty() {
  [[ -t 1 ]] && stty sane opost onlcr 2>/dev/null || true
}

normalize_tty

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
source "${REPO_DIR}/common/perf_setup.sh"

SYNC_HOST=""
SYNC_PORT=55001
SYNC_ACK_PORT=55002
PLATFORM="${PLATFORM:-publisher}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --sync) SYNC_HOST="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --port) SYNC_PORT="$2"; shift 2 ;;
    --ack-port) SYNC_ACK_PORT="$2"; shift 2 ;;
    *) echo "[ERROR] unknown option: $1"; exit 1 ;;
  esac
done

if [[ -z "${SYNC_HOST}" ]]; then
  echo "[ERROR] --sync <Receiver-IP> is required"
  exit 1
fi

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/common/environment.sh"
rp_exp_load_environment

export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-77}

SUDO_KEEPALIVE_PID=""
PUB_PID=""
NC_PID=""
CLEANED_UP=0

start_sudo_keepalive() {
  echo "[setup] Checking sudo credentials for unattended execution"
  if [[ "${RP_EXP_UNATTENDED:-0}" == 1 ]]; then
    sudo -n true
  else
    sudo -v
  fi
  (
    while true; do
      sudo -n true 2>/dev/null || exit
      sleep 60
    done
  ) >/dev/null 2>&1 &
  SUDO_KEEPALIVE_PID=$!
}

stop_sudo_keepalive() {
  if [[ -n "${SUDO_KEEPALIVE_PID}" ]]; then
    kill "${SUDO_KEEPALIVE_PID}" 2>/dev/null || true
    wait "${SUDO_KEEPALIVE_PID}" 2>/dev/null || true
    SUDO_KEEPALIVE_PID=""
  fi
}

setup_env() {
  setup_platform_performance "${PLATFORM}"
}

stop_current() {
  if [[ -n "${PUB_PID}" ]]; then
    echo "[pub] stopping (PID ${PUB_PID})  $(date '+%H:%M:%S')"
    kill -SIGINT -- "-${PUB_PID}" 2>/dev/null || true
    sleep 1
    kill -SIGTERM -- "-${PUB_PID}" 2>/dev/null || true
    sleep 1
    kill -SIGKILL -- "-${PUB_PID}" 2>/dev/null || true
    wait "${PUB_PID}" 2>/dev/null || true
    PUB_PID=""
    normalize_tty
  fi
}

stop_listener() {
  if [[ -n "${NC_PID}" ]]; then
    kill "${NC_PID}" 2>/dev/null || true
    wait "${NC_PID}" 2>/dev/null || true
    NC_PID=""
  fi
}

cleanup() {
  [[ "${CLEANED_UP}" == "1" ]] && return
  CLEANED_UP=1
  normalize_tty
  stop_listener
  stop_current
  stop_sudo_keepalive
  normalize_tty
}

handle_signal() {
  trap - INT TERM
  normalize_tty
  echo ""
  echo "[interrupt] stop requested; cleaning up..."
  cleanup
  exit 130
}

trap cleanup EXIT
trap handle_signal INT TERM

echo "================================================"
echo " Experiment 2 (resource overhead) - Publisher Host"
echo " Receiver IP : ${SYNC_HOST}"
echo " Platform    : ${PLATFORM}"
echo " Listen port : ${SYNC_PORT}"
echo " ACK port    : ${SYNC_ACK_PORT}"
echo " ROS_DOMAIN  : ${ROS_DOMAIN_ID}"
echo "================================================"
echo ""

start_sudo_keepalive
setup_env

echo "Waiting for commands from Receiver. Start run_rate_subscribers.sh --sync <Publisher-IP> on receiver."
echo ""

while true; do
  TMP_CMD=$(mktemp /tmp/rp_overhead_cmd.XXXXXX)
  nc -l -p "${SYNC_PORT}" > "${TMP_CMD}" 2>/dev/null &
  NC_PID=$!
  wait "${NC_PID}" 2>/dev/null || true
  NC_PID=""
  CMD=$(cat "${TMP_CMD}" 2>/dev/null || true)
  rm -f "${TMP_CMD}"
  CMD="${CMD%%$'\r'}"
  normalize_tty

  rp_exp_unwrap_schedule "${CMD}"
  CMD="${RP_EXP_COMMAND}"
  case "${CMD}" in
    START\ *)
      SCENARIO="${CMD#START }"
      stop_current
      echo ""
      echo "  [$(date '+%H:%M:%S')] starting ${SCENARIO} publisher"
      PUB_LOG="${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/02_resource_overhead/publisher/rate_${SCENARIO}.log"
      mkdir -p "$(dirname "${PUB_LOG}")"
      setsid env --default-signal=INT,QUIT bash "${SCRIPT_DIR}/publish_once.sh" "${SCENARIO}" > "${PUB_LOG}" 2>&1 &
      PUB_PID=$!
      echo "  [pub] log: ${PUB_LOG}"
      sleep 1
      if kill -0 "${PUB_PID}" 2>/dev/null; then
        echo "READY" | nc -N -w5 "${SYNC_HOST}" "${SYNC_ACK_PORT}"
      else
        echo "ERROR publisher exited" | nc -N -w5 "${SYNC_HOST}" "${SYNC_ACK_PORT}" || true
        echo "[ERROR] publisher failed to start" >&2
        exit 1
      fi
      normalize_tty
      ;;
    STOP)
      echo "  [$(date '+%H:%M:%S')] publisher stop requested"
      stop_current
      normalize_tty
      ;;
    DONE)
      echo ""
      echo "================================================"
      echo " Experiment 2 (resource overhead) publisher complete  $(date '+%H:%M:%S')"
      echo "================================================"
      break
      ;;
    "")
      ;;
    *)
      echo "[WARN] unknown command: '${CMD}'"
      ;;
  esac
done
