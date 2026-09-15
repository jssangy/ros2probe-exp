#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USAGE'
Usage: scripts/build.sh [all|1|2|3|4]
  Installs bundled rp with Cargo, then builds the selected ROS workloads.
  1 discovery, 2 resource overhead, 3 observation fidelity, 4 probe effect.
Environment: ROS_SETUP or ROS_DISTRO (default humble), BUILD_BASE,
INSTALL_BASE, COLCON_LOG_BASE, CMAKE_BUILD_PARALLEL_LEVEL.
USAGE
  exit 0
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROS_SETUP=${ROS_SETUP:-/opt/ros/${ROS_DISTRO:-humble}/setup.bash}
if [[ ! -f "${ROS_SETUP}" ]]; then
  echo "[ERROR] ROS setup not found: ${ROS_SETUP}. Set ROS_SETUP explicitly." >&2
  exit 1
fi
case "${1:-all}" in
  1) packages=(rclcpp_discovery_traffic) ;;
  2) packages=(rp_exp_overhead) ;;
  3) packages=(rp_exp_fidelity) ;;
  4) packages=(rp_exp_probe_effect) ;;
  all) packages=(rclcpp_discovery_traffic rp_exp_overhead rp_exp_fidelity rp_exp_probe_effect) ;;
  *) echo "[ERROR] Expected all or an experiment number from 1 to 4." >&2; exit 1 ;;
esac
if (( $# > 1 )); then
  echo "[ERROR] Unexpected extra arguments; see --help." >&2
  exit 1
fi
python3 "${REPO_DIR}/scripts/install_rp.py"
set +u
source "${ROS_SETUP}"
set -u
export CMAKE_BUILD_PARALLEL_LEVEL=${CMAKE_BUILD_PARALLEL_LEVEL:-2}
exec colcon --log-base "${COLCON_LOG_BASE:-${REPO_DIR}/log}" build \
  --base-paths "${REPO_DIR}/experiments" \
  --build-base "${BUILD_BASE:-${REPO_DIR}/build}" \
  --install-base "${INSTALL_BASE:-${REPO_DIR}/install}" \
  --parallel-workers 2 --packages-select "${packages[@]}" \
  --cmake-args -DBUILD_TESTING=OFF
