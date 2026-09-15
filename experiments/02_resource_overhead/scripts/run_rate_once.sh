#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: run_rate_once.sh <platform> <scenario> <condition> <run>
Experiment: 02_resource_overhead
Called by the corresponding experiment runner.
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Receiver Platform - Experiment 2 (resource overhead) single run
# usage: ./run_rate_once.sh <platform> <scenario> <condition> <run>
# scenarios: ST100 | ST500 | ST1000
# conditions: baseline | rp_hz | topic_hz

set -euo pipefail

normalize_tty() {
  [[ -t 1 ]] && stty sane opost onlcr 2>/dev/null || true
}

normalize_tty

source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/common/environment.sh"
rp_exp_load_environment

export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-77}

PLATFORM=${1:?usage: $0 <platform> <scenario> <condition> <run>}
SCENARIO=${2:?}
CONDITION=${3:?}
RUN=$(printf "%02d" "${4:?}")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
OUTDIR="${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/02_resource_overhead/rate/${PLATFORM}/${SCENARIO}/${CONDITION}/run${RUN}"

NIC=${NIC:-$(ip route show default | awk '/default/ {print $5; exit}')}
SYNC_HOST=${SYNC_HOST:-""}
SYNC_PORT=${SYNC_PORT:-55001}
SYNC_ACK_PORT=${SYNC_ACK_PORT:-55002}
RP_BIN=${RP_BIN:-$(command -v rp || true)}
RP_SOCKET=${RP_SOCKET:-/tmp/ros2probe.sock}
TOPIC=${STRESS_TOPIC:-/stress}
PAYLOAD_BYTES=${STRESS_PAYLOAD_BYTES:-65536}
WARMUP_SEC=${WARMUP_SEC:-10}
MEASURE_SEC=${MEASURE_SEC:-60}
CLK_TCK=$(getconf CLK_TCK)

case ${SCENARIO} in
  ST100)  HZ=100 ;;
  ST500)  HZ=500 ;;
  ST1000) HZ=1000 ;;
  *) echo "[ERROR] unknown scenario: ${SCENARIO}"; exit 1 ;;
esac

OBS_PID=""
RP_PID=""
SUB_PID=""
NETDEV_PID=""
OBSERVER_CPU_PID=""
PUBLISHER_STARTED=0
STOP_SENT=0
CLEANED_UP=0

wait_for_rp_socket() {
  rp_exp_wait_runtime "${RP_PID:-${rp_pid:-}}" "${1:-100}"
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

stop_rp_runtime() {
  if [[ -n "${RP_PID}" ]]; then
    stop_pid_gracefully "${RP_PID}" INT 5 1
  fi
}

cleanup_on_exit() {
  [[ "${CLEANED_UP}" == "1" ]] && return
  CLEANED_UP=1
  normalize_tty

  if [[ "${PUBLISHER_STARTED}" == "1" && "${STOP_SENT}" == "0" && -n "${SYNC_HOST}" ]]; then
    echo "STOP" | nc -N -w5 "${SYNC_HOST}" "${SYNC_PORT}" 2>/dev/null || true
    STOP_SENT=1
  fi

  [[ -n "${OBSERVER_CPU_PID}" ]] && stop_pid_gracefully "${OBSERVER_CPU_PID}" TERM 2 1
  [[ -n "${NETDEV_PID}" ]] && stop_pid_gracefully "${NETDEV_PID}" TERM 2
  [[ -n "${SUB_PID}" ]] && stop_pid_gracefully "${SUB_PID}" INT 2
  [[ -n "${OBS_PID}" ]] && stop_pid_gracefully "${OBS_PID}" INT 5
  [[ -n "${RP_PID}" ]] && stop_rp_runtime
  [[ -n "${SUB_PID}" ]] && stop_pid_gracefully "${SUB_PID}" TERM 3
  normalize_tty
}

handle_signal() {
  trap - INT TERM
  normalize_tty
  echo ""
  echo "[interrupt] run stop requested"
  cleanup_on_exit
  exit 130
}

trap cleanup_on_exit EXIT
trap handle_signal INT TERM

mkdir -p "${OUTDIR}"
rp_exp_begin_run "${OUTDIR}"
echo "[overhead_rate] ${PLATFORM}/${SCENARIO}/${CONDITION}/run${RUN}  NIC=${NIC}  outdir=${OUTDIR}"

{
  echo "platform=${PLATFORM}"
  echo "scenario=${SCENARIO}"
  echo "condition=${CONDITION}"
  echo "hz=${HZ}"
  echo "payload_bytes=${PAYLOAD_BYTES}"
  echo "topic=${TOPIC}"
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
} > "${OUTDIR}/platform.log"

ros2 daemon stop 2>/dev/null || true

case ${CONDITION} in
  topic_hz)
    setsid env --default-signal=INT,QUIT PYTHONUNBUFFERED=1 ros2 topic hz "${TOPIC}" > "${OUTDIR}/obs.log" 2>&1 &
    OBS_PID=$!
    ;;
  rp_hz)
    rp_exp_prepare_runtime
    setsid env --default-signal=INT,QUIT sudo -n "${RP_BIN}" run > "${OUTDIR}/obs.log" 2>&1 &
    RP_PID=$!
    if ! wait_for_rp_socket 100; then
      stop_rp_runtime
      exit 1
    fi
    setsid env --default-signal=INT,QUIT "${RP_BIN}" topic hz "${TOPIC}" >> "${OUTDIR}/obs.log" 2>&1 &
    OBS_PID=$!
    ;;
  baseline)
    ;;
  *)
    echo "[ERROR] unknown condition: ${CONDITION}"; exit 1 ;;
