#!/bin/bash

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: run_once.sh <scenario> <condition> <run>
Experiment: 04_probe_effect
Called by the corresponding experiment runner.
ROS_SETUP selects a ROS setup file; otherwise ROS_DISTRO (default humble) is used.
See the experiment README.md for arguments, timing and output locations.
USAGE
  exit 0
fi
# Laptop B - single run
# usage: ./run_once.sh <scenario> <condition> <run>
# e.g.:  ./run_once.sh S3 rosbag2_one 1
#
# conditions: baseline | rp_hz | rp_bag_one | rp_bag_all | topic_hz | rosbag2_one | rosbag2_all
#
# Environment variables:
#   SYNC_HOST      : Laptop A wlan IP. Enables per-run publisher sync when set.
#   SYNC_PORT      : B -> A port. Default: 55001.
#   SYNC_ACK_PORT  : A -> B port. Default: 55002.
#   NIC            : network interface to measure. Auto-detected when unset.

set -euo pipefail

normalize_tty() {
  [[ -t 1 ]] && stty sane opost onlcr 2>/dev/null || true
}

normalize_tty

# Source ROS when it is not already available.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/common/environment.sh"
rp_exp_load_environment

export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-77}

SCENARIO=${1:?usage: $0 <scenario> <condition> <run>}
CONDITION=${2:?}
RUN=$(printf "%02d" "${3:?}")

case ${CONDITION} in
  rp_bag|rosbag2)
    # Recording includes every topic in the paper workload.
    CONDITION="${CONDITION}_all"
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

NIC=${NIC:-$(ip route show default | awk '/default/ {print $5; exit}')}
SYNC_HOST=${SYNC_HOST:-""}
SYNC_PORT=${SYNC_PORT:-55001}
SYNC_ACK_PORT=${SYNC_ACK_PORT:-55002}
PROBE_QOS=${PROBE_QOS:-best_effort}
PROBE_LINK=${PROBE_LINK:-wired}
case ${PROBE_QOS} in
  best_effort) QOS_SUFFIX=""; LAUNCH_SUFFIX=""; QOS_NAME="best_effort" ;;
  reliable)    QOS_SUFFIX="_rel"; LAUNCH_SUFFIX="_rel"; QOS_NAME="reliable" ;;
  *) echo "[ERROR] unknown PROBE_QOS: ${PROBE_QOS}"; exit 1 ;;
esac
case ${PROBE_LINK} in
  wired|wireless) ;;
  *) echo "[ERROR] unknown PROBE_LINK: ${PROBE_LINK}"; exit 1 ;;
esac
RESULTS_NAME="04_probe_effect/${PROBE_LINK}_${QOS_NAME}"
OUTDIR="${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/${RESULTS_NAME}/${SCENARIO}/${CONDITION}/run${RUN}"
BAGDIR="${RP_EXP_BAGS_ROOT:-${REPO_DIR}/bags}/${RESULTS_NAME}/${SCENARIO}/${CONDITION}/run${RUN}"

RP_BIN=${RP_BIN:-$(command -v rp || true)}
RP_SOCKET=${RP_SOCKET:-/tmp/ros2probe.sock}
OBS_PID=""
RP_PID=""
SUB_PID=""
SUB_FRONT_PID=""
NETDEV_PID=""
PUBLISHER_STARTED=0
STOP_SENT=0
CLEANED_UP=0
SUB_TIMEOUT_SEC=${SUB_TIMEOUT_SEC:-120}

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

wait_pid_until_deadline() {
  local pid=${1:?}
  local label=${2:?}
  local deadline=${3:?}
  local now

  while true; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      wait "${pid}"
      return $?
    fi
    now=$(date +%s)
    if (( now >= deadline )); then
      echo "[WARN] ${label} did not finish within ${SUB_TIMEOUT_SEC}s" >&2
      return 1
    fi
    sleep 1
  done
}

