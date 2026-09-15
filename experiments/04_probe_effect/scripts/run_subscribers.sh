#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: run_subscribers.sh --sync <peer-IP> [options]
Experiment: 04_probe_effect
Options: --scenarios, --conditions, --runs, --buffer, --platform, --qos, --link, --nic, --sync
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Laptop B - Experiment 4 (probe effect) runner
#
# Usage:
#   ./experiments/04_probe_effect/scripts/run_subscribers.sh --sync <Laptop-A-IP> [--platform <pc|rpi|jetson>] [--nic <iface>]  # event-driven mode
#   ./experiments/04_probe_effect/scripts/run_subscribers.sh                        # timer-based legacy mode
#
# Event mode: send START/STOP to Laptop A for every run.
# Timer mode: wait for SCENARIO_WAIT when --sync is not used.
#
# --sync IP can be Ethernet or Wi-Fi.

set -euo pipefail

normalize_tty() {
  [[ -t 1 ]] && stty sane opost onlcr 2>/dev/null || true
}

normalize_tty

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
source "${REPO_DIR}/common/perf_setup.sh"

ALL_SCENARIOS=(S1 S2 S3 S4 S5 S6 S7)
SCENARIOS=()
SELECTED_CONDITIONS=()
N_RUNS=10
BUFFER_S=180
SYNC_HOST=""
SYNC_PORT=${SYNC_PORT:-55001}
SYNC_ACK_PORT=${SYNC_ACK_PORT:-55002}
PLATFORM=""
PROBE_QOS=${PROBE_QOS:-best_effort}
PROBE_LINK=${PROBE_LINK:-wired}

while [[ $# -gt 0 ]]; do
  case $1 in
    --scenarios) IFS=',' read -ra SCENARIOS <<< "$2"; shift 2 ;;
    --conditions) IFS=',' read -ra SELECTED_CONDITIONS <<< "$2"; shift 2 ;;
    --runs)      N_RUNS="$2"; shift 2 ;;
    --buffer)    BUFFER_S="$2"; shift 2 ;;
    --platform)  PLATFORM="$2"; shift 2 ;;
    --qos)       PROBE_QOS="$2"; shift 2 ;;
    --link)      PROBE_LINK="$2"; shift 2 ;;
    --nic)       NIC="$2"; shift 2 ;;
    --sync)      SYNC_HOST="$2"; shift 2 ;;
    *) echo "[ERROR] unknown option: $1"; exit 1 ;;
  esac
done
[[ ${#SCENARIOS[@]} -eq 0 ]] && SCENARIOS=("${ALL_SCENARIOS[@]}")
[[ -z "${PLATFORM}" ]] && PLATFORM="$(hostname -s)"
[[ "${N_RUNS}" =~ ^[1-9][0-9]*$ ]] || { echo '[ERROR] --runs must be a positive integer' >&2; exit 1; }
for scenario in "${SCENARIOS[@]}"; do
  case "$scenario" in S[1-7]) ;; *) echo "[ERROR] unknown scenario: $scenario" >&2; exit 1 ;; esac
done
for condition in "${SELECTED_CONDITIONS[@]}"; do
  case "$condition" in baseline|rp_hz|rp_bag_one|rp_bag_all|topic_hz|rosbag2_one|rosbag2_all) ;;
    *) echo "[ERROR] unknown condition: $condition" >&2; exit 1 ;; esac
done
case ${PROBE_QOS} in
  best_effort|reliable) ;;
  *) echo "[ERROR] unknown --qos: ${PROBE_QOS}"; exit 1 ;;
esac
case ${PROBE_LINK} in
  wired|wireless) ;;
  *) echo "[ERROR] unknown --link: ${PROBE_LINK}"; exit 1 ;;
esac
export PROBE_QOS
export PROBE_LINK

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/common/environment.sh"
rp_exp_load_environment
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-77}

NIC=${NIC:-$(ip route show default | awk '/default/ {print $5; exit}')}
if [[ -z "${NIC}" ]]; then
  echo "[ERROR] NIC is empty; pass --nic <iface> or export NIC=<iface>" >&2
  exit 1
fi
RX_BYTES_PATH="/sys/class/net/${NIC}/statistics/rx_bytes"
if [[ ! -r "${RX_BYTES_PATH}" ]]; then
  echo "[ERROR] netdev rx counter not readable for NIC=${NIC}: ${RX_BYTES_PATH}" >&2
  exit 1
fi
export NIC
export SYNC_HOST
export SYNC_PORT
export SYNC_ACK_PORT

