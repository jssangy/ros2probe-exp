#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Usage: scripts/check_environment.sh [1|2|3|4|all] [--role publisher|receiver] [--nic IFACE] [--skip-sudo]'
  echo 'Read-only checks after building. Default: all experiments, receiver role.'
  echo 'Set ROS_SETUP / WORKSPACE_SETUP for a nondefault installation.'
  exit 0
fi
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/common/environment.sh"
rp_exp_load_environment
exec python3 "${RP_EXP_ROOT}/scripts/check_environment.py" "$@"