wait_subscribers() {
  local deadline
  local status=0

  deadline=$(( $(date +%s) + SUB_TIMEOUT_SEC ))
  if [[ -n "${SUB_PID}" ]]; then
    wait_pid_until_deadline "${SUB_PID}" "subscriber" "${deadline}" || status=1
  fi
  if [[ -n "${SUB_FRONT_PID}" ]]; then
    wait_pid_until_deadline "${SUB_FRONT_PID}" "front points subscriber" "${deadline}" || status=1
  fi
  return "${status}"
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
    # `rp run` is started via sudo, so signal the run's process group with sudo.
    stop_pid_gracefully "${RP_PID}" INT 5 1
    RP_PID=""
  fi
}

stop_samplers() {
  [[ -n "${NETDEV_PID}" ]] && stop_pid_gracefully "${NETDEV_PID}" TERM 3
  NETDEV_PID=""
}

stop_observer() {
  case ${CONDITION} in
    rp_hz|rp_bag_one|rp_bag_all)
      # rp CLI sends TopicHzStop/BagStop only on Ctrl-C (SIGINT), not SIGTERM.
      [[ -n "${OBS_PID}" ]] && stop_pid_gracefully "${OBS_PID}" INT 10
      OBS_PID=""
      stop_rp_runtime
      ;;
    rosbag2_one|rosbag2_all)
      # Let rosbag2 close metadata/storage cleanly.
      [[ -n "${OBS_PID}" ]] && stop_pid_gracefully "${OBS_PID}" INT 10
      OBS_PID=""
      ;;
    topic_hz)
      [[ -n "${OBS_PID}" ]] && stop_pid_gracefully "${OBS_PID}" INT 5
      OBS_PID=""
      ;;
    baseline)
      ;;
  esac
}

stop_subscribers() {
  [[ -n "${SUB_FRONT_PID}" ]] && stop_pid_gracefully "${SUB_FRONT_PID}" TERM 5
  SUB_FRONT_PID=""
  [[ -n "${SUB_PID}" ]] && stop_pid_gracefully "${SUB_PID}" TERM 5
  SUB_PID=""
}

remove_existing_output() {
  local path=${1:?}

  if [[ -e "${path}" || -L "${path}" ]]; then
    rm -rf -- "${path}" 2>/dev/null || sudo -n rm -rf -- "${path}"
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

  stop_samplers
  stop_observer
  stop_subscribers
}

handle_signal() {
  trap - INT TERM
  normalize_tty
  echo ""
  echo "[interrupt] run stop requested"
  exit 130
}

trap cleanup_on_exit EXIT
trap handle_signal INT TERM

# Scenario-specific configuration.
SUB_LAUNCH=""
case ${SCENARIO} in
  S1) SUB_NODE="s2_sub${QOS_SUFFIX}";        TOPIC="/imu" ;;
  S2) SUB_NODE="s3a_sub${QOS_SUFFIX}";       TOPIC="/scan" ;;
  S3) SUB_NODE="s3_points_sub${QOS_SUFFIX}"; TOPIC="/points" ;;
  S4) SUB_NODE="s3_points_sub${QOS_SUFFIX}"; TOPIC="/points" ;;
  S5) SUB_NODE="s4a_sub${QOS_SUFFIX}";       TOPIC="/image_raw/compressed" ;;
  S6) SUB_NODE="s4_image_sub${QOS_SUFFIX}";  TOPIC="/depth/image_raw" ;;
  S7) SUB_NODE="";                           SUB_LAUNCH="s5b_sub${LAUNCH_SUFFIX}.launch.py"; TOPIC="/points/front" ;;
  *) echo "[ERROR] unknown scenario: ${SCENARIO}"; exit 1 ;;
esac

BAG_MODE="one"
case ${CONDITION} in
  rp_bag_one|rosbag2_one)
    BAG_MODE="one"
    ;;
  rp_bag_all|rosbag2_all)
    BAG_MODE="all"
    ;;
esac

REQUIRED_BAG_TOPICS=("${TOPIC}")
if [[ "${SCENARIO}" == S7 && "${BAG_MODE}" == all ]]; then
  REQUIRED_BAG_TOPICS=(/cmd_vel /imu /points/front /points/rear
    /camera/front/compressed /camera/left/compressed /camera/right/compressed
    /camera/rear/compressed /depth/image_raw)
