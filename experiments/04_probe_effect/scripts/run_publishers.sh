#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: run_publishers.sh --sync <peer-IP> [options]
Experiment: 04_probe_effect
Options: --scenarios, --runs, --buffer, --platform, --qos, --link, --sync
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Laptop A - Experiment 4 (probe effect) publisher controller
#
# Usage:
#   ./experiments/04_probe_effect/scripts/run_publishers.sh --sync <Laptop-B-IP> [--platform <pc|rpi|jetson>]  # event-driven mode
#   ./experiments/04_probe_effect/scripts/run_publishers.sh                        # timer-based legacy mode
#
# Event mode (--sync):
#   B sends "START <scenario>" / "STOP" / "DONE" for each run.
#   A starts/stops publishers accordingly.
#   B -> A: port 55001 / A -> B: port 55002
#
# Timer mode:
#   Keep publishers alive for SCENARIO_WAIT per scenario when --sync is not used.

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
N_RUNS=10
BUFFER_S=180
SYNC_HOST=""
SYNC_PORT=${SYNC_PORT:-55001}
SYNC_ACK_PORT=${SYNC_ACK_PORT:-55002}
PLATFORM="${PLATFORM:-publisher}"
PROBE_QOS=${PROBE_QOS:-best_effort}
PROBE_LINK=${PROBE_LINK:-wired}

while [[ $# -gt 0 ]]; do
  case $1 in
    --scenarios) IFS=',' read -ra SCENARIOS <<< "$2"; shift 2 ;;
    --runs)      N_RUNS="$2"; shift 2 ;;
    --buffer)    BUFFER_S="$2"; shift 2 ;;
    --platform)  PLATFORM="$2"; shift 2 ;;
    --qos)       PROBE_QOS="$2"; shift 2 ;;
    --link)      PROBE_LINK="$2"; shift 2 ;;
    --sync)      SYNC_HOST="$2"; shift 2 ;;
    *) echo "[ERROR] unknown option: $1"; exit 1 ;;
  esac
done
[[ ${#SCENARIOS[@]} -eq 0 ]] && SCENARIOS=("${ALL_SCENARIOS[@]}")
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

condition_count_for_scenario() {
  local scenario=${1:?}
  if [[ "${scenario}" == "S7" ]]; then
    echo 5
  else
    echo 5
  fi
}

total_condition_count() {
  local total=0
  local scenario

  for scenario in "${SCENARIOS[@]}"; do
    total=$(( total + $(condition_count_for_scenario "${scenario}") ))
  done
  echo "${total}"
}

SUDO_KEEPALIVE_PID=""
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

# Environment setup.
setup_env() {
  setup_platform_performance "${PLATFORM}"
}

# Publisher control.

PUB_PID=""

stop_current() {
  if [[ -n "${PUB_PID}" ]]; then
    echo "[pub] stopping (PID ${PUB_PID})  $(date '+%H:%M:%S')"
    # Kill the whole process group started by setsid.
    kill -SIGTERM -- "-${PUB_PID}" 2>/dev/null || true
    sleep 1
    kill -SIGKILL -- "-${PUB_PID}" 2>/dev/null || true
    wait "${PUB_PID}" 2>/dev/null || true
    PUB_PID=""
  fi
}
cleanup() {
  [[ "${CLEANED_UP}" == "1" ]] && return
  CLEANED_UP=1
  normalize_tty
  stop_current
  stop_sudo_keepalive
}

handle_signal() {
  trap - INT TERM
  normalize_tty
  echo ""
  echo "[interrupt] stop requested; cleaning up..."
  exit 130
}

trap cleanup EXIT
trap handle_signal INT TERM

# Event-driven mode.

run_event_driven() {
  echo "================================================"
  echo " Experiment 4 (probe effect) - Laptop A (Publisher, event-driven)"
  echo " B wlan IP : ${SYNC_HOST}"
  echo " Platform  : ${PLATFORM}"
  echo " Link      : ${PROBE_LINK}"
  echo " QoS       : ${PROBE_QOS}"
  echo " Listen port: ${SYNC_PORT}  ACK port: ${SYNC_ACK_PORT}"
  echo "================================================"
  echo ""

  start_sudo_keepalive
  setup_env

  normalize_tty
  echo ""
  echo " Waiting for commands from Laptop B. Start run_subscribers.sh --sync <A-IP> on Laptop B."
  echo ""

  while true; do
    CMD=$(nc -l -p "${SYNC_PORT}" 2>/dev/null || true)
    CMD="${CMD%%$'\r'}"  # Strip carriage return.

    rp_exp_unwrap_schedule "${CMD}"
    CMD="${RP_EXP_COMMAND}"
    case "${CMD}" in
      START\ *)
        read -r _START SCENARIO CMD_QOS <<< "${CMD}"
        [[ -n "${CMD_QOS:-}" ]] && PROBE_QOS="${CMD_QOS}"
        export PROBE_QOS
        stop_current
        echo ""
        echo "  [$(date '+%H:%M:%S')] starting ${SCENARIO} publisher (${PROBE_QOS})"
        setsid env --default-signal=INT,QUIT bash "${SCRIPT_DIR}/publish_once.sh" "${SCENARIO}" &
        PUB_PID=$!
        sleep 1  # Wait for publisher initialization.
        if kill -0 "${PUB_PID}" 2>/dev/null; then
          echo "READY" | nc -N -w5 "${SYNC_HOST}" "${SYNC_ACK_PORT}"
        else
          echo "ERROR publisher exited" | nc -N -w5 "${SYNC_HOST}" "${SYNC_ACK_PORT}" || true
          echo "[ERROR] publisher failed to start" >&2
          exit 1
        fi
        ;;
      STOP)
        echo "  [$(date '+%H:%M:%S')] publisher stop requested"
        stop_current
        ;;
      DONE)
        echo ""
        echo "================================================"
        echo " Experiment 4 (probe effect) (Laptop A) complete  $(date '+%H:%M:%S')"
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
}

