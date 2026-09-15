#!/usr/bin/env bash
# Run guards: no ROS discovery, performance tuning, or measurement timing changes.

rp_exp_start_publisher() {
  local timing=() folder=${OUTDIR:-${outdir:-}}
  [[ -n "${folder}" ]] && timing=(--timing-log "${folder}/timing.jsonl")
  python3 "${RP_EXP_ROOT}/common/sync.py" --host "${SYNC_HOST}" \
    --port "${SYNC_PORT}" --ack-port "${SYNC_ACK_PORT}" --message "$1" \
    --timeout "${SYNC_TIMEOUT_SEC:-30}" "${timing[@]}"
}

rp_exp_unwrap_schedule() {
  RP_EXP_COMMAND="$1"
  if [[ "$1" == AT\ * ]]; then
    local prefix target command
    read -r prefix target command <<< "$1"
    [[ "${target}" =~ ^[0-9]+$ && -n "${command}" ]] || return 1
    python3 "${RP_EXP_ROOT}/common/schedule.py" "${target}" --command "${command}" \
      --log "${RP_EXP_RESULTS_ROOT}/_master/publisher-timing.jsonl" || return
    RP_EXP_COMMAND="${command}"
  fi
}

rp_exp_require_live() {
  local pid=${1:-}
  local label=${2:-process}
  if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
    echo "[ERROR] ${label} is not running; inspect this run's logs" >&2
    return 1
  fi
}

rp_exp_wait_log() {
  local pid=${1:?} log=${2:?} pattern=${3:?} label=${4:?}
  local count=${5:-1} timeout_s=${6:-15} deadline matches
  deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    rp_exp_require_live "${pid}" "${label}" || return
    matches=$(grep -Ec "${pattern}" "${log}" 2>/dev/null || true)
    if (( ${matches:-0} >= count )); then
      echo "[ready] ${label} $(date +%s%3N)"
      return 0
    fi
    sleep 0.1
  done
  echo "[ERROR] ${label} readiness timed out after ${timeout_s}s; inspect ${log}" >&2
  return 1
}

rp_exp_wait_subscriber() {
  local pid=${1:?} timeout_s=${2:?} deadline
  deadline=$((SECONDS + timeout_s))
  while kill -0 "${pid}" 2>/dev/null; do
    if (( SECONDS >= deadline )); then
      echo "[ERROR] subscriber timed out after ${timeout_s}s" >&2
      return 1
    fi
    sleep 1
  done
  wait "${pid}"
}

rp_exp_validate_subscriber() {
  local log=${1:?} expected_finals=${2:-1} count
  count=$(grep -Ec 'FINAL \[[0-9]+s\]: recv [1-9][0-9]* / expected [1-9][0-9]*' "${log}" || true)
  # A stream that started and then stalled may legitimately lose all measured
  # messages. A process that never received anything has no interval markers.
  if grep -q 'MEASURE_START_MS' "${log}"; then
    count=$(grep -Ec 'FINAL \[[0-9]+s\]: recv [0-9]+ / expected [1-9][0-9]*' "${log}" || true)
  fi
  if [[ "${count}" != "${expected_finals}" ]]; then
    echo "[ERROR] expected ${expected_finals} completed FINAL result(s) in ${log}; found ${count}" >&2
    return 1
  fi
}

rp_exp_prepare_runtime() {
  if [[ "${RP_SOCKET:-/tmp/ros2probe.sock}" != /tmp/ros2probe.sock ]]; then
    echo '[ERROR] installed rp CLI uses /tmp/ros2probe.sock; custom RP_SOCKET is unsupported' >&2
    return 1
  fi
  if [[ -z "${RP_BIN:-}" || ! -x "${RP_BIN}" ]]; then
    echo '[ERROR] rp executable not found; install rp or set RP_BIN' >&2
    return 1
  fi
  # Never unlink a live runtime's command socket. rp itself removes stale sockets.
  if timeout 2 "${RP_BIN}" topic list >/dev/null 2>&1; then
    echo '[ERROR] an rp runtime is already running; stop it before an isolated experiment' >&2
    return 1
  fi
  sudo -n true || return
}

rp_exp_validate_bag() {
  python3 "${RP_EXP_ROOT}/common/validate_bag.py" "$@"
}

rp_exp_begin_run() {
  if [[ -n "$(find "$1" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    echo "[ERROR] run directory already contains data; choose a new run label: $1" >&2
    return 1
  fi
  printf 'incomplete\n' > "$1/run_status.txt"
  printf 'paper-v2\n' > "$1/protocol_version.txt"
  printf '%s\n' "${RP_EXP_EXECUTION_PROFILE:-direct}" > "$1/execution_profile.txt"
}

rp_exp_complete_run() {
  python3 "${RP_EXP_ROOT}/common/check_measurements.py" "$1" || return
  printf 'complete\n' > "$1/run_status.txt"
}

rp_exp_wait_runtime() {
  local pid=${1:?} decisec=${2:-100} deadline
  deadline=$((SECONDS + (decisec + 9) / 10))
  while (( SECONDS < deadline )); do
    rp_exp_require_live "${pid}" 'rp runtime' || return
    if [[ -S "${RP_SOCKET}" ]] && timeout 1 "${RP_BIN}" topic list >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  echo "[ERROR] ros2probe command server not ready: ${RP_SOCKET}" >&2
  return 1
}
