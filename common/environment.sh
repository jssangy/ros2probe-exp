#!/usr/bin/env bash
# Source this file, then call rp_exp_load_environment before launching workloads.
RP_EXP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RP_BIN=${RP_BIN:-${RP_EXP_ROOT}/.tools/bin/rp}
source "${RP_EXP_ROOT}/common/validation.sh"

rp_exp_source_setup() {
  local setup_path=${1:?setup path required}
  local had_nounset=0
  [[ $- == *u* ]] && had_nounset=1
  if [[ ! -f "${setup_path}" ]]; then
    echo "[ERROR] Setup file not found: ${setup_path}" >&2
    return 1
  fi
  set +u
  local setup_status=0
  source "${setup_path}" || setup_status=$?
  if [[ "${had_nounset}" == 1 ]]; then
    set -u
  fi
  return "${setup_status}"
}

rp_exp_load_environment() {
  rp_exp_source_setup "${ROS_SETUP:-/opt/ros/${ROS_DISTRO:-humble}/setup.bash}" || return
  rp_exp_source_setup "${WORKSPACE_SETUP:-${RP_EXP_ROOT}/install/setup.bash}"
}