# Timer-based legacy mode.

run_timer_based() {
  SECS_PER_RUN=85
  TOTAL_CONDITIONS=$(total_condition_count)
  TOTAL_SECS=$(( TOTAL_CONDITIONS * N_RUNS * SECS_PER_RUN + ${#SCENARIOS[@]} * BUFFER_S ))
  TOTAL_H=$(( TOTAL_SECS / 3600 ))
  TOTAL_M=$(( (TOTAL_SECS % 3600) / 60 ))

  echo "================================================"
  echo " Experiment 4 (probe effect) - Laptop A (Publisher, timer-based)"
  echo " Platform        : ${PLATFORM}"
  echo " Scenarios       : ${SCENARIOS[*]}"
  echo " Link            : ${PROBE_LINK}"
  echo " QoS             : ${PROBE_QOS}"
  echo " Runs            : ${N_RUNS}"
  echo " Conditions      : S1-S7=5"
  echo " Estimate        : ${TOTAL_H}h ${TOTAL_M}m"
  echo "================================================"
  echo ""

  start_sudo_keepalive
  setup_env

  normalize_tty
  echo ""
  echo " Start run_subscribers.sh on Laptop B."
  echo " Press Enter on both laptops at the same time."
  echo ""
  read -rp "Press Enter when ready..."

  for SCENARIO in "${SCENARIOS[@]}"; do
    SCENARIO_WAIT=$(( $(condition_count_for_scenario "${SCENARIO}") * N_RUNS * SECS_PER_RUN + BUFFER_S ))
    echo ""
    echo "========================================"
    echo " [$(date '+%H:%M:%S')] Scenario: ${SCENARIO}"
    echo "========================================"

    setsid env --default-signal=INT,QUIT bash "${SCRIPT_DIR}/publish_once.sh" "${SCENARIO}" &
    PUB_PID=$!
    echo "[pub] started (PID ${PUB_PID})"

    END_TS=$(( $(date +%s) + SCENARIO_WAIT ))
    while (( $(date +%s) < END_TS )); do
      REMAINING=$(( END_TS - $(date +%s) ))
      echo "  Waiting... ${REMAINING}s remaining"
      sleep 5
    done

    stop_current
    echo "[pub] ${SCENARIO} complete  $(date '+%H:%M:%S')"
  done

  echo ""
  echo "================================================"
  echo " Experiment 4 (probe effect) (Laptop A) complete  $(date '+%H:%M:%S')"
  echo "================================================"
}

# Main.

if [[ -n "${SYNC_HOST}" ]]; then
  run_event_driven
else
  run_timer_based
fi
