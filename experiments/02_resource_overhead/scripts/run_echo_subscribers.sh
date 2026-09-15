#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: run_echo_subscribers.sh --sync <peer-IP> [options]
Experiment: 02_resource_overhead
Options: --scenarios, --conditions, --runs, --platform, --sync, --port, --ack-port
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Receiver platform - Experiment 2 (resource overhead) echo-observer runner
#
# Usage:
#   ./experiments/02_resource_overhead/scripts/run_echo_subscribers.sh --sync <Publisher-IP> --platform <pc|rpi|jetson>
#   ./experiments/02_resource_overhead/scripts/run_echo_subscribers.sh --sync <Publisher-IP> --platform rpi --scenarios ST100 --conditions rp_echo,ros2_echo --runs 10

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
CONDITIONS=(rp_echo ros2_echo)
N_RUNS=10
PLATFORM=""
SYNC_HOST=""
SYNC_PORT=55201
SYNC_ACK_PORT=55202

while [[ $# -gt 0 ]]; do
  case $1 in
    --scenarios) IFS=',' read -ra SCENARIOS <<< "$2"; shift 2 ;;
    --conditions) IFS=',' read -ra CONDITIONS <<< "$2"; shift 2 ;;
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
TOPIC=${STRESS_TOPIC:-/stress}
TOPIC_TYPE=${STRESS_TOPIC_TYPE:-sensor_msgs/msg/Image}
ECHO_ARGS=${ECHO_ARGS:-}
PAYLOAD_BYTES=${STRESS_PAYLOAD_BYTES:-65536}
WARMUP_SEC=${WARMUP_SEC:-10}
MEASURE_SEC=${MEASURE_SEC:-60}
RP_BIN=${RP_BIN:-$(command -v rp || true)}
RP_SOCKET=${RP_SOCKET:-/tmp/ros2probe.sock}
CLK_TCK=$(getconf CLK_TCK)

SUDO_KEEPALIVE_PID=""
CURRENT_RUN_PID=""
CLEANED_UP=0

