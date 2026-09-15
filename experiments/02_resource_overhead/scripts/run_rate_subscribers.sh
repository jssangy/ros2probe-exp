#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: run_rate_subscribers.sh --sync <peer-IP> [options]
Experiment: 02_resource_overhead
Options: --scenarios, --runs, --platform, --sync, --port, --ack-port
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Receiver Platform - Experiment 2 (resource overhead) runner
#
# Usage:
#   ./experiments/02_resource_overhead/scripts/run_rate_subscribers.sh --sync <Publisher-IP> --platform <pc|rpi|jetson>

set -euo pipefail

normalize_tty() {
  [[ -t 1 ]] && stty sane opost onlcr 2>/dev/null || true
}

normalize_tty

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
source "${REPO_DIR}/common/perf_setup.sh"

ALL_SCENARIOS=(ST100 ST500 ST1000)
SCENARIOS=()
CONDITIONS=(baseline rp_hz topic_hz)
N_RUNS=10
PLATFORM=""
SYNC_HOST=""
SYNC_PORT=55001
SYNC_ACK_PORT=55002

while [[ $# -gt 0 ]]; do
  case $1 in
    --scenarios) IFS=',' read -ra SCENARIOS <<< "$2"; shift 2 ;;
    --runs) N_RUNS="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --sync) SYNC_HOST="$2"; shift 2 ;;
    --port) SYNC_PORT="$2"; shift 2 ;;
    --ack-port) SYNC_ACK_PORT="$2"; shift 2 ;;
    *) echo "[ERROR] unknown option: $1"; exit 1 ;;
  esac
done

[[ ${#SCENARIOS[@]} -eq 0 ]] && SCENARIOS=("${ALL_SCENARIOS[@]}")
if [[ -z "${PLATFORM}" ]]; then
  PLATFORM="$(hostname -s)"
fi
if [[ -z "${SYNC_HOST}" ]]; then
  echo "[ERROR] --sync <Publisher-IP> is required"
  exit 1
fi

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/common/environment.sh"
rp_exp_load_environment

export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-77}
export SYNC_HOST
export SYNC_PORT
export SYNC_ACK_PORT
NIC=${NIC:-$(ip route show default | awk '/default/ {print $5; exit}')}
export NIC

SUDO_KEEPALIVE_PID=""
CLEANED_UP=0
CURRENT_RUN_PID=""

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

wait_pid_timeout() {
  local pid=${1:?}
  local timeout_s=${2:?}
  local i

  for i in $(seq 1 "${timeout_s}"); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

stop_current_run() {
  if [[ -n "${CURRENT_RUN_PID}" ]]; then
    kill -INT -- "-${CURRENT_RUN_PID}" 2>/dev/null || true
    kill -INT "${CURRENT_RUN_PID}" 2>/dev/null || true
    wait_pid_timeout "${CURRENT_RUN_PID}" 8 || {
      kill -TERM -- "-${CURRENT_RUN_PID}" 2>/dev/null || true
      kill -TERM "${CURRENT_RUN_PID}" 2>/dev/null || true
      wait_pid_timeout "${CURRENT_RUN_PID}" 3 || {
        kill -KILL -- "-${CURRENT_RUN_PID}" 2>/dev/null || true
        kill -KILL "${CURRENT_RUN_PID}" 2>/dev/null || true
      }
    }
    wait "${CURRENT_RUN_PID}" 2>/dev/null || true
    CURRENT_RUN_PID=""
  fi
}

setup_env() {
  setup_platform_performance "${PLATFORM}"
}

cleanup() {
  [[ "${CLEANED_UP}" == "1" ]] && return
  CLEANED_UP=1
  normalize_tty
  stop_current_run
  stop_sudo_keepalive
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

# Master profile: 5s scheduled START + 10s warmup + 60s sample + 1.1s tail + 10s cooldown.
FIXED_MS_PER_RUN=86100
TOTAL_SECS=$(( (${#SCENARIOS[@]} * ${#CONDITIONS[@]} * N_RUNS * FIXED_MS_PER_RUN + 999) / 1000 ))
TOTAL_H=$(( TOTAL_SECS / 3600 ))
TOTAL_M=$(( (TOTAL_SECS % 3600) / 60 ))

echo "================================================"
echo " Experiment 2 (resource overhead) - Receiver Platform"
echo " Platform  : ${PLATFORM}"
echo " Scenarios : ${SCENARIOS[*]}"
echo " Conditions: ${CONDITIONS[*]}"
echo " Runs      : ${N_RUNS}"
echo " NIC       : ${NIC}"
echo " Sync      : event-driven (publisher=${SYNC_HOST})"
echo " ROS_DOMAIN: ${ROS_DOMAIN_ID}"
echo " Fixed waits (master): ${TOTAL_H}h ${TOTAL_M}m; add startup, clock checks and cleanup"
echo "================================================"
echo ""

start_sudo_keepalive
setup_env

FAILED=()
START_TIME=$(date +%s)

for SCENARIO in "${SCENARIOS[@]}"; do
  echo ""
  echo "================================================"
  echo " [$(date '+%H:%M:%S')] Scenario: ${SCENARIO}"
  echo "================================================"

  for CONDITION in "${CONDITIONS[@]}"; do
    echo ""
    echo "  -- ${SCENARIO} / ${CONDITION} ------------------"
    for i in $(seq 1 "${N_RUNS}"); do
      RUN_LABEL="$(printf '%02d' "${i}")/${N_RUNS}"
      echo "    run ${RUN_LABEL}  ($(date '+%H:%M:%S'))"
      setsid env --default-signal=INT,QUIT bash "${SCRIPT_DIR}/run_rate_once.sh" "${PLATFORM}" "${SCENARIO}" "${CONDITION}" "${i}" &
      CURRENT_RUN_PID=$!
      if wait "${CURRENT_RUN_PID}"; then
        CURRENT_RUN_PID=""
        normalize_tty
      else
        RUN_STATUS=$?
        normalize_tty
        if [[ "${RUN_STATUS}" == "130" || "${RUN_STATUS}" == "143" ]]; then
          stop_current_run
          exit "${RUN_STATUS}"
        fi
        CURRENT_RUN_PID=""
        echo "    [WARN] run ${RUN_LABEL} failed; continuing"
        FAILED+=("${PLATFORM}/${SCENARIO}/${CONDITION}/run$(printf '%02d' "${i}")")
      fi
    done
    echo "  [finished] ${CONDITION}"
  done
done

echo "DONE" | nc -N -w5 "${SYNC_HOST}" "${SYNC_PORT}" 2>/dev/null || true
echo "  [sync] DONE sent"

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo ""
echo "================================================"
echo " Experiment 2 (resource overhead) complete  $(date '+%H:%M:%S')"
echo " Elapsed : $(( TOTAL_ELAPSED/3600 ))h $(( (TOTAL_ELAPSED%3600)/60 ))m"
echo " Results : ${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/02_resource_overhead/rate/${PLATFORM}/"
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo " Failed runs (rerun required):"
  for f in "${FAILED[@]}"; do echo "   - ${f}"; done
fi
echo "================================================"

if (( ${#FAILED[@]} > 0 )); then
  exit 1
fi