esac



setsid env --default-signal=INT,QUIT ros2 run rp_exp_overhead stress_sub "${HZ}" "${WARMUP_SEC}" "${MEASURE_SEC}" "${TOPIC}" > "${OUTDIR}/sub.log" 2>&1 &
SUB_PID=$!


RX_BYTES_PATH="/sys/class/net/${NIC}/statistics/rx_bytes"
if [[ ! -r "${RX_BYTES_PATH}" ]]; then
  echo "[ERROR] netdev rx counter not readable for NIC=${NIC}: ${RX_BYTES_PATH}" >&2
  exit 1
fi

(
  while true; do
    echo "$(date +%s%3N) $(cat "${RX_BYTES_PATH}")"
    sleep 1
  done
) > "${OUTDIR}/netdev.log" &
NETDEV_PID=$!

sampler_sessions=()
[[ -n "${RP_PID}" ]] && sampler_sessions+=("${RP_PID}")
[[ -n "${OBS_PID}" ]] && sampler_sessions+=("${OBS_PID}")
setsid env --default-signal=INT,QUIT sudo -n python3 "${REPO_DIR}/common/sample_resources.py" \
  --sessions "${sampler_sessions[@]}" > "${OUTDIR}/observer_cpu.log" 2> "${OUTDIR}/sampler.log" &
OBSERVER_CPU_PID=$!

rp_exp_wait_log "${SUB_PID}" "${OUTDIR}/sub.log" 'SUB_READY' subscriber
rp_exp_wait_log "${NETDEV_PID}" "${OUTDIR}/netdev.log" '^[0-9]+ [0-9]+$' NIC-sampler
rp_exp_wait_log "${OBSERVER_CPU_PID}" "${OUTDIR}/observer_cpu.log" '^[0-9]+ ' resource-sampler
if [[ -n "${OBS_PID}" ]]; then rp_exp_require_live "${OBS_PID}" observer; fi

if [[ -n "${SYNC_HOST}" ]]; then
  echo "  [sync] sending START ${SCENARIO}..."
  PUBLISHER_STARTED=1
  rp_exp_start_publisher "START ${SCENARIO}"
  echo "  [sync] Publisher ready  $(date '+%H:%M:%S')"
else
  echo "  [warn] SYNC_HOST is not set; publisher must already be running"
fi

rp_exp_wait_subscriber "${SUB_PID}" "${SUB_TIMEOUT_SEC:-$((WARMUP_SEC + MEASURE_SEC + 30))}"
rp_exp_validate_subscriber "${OUTDIR}/sub.log"
if [[ -n "${OBS_PID}" ]]; then rp_exp_require_live "${OBS_PID}" observer; fi
if [[ -n "${RP_PID}" ]]; then rp_exp_require_live "${RP_PID}" 'rp runtime'; fi
rp_exp_require_live "${OBSERVER_CPU_PID}" resource-sampler
sleep 1.1  # Final NIC/resource sample must bracket the measurement end.
echo "  [sub] complete  $(date '+%H:%M:%S')"

if [[ -n "${SYNC_HOST}" ]]; then
  echo "STOP" | nc -N -w5 "${SYNC_HOST}" "${SYNC_PORT}" 2>/dev/null || true
  STOP_SENT=1
fi

stop_pid_gracefully "${OBSERVER_CPU_PID}" TERM 3 1
OBSERVER_CPU_PID=""
case ${CONDITION} in
  rp_hz)
    [[ -n "${OBS_PID}" ]] && stop_pid_gracefully "${OBS_PID}" INT 10
    stop_rp_runtime
    ;;
  topic_hz)
    [[ -n "${OBS_PID}" ]] && stop_pid_gracefully "${OBS_PID}" INT 5
    ;;
esac

[[ -n "${OBSERVER_CPU_PID}" ]] && stop_pid_gracefully "${OBSERVER_CPU_PID}" TERM 3 1
[[ -n "${NETDEV_PID}" ]] && stop_pid_gracefully "${NETDEV_PID}" TERM 3
[[ -n "${SUB_PID}" ]] && stop_pid_gracefully "${SUB_PID}" INT 2
[[ -n "${SUB_PID}" ]] && stop_pid_gracefully "${SUB_PID}" TERM 5
ros2 daemon stop 2>/dev/null || true
normalize_tty

echo "[overhead_rate] run${RUN} done -> ${OUTDIR}"
rp_exp_complete_run "${OUTDIR}"
trap - EXIT
sleep 10
