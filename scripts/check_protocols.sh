#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Usage: scripts/check_protocols.sh [--output-dir DIR]'
  echo 'Checks command topic types, measurement deadlines, reader/gate ordering, bag topics, and process accounting (~20 seconds).'
  echo 'Source ROS / build first, or set ROS_SETUP and WORKSPACE_SETUP.'
  echo 'No sudo, CPU tuning, packet loss, or external hosts. Not a paper benchmark.'
  exit 0
fi
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/common/environment.sh"
rp_exp_load_environment
exec python3 "${RP_EXP_ROOT}/scripts/check_protocols.py" "$@"