fi
ROS2_BAG_ARGS=("${TOPIC}")
RP_BAG_ARGS=("${TOPIC}")
if [[ "${BAG_MODE}" == "all" ]]; then
  ROS2_BAG_ARGS=(-a)
  RP_BAG_ARGS=(--all)
fi

mkdir -p "${OUTDIR}"
rp_exp_begin_run "${OUTDIR}"
printf 'scenario=%s\ncondition=%s\nlink=%s\nqos=%s\nnic=%s\nmeasure_sec=%s\nwarmup_sec=%s\n' \
  "${SCENARIO}" "${CONDITION}" "${PROBE_LINK}" "${PROBE_QOS}" "${NIC}" \
  "${RP_EXP_MEASURE_SEC:-60}" "${RP_EXP_WARMUP_SEC:-0}" > "${OUTDIR}/platform.log"
echo "[probe_effect] ${SCENARIO}/${CONDITION}/run${RUN}  link=${PROBE_LINK}  QoS=${PROBE_QOS}  NIC=${NIC}  outdir=${OUTDIR}"

# Remove stale ros2 daemon state.
ros2 daemon stop 2>/dev/null || true

# Step 1: start the observer before DDS discovery.
case ${CONDITION} in
  topic_hz)
    setsid env --default-signal=INT,QUIT PYTHONUNBUFFERED=1 ros2 topic hz "${TOPIC}" > "${OUTDIR}/obs.log" 2>&1 &
    OBS_PID=$!
    ;;
  rosbag2_one|rosbag2_all)
    mkdir -p "${BAGDIR}"
    remove_existing_output "${BAGDIR}/rosbag2"
    setsid env --default-signal=INT,QUIT ros2 bag record "${ROS2_BAG_ARGS[@]}" --storage mcap -o "${BAGDIR}/rosbag2" > "${OUTDIR}/obs.log" 2>&1 &
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
  rp_bag_one|rp_bag_all)
    mkdir -p "${BAGDIR}"
    remove_existing_output "${BAGDIR}/rp.mcap"
    rp_exp_prepare_runtime
    setsid env --default-signal=INT,QUIT sudo -n "${RP_BIN}" run > "${OUTDIR}/obs.log" 2>&1 &
    RP_PID=$!
    if ! wait_for_rp_socket 100; then
      stop_rp_runtime
      exit 1
    fi
    setsid env --default-signal=INT,QUIT "${RP_BIN}" bag record "${RP_BAG_ARGS[@]}" -o "${BAGDIR}/rp.mcap" >> "${OUTDIR}/obs.log" 2>&1 &
    OBS_PID=$!
    ;;
  baseline)
    ;;
  *)
    echo "[ERROR] unknown condition: ${CONDITION}"; exit 1
    ;;
esac

case ${CONDITION} in
  rosbag2_*|rp_bag_*) rp_exp_wait_log "${OBS_PID}" "${OUTDIR}/obs.log" 'Recording\.\.\.' recorder ;;
esac

# Step 2: start subscriber in the background before releasing the publisher.
# The subscriber records a 60s interval from the first callback; discovery precedes the window.
if [[ "${SCENARIO}" == "S7" ]]; then
  setsid env --default-signal=INT,QUIT ros2 launch rp_exp_probe_effect "${SUB_LAUNCH}" include_points_front:=false > "${OUTDIR}/sub.log" 2>&1 &
  SUB_PID=$!
  setsid env --default-signal=INT,QUIT ros2 run rp_exp_probe_effect "s3_points_sub${QOS_SUFFIX}" --ros-args -r /points:=/points/front -r __node:=points_front_subscriber \
    > "${OUTDIR}/sub_points_front.log" 2>&1 &
  SUB_FRONT_PID=$!
elif [[ -n "${SUB_LAUNCH}" ]]; then
  setsid env --default-signal=INT,QUIT ros2 launch rp_exp_probe_effect "${SUB_LAUNCH}" > "${OUTDIR}/sub.log" 2>&1 &
  SUB_PID=$!
