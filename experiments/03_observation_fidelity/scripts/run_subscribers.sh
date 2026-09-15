#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: run_subscribers.sh --sync <peer-IP> [options]
Experiment: 03_observation_fidelity
Options: --sync, --port, --platform, --runs, --losses, --conditions
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Laptop B - Experiment 3 (observation fidelity) observer fidelity runner

set -euo pipefail

normalize_tty() {
  [[ -t 1 ]] && stty sane opost onlcr 2>/dev/null || true
}

normalize_tty

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
source "${REPO_DIR}/common/perf_setup.sh"

SYNC_HOST=""
SYNC_PORT=56001
SYNC_ACK_PORT=56002
PLATFORM=""
LOSSES=(0 10 20)
CONDITIONS=(rp_bag rosbag2)
N_RUNS=10
OBSERVER_LEAD_SEC=3
PRESTART_SEC=5
HZ=30
PAYLOAD_BYTES=1024
EXPECTED=1800
TOPIC="/drop_image"
SUB_TIMEOUT=62
RP_BIN=${RP_BIN:-$(command -v rp || true)}
RP_SOCKET=${RP_SOCKET:-/tmp/ros2probe.sock}

while [[ $# -gt 0 ]]; do
  case $1 in
    --sync)       SYNC_HOST="$2"; shift 2 ;;
    --port)       SYNC_PORT="$2"; shift 2 ;;
    --ack-port)   SYNC_ACK_PORT="$2"; shift 2 ;;
    --platform)   PLATFORM="$2"; shift 2 ;;
    --runs)       N_RUNS="$2"; shift 2 ;;
    --losses)     IFS=',' read -ra LOSSES <<< "$2"; shift 2 ;;
    --conditions) IFS=',' read -ra CONDITIONS <<< "$2"; shift 2 ;;
    *) echo "[ERROR] unknown option: $1"; exit 1 ;;
  esac
done
[[ -z "${SYNC_HOST}" ]] && { echo "[ERROR] --sync <Laptop-A-IP> is required"; exit 1; }
[[ -z "${PLATFORM}" ]] && PLATFORM="$(hostname -s)"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/common/environment.sh"
rp_exp_load_environment
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-77}

SUB_PID=""
OBS_PID=""
RP_PID=""
SUDO_KEEPALIVE_PID=""
CURRENT_LOSS=""
CURRENT_CONDITION=""
CURRENT_RUN=""
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

stop_pid_gracefully() {
  local pid=${1:?}
  local signal=${2:-TERM}
  local timeout_s=${3:-5}
  kill "-${signal}" -- "-${pid}" 2>/dev/null || true
  kill "-${signal}" "${pid}" 2>/dev/null || true
  for _ in $(seq 1 "${timeout_s}"); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      wait "${pid}" 2>/dev/null || true
      return 0
    fi
    sleep 1
  done
  kill -KILL -- "-${pid}" 2>/dev/null || true
  kill -KILL "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
}

wait_for_rp_socket() {
  rp_exp_wait_runtime "${RP_PID:-${rp_pid:-}}" "${1:-100}"
}

remove_existing_output() {
  local path=${1:?}

  if [[ -e "${path}" || -L "${path}" ]]; then
    rm -rf -- "${path}" 2>/dev/null || sudo -n rm -rf -- "${path}"
  fi
}

stop_observer() {
  case ${CURRENT_CONDITION} in
    rp_bag)
      [[ -n "${OBS_PID}" ]] && stop_pid_gracefully "${OBS_PID}" INT 10
      OBS_PID=""
      if [[ -n "${RP_PID}" ]]; then
        sudo -n kill -INT -- "-${RP_PID}" 2>/dev/null || true
        sudo -n kill -INT "${RP_PID}" 2>/dev/null || true
        for _ in $(seq 1 5); do
          if ! kill -0 "${RP_PID}" 2>/dev/null; then
            wait "${RP_PID}" 2>/dev/null || true
            RP_PID=""
            return 0
          fi
          sleep 1
        done
        sudo -n kill -KILL -- "-${RP_PID}" 2>/dev/null || true
        sudo -n kill -KILL "${RP_PID}" 2>/dev/null || true
        wait "${RP_PID}" 2>/dev/null || true
        RP_PID=""
      fi
      ;;
    rosbag2)
      [[ -n "${OBS_PID}" ]] && stop_pid_gracefully "${OBS_PID}" INT 10
      OBS_PID=""
      ;;
  esac
}

stop_subscriber() {
  [[ -n "${SUB_PID}" ]] && stop_pid_gracefully "${SUB_PID}" TERM 5
  SUB_PID=""
}