start_sudo_keepalive() {
  echo "[setup] Checking sudo credentials for unattended execution"
  sudo -v
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

stop_pid_gracefully() {
  local pid=${1:?}
  local signal=${2:-TERM}
  local timeout_s=${3:-5}
  local use_sudo=${4:-0}

  if [[ "${use_sudo}" == "1" ]]; then
    sudo -n kill "-${signal}" -- "-${pid}" 2>/dev/null || true
    sudo -n kill "-${signal}" "${pid}" 2>/dev/null || true
  fi
  kill "-${signal}" -- "-${pid}" 2>/dev/null || true
  kill "-${signal}" "${pid}" 2>/dev/null || true
  wait_pid_timeout "${pid}" "${timeout_s}" || {
    if [[ "${use_sudo}" == "1" ]]; then
      sudo -n kill -KILL -- "-${pid}" 2>/dev/null || true
      sudo -n kill -KILL "${pid}" 2>/dev/null || true
    fi
    kill -KILL -- "-${pid}" 2>/dev/null || true
    kill -KILL "${pid}" 2>/dev/null || true
  }
  wait "${pid}" 2>/dev/null || true
}

stop_current_run() {
  if [[ -n "${CURRENT_RUN_PID}" ]]; then
    stop_pid_gracefully "${CURRENT_RUN_PID}" INT 8
    CURRENT_RUN_PID=""
  fi
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

wait_for_rp_socket() {
  rp_exp_wait_runtime "${RP_PID:-${rp_pid:-}}" "${1:-100}"
}

write_platform_log() {
  local outdir=${1:?}
  local scenario=${2:?}
  local condition=${3:?}
  local hz=${4:?}

  {
    echo "platform=${PLATFORM}"
    echo "scenario=${scenario}"
    echo "condition=${condition}"
    echo "hz=${hz}"
    echo "payload_bytes=${PAYLOAD_BYTES}"
    echo "topic=${TOPIC}"
    echo "topic_type=${TOPIC_TYPE}"
    echo "echo_args=${ECHO_ARGS}"
    echo "ros_domain_id=${ROS_DOMAIN_ID}"
    echo "nic=${NIC}"
    echo "date=$(date -Is)"
    echo "kernel=$(uname -a)"
    echo "logical_cores=$(nproc)"
    if command -v cpupower >/dev/null 2>&1; then
      cpupower frequency-info -p 2>/dev/null || true
    fi
    for cpu_dir in /sys/devices/system/cpu/cpu[0-9]*; do
      [[ -d "${cpu_dir}/cpufreq" ]] || continue
      cpu_name=$(basename "${cpu_dir}")
      [[ -r "${cpu_dir}/cpufreq/scaling_governor" ]] && echo "${cpu_name}_governor=$(cat "${cpu_dir}/cpufreq/scaling_governor")"
      [[ -r "${cpu_dir}/cpufreq/scaling_cur_freq" ]] && echo "${cpu_name}_cur_freq=$(cat "${cpu_dir}/cpufreq/scaling_cur_freq")"
      [[ -r "${cpu_dir}/cpufreq/scaling_min_freq" ]] && echo "${cpu_name}_min_freq=$(cat "${cpu_dir}/cpufreq/scaling_min_freq")"
      [[ -r "${cpu_dir}/cpufreq/scaling_max_freq" ]] && echo "${cpu_name}_max_freq=$(cat "${cpu_dir}/cpufreq/scaling_max_freq")"
    done
    if command -v nvpmodel >/dev/null 2>&1; then
      sudo -n nvpmodel -q 2>/dev/null | sed 's/^/nvpmodel: /' || true
    fi
    if command -v jetson_clocks >/dev/null 2>&1; then
      sudo -n jetson_clocks --show 2>/dev/null | sed 's/^/jetson_clocks: /' || true
    fi
  } > "${outdir}/platform.log"
}

run_one() {
  local scenario=${1:?}
  local condition=${2:?}
  local run_no=${3:?}
  local run
  local hz
  local outdir
  local obs_pid=""
  local rp_pid=""
  local sub_pid=""
  local netdev_pid=""
  local observer_cpu_pid=""
  local publisher_started=0
  local stop_sent=0
  local run_cleaned_up=0

  run=$(printf "%02d" "${run_no}")
  case ${scenario} in
    ST100) hz=100 ;;
    ST500) hz=500 ;;
    ST1000) hz=1000 ;;
    *) echo "[ERROR] unknown scenario: ${scenario}"; return 1 ;;
  esac

  case ${condition} in
    rp_echo|ros2_echo) ;;
    *) echo "[ERROR] unknown condition: ${condition}"; return 1 ;;
  esac

  outdir="${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/02_resource_overhead/echo/${PLATFORM}/${scenario}/${condition}/run${run}"
  mkdir -p "${outdir}"
  rp_exp_begin_run "${outdir}"
  echo "[overhead_echo] ${PLATFORM}/${scenario}/${condition}/run${run}  NIC=${NIC}"

  cleanup_run() {
    [[ "${run_cleaned_up}" == "1" ]] && return
    run_cleaned_up=1
    normalize_tty
    if [[ "${publisher_started}" == "1" && "${stop_sent}" == "0" ]]; then
      echo "STOP" | nc -N -w5 "${SYNC_HOST}" "${SYNC_PORT}" 2>/dev/null || true
      stop_sent=1
    fi
    [[ -n "${observer_cpu_pid}" ]] && stop_pid_gracefully "${observer_cpu_pid}" TERM 2 1
    [[ -n "${netdev_pid}" ]] && stop_pid_gracefully "${netdev_pid}" TERM 2
    [[ -n "${sub_pid}" ]] && stop_pid_gracefully "${sub_pid}" INT 2
    [[ -n "${obs_pid}" ]] && stop_pid_gracefully "${obs_pid}" INT 10
    [[ -n "${rp_pid}" ]] && stop_pid_gracefully "${rp_pid}" INT 5 1
    [[ -n "${sub_pid}" ]] && stop_pid_gracefully "${sub_pid}" TERM 3
    normalize_tty
  }
  trap cleanup_run EXIT
  trap 'cleanup_run; exit 130' INT TERM

  write_platform_log "${outdir}" "${scenario}" "${condition}" "${hz}"
  ros2 daemon stop 2>/dev/null || true

  case ${condition} in
    ros2_echo)
      echo "ros2 topic echo ${ECHO_ARGS} ${TOPIC} ${TOPIC_TYPE}" > "${outdir}/obs.log"
      setsid env --default-signal=INT,QUIT ros2 topic echo ${ECHO_ARGS} "${TOPIC}" "${TOPIC_TYPE}" > /dev/null 2>> "${outdir}/obs.log" &
      obs_pid=$!
      ;;
    rp_echo)
      rp_exp_prepare_runtime
      setsid env --default-signal=INT,QUIT sudo -n "${RP_BIN}" run > "${outdir}/obs.log" 2>&1 &
      rp_pid=$!
      if ! wait_for_rp_socket 100; then
        return 1
      fi
      echo "${RP_BIN} topic echo ${ECHO_ARGS} ${TOPIC} ${TOPIC_TYPE}" >> "${outdir}/obs.log"
      setsid env --default-signal=INT,QUIT "${RP_BIN}" topic echo ${ECHO_ARGS} "${TOPIC}" "${TOPIC_TYPE}" > /dev/null 2>> "${outdir}/obs.log" &
      obs_pid=$!
      ;;
  esac


  setsid env --default-signal=INT,QUIT ros2 run rp_exp_overhead stress_sub "${hz}" "${WARMUP_SEC}" "${MEASURE_SEC}" "${TOPIC}" > "${outdir}/sub.log" 2>&1 &
  sub_pid=$!


  local rx_bytes_path="/sys/class/net/${NIC}/statistics/rx_bytes"
  if [[ ! -r "${rx_bytes_path}" ]]; then
    echo "[ERROR] netdev rx counter not readable for NIC=${NIC}: ${rx_bytes_path}" >&2
    return 1
  fi

  (
    while true; do
      echo "$(date +%s%3N) $(cat "${rx_bytes_path}")"
      sleep 1
    done
  ) > "${outdir}/netdev.log" &
  netdev_pid=$!

  sampler_sessions=()
  [[ -n "${rp_pid}" ]] && sampler_sessions+=("${rp_pid}")
  [[ -n "${obs_pid}" ]] && sampler_sessions+=("${obs_pid}")
  setsid env --default-signal=INT,QUIT sudo -n python3 "${REPO_DIR}/common/sample_resources.py" \
    --sessions "${sampler_sessions[@]}" > "${outdir}/observer_cpu.log" 2> "${outdir}/sampler.log" &
  observer_cpu_pid=$!

  echo "  [sync] sending START ${scenario}..."
  publisher_started=1
  rp_exp_start_publisher "START ${scenario}"
  echo "  [sync] Publisher ready  $(date '+%H:%M:%S')"


  rp_exp_wait_subscriber "${sub_pid}" "${SUB_TIMEOUT_SEC:-$((WARMUP_SEC + MEASURE_SEC + 30))}"
  rp_exp_validate_subscriber "${outdir}/sub.log"
  rp_exp_require_live "${obs_pid}" observer
  if [[ -n "${rp_pid}" ]]; then rp_exp_require_live "${rp_pid}" 'rp runtime'; fi
  rp_exp_require_live "${observer_cpu_pid}" resource-sampler
  sleep 1.1  # Final NIC/resource sample must bracket the measurement end.
  echo "  [sub] complete  $(date '+%H:%M:%S')"

  echo "STOP" | nc -N -w5 "${SYNC_HOST}" "${SYNC_PORT}" 2>/dev/null || true
  stop_sent=1
  cleanup_run
  rp_exp_complete_run "${outdir}"
  trap - EXIT INT TERM
  ros2 daemon stop 2>/dev/null || true
  normalize_tty
  echo "[overhead_echo] run${run} done -> ${outdir}"
  sleep 10
}