else
  setsid env --default-signal=INT,QUIT ros2 run rp_exp_probe_effect "${SUB_NODE}" > "${OUTDIR}/sub.log" 2>&1 &
  SUB_PID=$!
fi

# Step 3: start samplers before releasing the publisher, so publisher startup is captured.
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

# Step 4: request publisher start on Laptop A.
if [[ "${SCENARIO}" == S7 ]]; then
  rp_exp_wait_log "${SUB_PID}" "${OUTDIR}/sub.log" 'SUB_READY' subscribers 8
  rp_exp_wait_log "${SUB_FRONT_PID}" "${OUTDIR}/sub_points_front.log" 'SUB_READY' front-subscriber
else
  rp_exp_wait_log "${SUB_PID}" "${OUTDIR}/sub.log" 'SUB_READY' subscriber
fi
rp_exp_wait_log "${NETDEV_PID}" "${OUTDIR}/netdev.log" '^[0-9]+ [0-9]+$' NIC-sampler
if [[ -n "${OBS_PID}" ]]; then rp_exp_require_live "${OBS_PID}" observer; fi

if [[ -n "${SYNC_HOST}" ]]; then
  echo "  [sync] sending START ${SCENARIO} ${PROBE_QOS}..."
  PUBLISHER_STARTED=1
  rp_exp_start_publisher "START ${SCENARIO} ${PROBE_QOS}"
  echo "  [sync] Publisher ready  $(date '+%H:%M:%S')"
else
  echo "  [warn] SYNC_HOST is not set; publisher must already be running"
fi

# Step 5: wait for subscriber completion. It exits after the 60s measurement.
RUN_STATUS=0
if wait_subscribers; then
  if [[ "${SCENARIO}" == S7 ]]; then
    rp_exp_validate_subscriber "${OUTDIR}/sub.log" 8 || RUN_STATUS=1
    rp_exp_validate_subscriber "${OUTDIR}/sub_points_front.log" || RUN_STATUS=1
  else
    rp_exp_validate_subscriber "${OUTDIR}/sub.log" || RUN_STATUS=1
  fi
  echo "  [sub] complete  $(date '+%H:%M:%S')"
else
  echo "  [sub] failed or timed out  $(date '+%H:%M:%S')" >&2
  RUN_STATUS=1
fi
if [[ -n "${OBS_PID}" ]]; then
  rp_exp_require_live "${OBS_PID}" observer || RUN_STATUS=1
fi
if [[ -n "${RP_PID}" ]]; then
  rp_exp_require_live "${RP_PID}" 'rp runtime' || RUN_STATUS=1
fi

sleep 1.1  # Keep a NIC sample beyond the exact measurement deadline.

# Step 6: request publisher stop on Laptop A.
if [[ -n "${SYNC_HOST}" ]]; then
  echo "STOP" | nc -N -w5 "${SYNC_HOST}" "${SYNC_PORT}" 2>/dev/null || true
  STOP_SENT=1
fi

# Step 7: clean up run-local background processes in shutdown order.
stop_samplers
stop_observer
case "${CONDITION}" in
  rp_bag_one|rp_bag_all)
    rp_exp_validate_bag "${BAGDIR}/rp.mcap" "${REQUIRED_BAG_TOPICS[@]}" || RUN_STATUS=1 ;;
  rosbag2_one|rosbag2_all)
    rp_exp_validate_bag "${BAGDIR}/rosbag2" "${REQUIRED_BAG_TOPICS[@]}" || RUN_STATUS=1 ;;
esac

# Step 8: clean up the subscriber/launch group started by this run.
stop_subscribers
ros2 daemon stop 2>/dev/null || true

echo "[probe_effect] run${RUN} done -> ${OUTDIR}"
if (( RUN_STATUS == 0 )); then rp_exp_complete_run "${OUTDIR}"; fi
trap - EXIT
sleep 10
exit "${RUN_STATUS}"