cleanup() {
  [[ "${CLEANED_UP}" == "1" ]] && return
  CLEANED_UP=1
  normalize_tty
  echo "ABORT" | nc -N -w5 "${SYNC_HOST}" "${SYNC_PORT}" 2>/dev/null || true
  stop_observer
  stop_subscriber
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

start_sudo_keepalive
setup_platform_performance "${PLATFORM}"

echo "================================================"
echo " Experiment 3 (observation fidelity) - Laptop B (Observer Fidelity)"
echo " A host    : ${SYNC_HOST}"
echo " Platform  : ${PLATFORM}"
echo " Losses    : ${LOSSES[*]}"
echo " Conditions: ${CONDITIONS[*]}"
echo " Runs      : ${N_RUNS}"
echo " Topic     : ${TOPIC}"
echo " Workload  : ${HZ} Hz, ${PAYLOAD_BYTES} bytes, ${EXPECTED} messages"
echo "================================================"

FAILED=()

for loss in "${LOSSES[@]}"; do
  case ${loss} in 0|10|20) ;; *) echo "[ERROR] unsupported loss: ${loss}"; exit 1 ;; esac

  for condition in "${CONDITIONS[@]}"; do
    case ${condition} in rp_bag|rosbag2) ;; *) echo "[ERROR] unknown condition: ${condition}"; exit 1 ;; esac

    for i in $(seq 1 "${N_RUNS}"); do
      run=$(printf "%02d" "${i}")
      loss_dir=$(printf "loss%02d" "${loss}")
      CURRENT_LOSS="${loss}"
      CURRENT_CONDITION="${condition}"
      CURRENT_RUN="run${run}"
      outdir="${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/03_observation_fidelity/${loss_dir}/${condition}/run${run}"
      bagdir="${RP_EXP_BAGS_ROOT:-${REPO_DIR}/bags}/03_observation_fidelity/${loss_dir}/${condition}/run${run}"

      if [[ -e "${outdir}" || -e "${bagdir}" ]]; then
        echo "[ERROR] existing run output; select a new run label" >&2
        exit 1
      fi
      mkdir -p "${outdir}" "${bagdir}"
      rp_exp_begin_run "${outdir}"
      failures_before=${#FAILED[@]}

      {
        echo "date=$(date --iso-8601=seconds)"
        echo "platform=${PLATFORM}"
        echo "loss_pct=${loss}"
        echo "condition=${condition}"
        echo "hz=${HZ}"
        echo "payload_bytes=${PAYLOAD_BYTES}"
        echo "expected=${EXPECTED}"
        echo "topic=${TOPIC}"
      } > "${outdir}/platform.log"

      echo "[run] ${loss_dir}/${condition}/run${run}  $(date '+%H:%M:%S')"

      case ${condition} in
        rp_bag)
          remove_existing_output "${bagdir}/rp.mcap"
          rp_exp_prepare_runtime
          setsid env --default-signal=INT,QUIT sudo -n "${RP_BIN}" run > "${outdir}/obs.log" 2>&1 &
          RP_PID=$!
          wait_for_rp_socket
          setsid env --default-signal=INT,QUIT "${RP_BIN}" bag record "${TOPIC}" -o "${bagdir}/rp.mcap" >> "${outdir}/obs.log" 2>&1 &
          OBS_PID=$!
          ;;
        rosbag2)
          remove_existing_output "${bagdir}/rosbag2"
          setsid env --default-signal=INT,QUIT ros2 bag record "${TOPIC}" --storage mcap -o "${bagdir}/rosbag2" > "${outdir}/obs.log" 2>&1 &
          OBS_PID=$!
          ;;
      esac

      rp_exp_wait_log "${OBS_PID}" "${outdir}/obs.log" 'Recording\.\.\.' recorder
      sleep "${OBSERVER_LEAD_SEC}"

      setsid env --default-signal=INT,QUIT ros2 run rp_exp_fidelity drop_image_sub "${HZ}" "${EXPECTED}" "${PAYLOAD_BYTES}" "${TOPIC}" "${SUB_TIMEOUT}" \
        > "${outdir}/sub.log" 2>&1 &
      SUB_PID=$!

      sleep "${PRESTART_SEC}"
      rp_exp_wait_log "${SUB_PID}" "${outdir}/sub.log" 'SUB_READY' subscriber
      rp_exp_require_live "${OBS_PID}" recorder
      rp_exp_start_publisher "START ${condition} ${loss} run${run}"
      rp_exp_start_publisher "SET_LOSS ${loss}"
      printf 'loss_acknowledged=%s\nrequired_readers=%s\n' "${loss}" "$([[ "${condition}" == rosbag2 ]] && echo 2 || echo 1)" > "${outdir}/control.log"
      rp_exp_start_publisher GO

      if ! rp_exp_wait_subscriber "${SUB_PID}" "$((SUB_TIMEOUT + PRESTART_SEC + 30))"; then
        FAILED+=("${loss_dir}/${condition}/run${run}: subscriber")
        stop_subscriber
      fi
      SUB_PID=""

      rp_exp_start_publisher STOP
      printf 'publisher_completed=1800\n' >> "${outdir}/control.log"
      if ! rp_exp_require_live "${OBS_PID}" observer; then
        FAILED+=("${loss_dir}/${condition}/run${run}: observer")
      fi
      stop_observer
      recording="${bagdir}/rp.mcap"
      [[ "${condition}" == rosbag2 ]] && recording="${bagdir}/rosbag2"
      if ! rp_exp_validate_bag "${recording}" "${TOPIC}"; then
        FAILED+=("${loss_dir}/${condition}/run${run}: recording")
      fi

      if ! rp_exp_validate_subscriber "${outdir}/sub.log"; then
        FAILED+=("${loss_dir}/${condition}/run${run}: missing FINAL")
      fi
      if (( ${#FAILED[@]} == failures_before )); then
        rp_exp_complete_run "${outdir}"
      fi
      sleep 10
    done
  done
done

rp_exp_start_publisher DONE
stop_sudo_keepalive
trap - EXIT

echo ""
echo "================================================"
echo " Experiment 3 (observation fidelity) complete"
echo " Results: ${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/03_observation_fidelity"
echo " Bags   : ${RP_EXP_BAGS_ROOT:-${REPO_DIR}/bags}/03_observation_fidelity"
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo " Failed/suspect runs:"
  for f in "${FAILED[@]}"; do echo "  - ${f}"; done
fi
echo "================================================"

if (( ${#FAILED[@]} > 0 )); then
  exit 1
fi