trap cleanup EXIT
trap handle_signal INT TERM

SECS_PER_RUN=84
TOTAL_SECS=$(( ${#SCENARIOS[@]} * ${#CONDITIONS[@]} * N_RUNS * SECS_PER_RUN ))
TOTAL_H=$(( TOTAL_SECS / 3600 ))
TOTAL_M=$(( (TOTAL_SECS % 3600) / 60 ))

echo "================================================"
echo " Experiment 2 (resource overhead) Echo - Receiver Platform"
echo " Platform  : ${PLATFORM}"
echo " Scenarios : ${SCENARIOS[*]}"
echo " Conditions: ${CONDITIONS[*]}"
echo " Runs      : ${N_RUNS}"
echo " NIC       : ${NIC}"
echo " Sync      : event-driven (publisher=${SYNC_HOST})"
echo " ROS_DOMAIN: ${ROS_DOMAIN_ID}"
echo " Results   : ${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/02_resource_overhead/echo/${PLATFORM}/"
echo " Estimate  : ${TOTAL_H}h ${TOTAL_M}m"
echo "================================================"
echo ""

start_sudo_keepalive
setup_platform_performance "${PLATFORM}"

FAILED=()
START_TIME=$(date +%s)

for scenario in "${SCENARIOS[@]}"; do
  echo ""
  echo "================================================"
  echo " [$(date '+%H:%M:%S')] Scenario: ${scenario}"
  echo "================================================"

  for condition in "${CONDITIONS[@]}"; do
    echo ""
    echo "  -- ${scenario} / ${condition} ------------------"
    for i in $(seq 1 "${N_RUNS}"); do
      RUN_LABEL="$(printf '%02d' "${i}")/${N_RUNS}"
      echo "    run ${RUN_LABEL}  ($(date '+%H:%M:%S'))"
      run_one "${scenario}" "${condition}" "${i}" &
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
        FAILED+=("${PLATFORM}/${scenario}/${condition}/run$(printf '%02d' "${i}")")
      fi
    done
    echo "  [finished] ${condition}"
  done
done

echo "DONE" | nc -N -w5 "${SYNC_HOST}" "${SYNC_PORT}" 2>/dev/null || true
echo "  [sync] DONE sent"

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo ""
echo "================================================"
echo " Experiment 2 (resource overhead) Echo complete  $(date '+%H:%M:%S')"
echo " Elapsed : $(( TOTAL_ELAPSED/3600 ))h $(( (TOTAL_ELAPSED%3600)/60 ))m"
echo " Results : ${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/02_resource_overhead/echo/${PLATFORM}/"
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo " Failed runs (rerun required):"
  for f in "${FAILED[@]}"; do echo "   - ${f}"; done
fi
echo "================================================"

if (( ${#FAILED[@]} > 0 )); then
  exit 1
fi