conditions_for_scenario() {
  local scenario=${1:?}
  if (( ${#SELECTED_CONDITIONS[@]} > 0 )); then
    echo "${SELECTED_CONDITIONS[*]}"
    return
  fi
  if [[ "${scenario}" == "S7" ]]; then
    echo "baseline rp_hz topic_hz rp_bag_all rosbag2_all"
  else
    echo "baseline rp_hz topic_hz rp_bag_all rosbag2_all"
  fi
}

total_condition_count() {
  local total=0
  local scenario
  local conditions

  for scenario in "${SCENARIOS[@]}"; do
    read -ra conditions <<< "$(conditions_for_scenario "${scenario}")"
    total=$(( total + ${#conditions[@]} ))
  done
  echo "${total}"
}

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

# Environment setup.
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
  exit 130
}

# Master profile: 5s scheduled START + 60s sample + 1.1s tail + 10s cooldown.
FIXED_MS_PER_RUN=76100
TOTAL_CONDITIONS=$(total_condition_count)
TOTAL_SECS=$(( (TOTAL_CONDITIONS * N_RUNS * FIXED_MS_PER_RUN + 999) / 1000 ))
TOTAL_H=$(( TOTAL_SECS / 3600 ))
TOTAL_M=$(( (TOTAL_SECS % 3600) / 60 ))

echo "================================================"
echo " Experiment 4 (probe effect) - Laptop B (Subscriber)"
echo " Platform  : ${PLATFORM}"
echo " Scenarios : ${SCENARIOS[*]}"
echo " Link      : ${PROBE_LINK}"
echo " QoS       : ${PROBE_QOS}"
echo " Conditions: baseline rp_hz topic_hz rp_bag_all rosbag2_all"
echo " Runs      : ${N_RUNS}"
echo " NIC      : ${NIC}"
if [[ -n "${SYNC_HOST}" ]]; then
  echo " Sync     : event-driven (A=${SYNC_HOST}, per-run publisher start/stop)"
  echo " Fixed waits (master): ${TOTAL_H}h ${TOTAL_M}m; add startup, clock checks, cleanup and bag validation"
else
  echo " Sync     : timer-based"
fi
echo "================================================"
echo ""

trap cleanup EXIT
trap handle_signal INT TERM
start_sudo_keepalive
setup_env

normalize_tty
echo ""
echo "Pre-flight check:"
echo "  1) Start run_publishers.sh --sync <B-wlan-IP> on Laptop A first"
echo ""
if [[ -n "${SYNC_HOST}" ]]; then
  echo "Event-driven sync mode: starting without an Enter prompt."
else
  read -rp "Press Enter when both laptops are ready..."
fi

START_TIME=$(date +%s)
FAILED=()

for SCENARIO in "${SCENARIOS[@]}"; do
  read -ra CONDITIONS <<< "$(conditions_for_scenario "${SCENARIO}")"
  SCENARIO_START=$(date +%s)
  echo ""
  echo "================================================"
  echo " [$(date '+%H:%M:%S')] Scenario: ${SCENARIO}"
  echo "================================================"

  for CONDITION in "${CONDITIONS[@]}"; do
    echo ""
    echo "  -- ${SCENARIO} / ${CONDITION} ------------------"
    for i in $(seq 1 "${N_RUNS}"); do
      RUN_LABEL="$(printf '%02d' ${i})/${N_RUNS}"
      echo "    run ${RUN_LABEL}  ($(date '+%H:%M:%S'))"
      setsid env --default-signal=INT,QUIT bash "${SCRIPT_DIR}/run_once.sh" "${SCENARIO}" "${CONDITION}" "${i}" &
      CURRENT_RUN_PID=$!
      if wait "${CURRENT_RUN_PID}"; then
        CURRENT_RUN_PID=""
      else
        RUN_STATUS=$?
        if [[ "${RUN_STATUS}" == "130" || "${RUN_STATUS}" == "143" ]]; then
          exit "${RUN_STATUS}"
        fi
        CURRENT_RUN_PID=""
        if [[ "${RP_EXP_UNATTENDED:-0}" == 1 ]]; then
          echo "[ERROR] ${SCENARIO}/${CONDITION}/run$(printf '%02d' ${i}) failed; aborting master-controlled batch"
          exit "${RUN_STATUS}"
        fi
        echo "    [WARN] run ${RUN_LABEL} failed; continuing"
        FAILED+=("${SCENARIO}/${CONDITION}/run$(printf '%02d' ${i})")
      fi
    done
    echo "  [finished] ${CONDITION}"
  done

  ELAPSED=$(( $(date +%s) - SCENARIO_START ))
  echo ""
  echo "  [ok] ${SCENARIO} complete - ${ELAPSED}s  ($(date '+%H:%M:%S'))"

  # Timer mode: wait before the next scenario unless this was the last one.
  if [[ -z "${SYNC_HOST}" && "${SCENARIO}" != "${SCENARIOS[-1]}" ]]; then
    SECS_PER_RUN_TIMER=85
    SCENARIO_WAIT=$(( ${#CONDITIONS[@]} * N_RUNS * SECS_PER_RUN_TIMER + BUFFER_S ))
    REMAINING=$(( SCENARIO_WAIT - ELAPSED ))
    if (( REMAINING > 0 )); then
      echo "  Waiting ${REMAINING}s before the next scenario..."
      sleep "${REMAINING}"
    fi
  fi
done

# Event mode: notify Laptop A that the experiment is complete.
if [[ -n "${SYNC_HOST}" ]]; then
  echo "DONE" | nc -N -w5 "${SYNC_HOST}" "${SYNC_PORT}" 2>/dev/null || true
  echo "  [sync] DONE sent"
fi

# Final summary.
TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo ""
echo "================================================"
echo " Experiment 4 (probe effect) complete  $(date '+%H:%M:%S')"
echo " Elapsed : $(( TOTAL_ELAPSED/3600 ))h $(( (TOTAL_ELAPSED%3600)/60 ))m"
  echo " Results : ${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/04_probe_effect/${PROBE_LINK}_${PROBE_QOS}/"
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo " Failed runs (rerun required):"
  for f in "${FAILED[@]}"; do echo "   - ${f}"; done
fi
echo "================================================"

if (( ${#FAILED[@]} > 0 )); then
  exit 1
fi
