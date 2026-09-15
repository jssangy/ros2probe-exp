#!/usr/bin/env bash
# Experiment 3: acknowledge discovery readiness, loss installation and publication.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
source "${REPO_DIR}/common/environment.sh"
SYNC_HOST=""
SYNC_PORT=56001
SYNC_ACK_PORT=56002
NIC=${NIC:-}
PLATFORM=pc
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sync) SYNC_HOST="$2"; shift 2 ;;
    --port) SYNC_PORT="$2"; shift 2 ;;
    --ack-port) SYNC_ACK_PORT="$2"; shift 2 ;;
    --nic) NIC="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --help|-h) echo 'Usage: run_publishers.sh --sync <B-IP> --nic <interface> [--port 56001 --ack-port 56002 --platform pc]'; exit 0 ;;
    *) echo "[ERROR] unknown option: $1" >&2; exit 1 ;;
  esac
done
[[ -n "${SYNC_HOST}" && -d "/sys/class/net/${NIC}" ]] || { echo '[ERROR] --sync and a valid --nic are required'; exit 1; }
python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "${SYNC_HOST}"
rp_exp_load_environment
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-77}
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}
sudo -n true
LOG_DIR="${RP_EXP_RESULTS_ROOT:-${REPO_DIR}/results}/03_observation_fidelity/pub"
mkdir -p "${LOG_DIR}"
PUB_PID=""
GATE_DIR=""
PUB_LOG=""
LOSS_OWNED=0
ORIGINAL_QDISC=$(tc -j qdisc show dev "${NIC}" | python3 -c 'import json,sys; print(next((r["kind"] for r in json.load(sys.stdin) if r.get("root")), "noqueue"))')
case "${ORIGINAL_QDISC}" in noqueue|mq|fq_codel|pfifo_fast) ;;
  *) echo '[ERROR] custom root qdisc present; use an interface with its default qdisc'; exit 1 ;;
esac

ack() { printf '%s\n' "$1" | nc -N -w5 "${SYNC_HOST}" "${SYNC_ACK_PORT}"; }
clear_loss() {
  if [[ "${LOSS_OWNED}" == 1 ]]; then
    if ! tc qdisc show dev "${NIC}" | grep -q 'qdisc prio 7f00:'; then
      echo '[ERROR] owned loss qdisc was replaced externally' >&2
      return 1
    fi
    sudo -n tc qdisc del dev "${NIC}" root handle 7f00:
    LOSS_OWNED=0
    case "${ORIGINAL_QDISC}" in
      fq_codel|pfifo_fast) sudo -n tc qdisc replace dev "${NIC}" root "${ORIGINAL_QDISC}" ;;
    esac
  fi
}
set_loss() {
  local loss=${1:?}
  case "${loss}" in 0|10|20) ;; *) return 1 ;; esac
  clear_loss || return
  if [[ "${loss}" != 0 ]]; then
    sudo -n tc qdisc replace dev "${NIC}" root handle 7f00: prio bands 3 \
      priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 || return
    LOSS_OWNED=1
    sudo -n tc qdisc add dev "${NIC}" parent 7f00:3 handle 7f30: netem loss "${loss}%" || return
    # Only outgoing UDP to B is impaired; START/READY/STOP use TCP.
    sudo -n tc filter add dev "${NIC}" protocol ip parent 7f00: prio 1 u32 \
      match ip dst "${SYNC_HOST}/32" match ip protocol 17 0xff flowid 7f00:3 || return
  fi
  tc -s qdisc show dev "${NIC}" > "${PUB_LOG}.tc.log"
  printf '[tc] installed loss=%s%% for UDP to %s\n' "${loss}" "${SYNC_HOST}"
}
stop_pub() {
  if [[ -n "${PUB_PID}" ]]; then
    kill -INT -- "-${PUB_PID}" 2>/dev/null || true
    for _ in {1..20}; do kill -0 "${PUB_PID}" 2>/dev/null || break; sleep 0.1; done
    kill -TERM -- "-${PUB_PID}" 2>/dev/null || true
    wait "${PUB_PID}" 2>/dev/null || true
    PUB_PID=""
  fi
  if [[ -n "${GATE_DIR}" ]]; then rm -rf -- "${GATE_DIR}"; GATE_DIR=""; fi
}
cleanup() { stop_pub; clear_loss; }
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "[fidelity] waiting on TCP ${SYNC_PORT}; replies to ${SYNC_HOST}:${SYNC_ACK_PORT}"
while true; do
  command=$(nc -l -p "${SYNC_PORT}")
  rp_exp_unwrap_schedule "${command}"
  command="${RP_EXP_COMMAND}"
  read -r op condition loss run <<< "${command}"
  case "${op}" in
    START)
      stop_pub
      clear_loss
      case "${condition}" in rp_bag) readers=1 ;; rosbag2) readers=2 ;; *) ack 'ERROR condition'; continue ;; esac
      [[ "${loss}" =~ ^(0|10|20)$ && "${run}" =~ ^run[0-9]+$ ]] || { ack 'ERROR run arguments'; continue; }
      PUB_LOG="${LOG_DIR}/loss${loss}_${condition}_${run}.log"
      [[ ! -e "${PUB_LOG}" ]] || { ack 'ERROR existing publisher log; select a new run label'; continue; }
      GATE_DIR=$(mktemp -d /tmp/ros2probe-fidelity-gate.XXXXXX)
      setsid env --default-signal=INT,QUIT ros2 run rp_exp_fidelity drop_image_pub \
        30 1024 1800 /drop_image 20 3 "${readers}" "${GATE_DIR}/go" > "${PUB_LOG}" 2>&1 &
      PUB_PID=$!
      ready=0
      for _ in {1..260}; do
        if grep -q '^PUBLISH_READY' "${PUB_LOG}"; then ready=1; break; fi
        kill -0 "${PUB_PID}" 2>/dev/null || break
        sleep 0.1
      done
      if [[ "${ready}" == 1 ]]; then ack READY; else ack 'ERROR DDS reader readiness'; stop_pub; fi
      ;;
    SET_LOSS)
      if set_loss "${condition}"; then ack READY; else ack 'ERROR installing netem'; exit 1; fi
      ;;
    GO)
      if [[ -n "${PUB_PID}" ]] && kill -0 "${PUB_PID}" 2>/dev/null; then
        touch "${GATE_DIR}/go"
        ack READY
      else ack 'ERROR publisher not running'; fi
      ;;
    STOP)
      complete=0
      for _ in {1..30}; do
        if [[ -n "${PUB_LOG}" ]] && grep -q '^PUBLISH_DONE expected=1800$' "${PUB_LOG}"; then complete=1; break; fi
        sleep 0.1
      done
      stop_pub
      clear_loss
      if [[ "${complete}" == 1 ]]; then ack READY; else ack 'ERROR publisher did not send all 1800 messages'; fi
      ;;
    ABORT) stop_pub; clear_loss ;; # Best-effort cleanup when the receiver is interrupted.
    DONE) ack READY; break ;;
    *) ack 'ERROR unknown command' ;;
  esac
done
