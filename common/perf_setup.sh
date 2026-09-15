#!/bin/bash

set_cpu_governor_performance() {
  local used_cpupower=0
  local found_sysfs=0
  local g

  sudo -n modprobe cpufreq_performance >/dev/null 2>&1 || true

  if command -v cpupower >/dev/null 2>&1; then
    if sudo -n cpupower frequency-set -g performance >/dev/null 2>&1; then
      used_cpupower=1
    else
      echo "  [warn] cpupower performance setup failed; trying sysfs"
    fi
  fi

  for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [[ -e "${g}" ]] || continue
    found_sysfs=1
    if ! echo performance | sudo -n tee "${g}" >/dev/null 2>&1; then
      echo "  [warn] failed to set ${g} to performance"
    fi
  done

  if [[ "${used_cpupower}" == "0" && "${found_sysfs}" == "0" ]]; then
    echo "  [warn] no CPU frequency governor interface found"
  fi
}

pin_cpu_min_to_max_freq() {
  local cpu_dir
  local max_freq
  local pinned=0

  for cpu_dir in /sys/devices/system/cpu/cpu[0-9]*; do
    [[ -d "${cpu_dir}/cpufreq" ]] || continue
    [[ -r "${cpu_dir}/cpufreq/scaling_max_freq" ]] || continue
    [[ -w "${cpu_dir}/cpufreq/scaling_min_freq" || -e "${cpu_dir}/cpufreq/scaling_min_freq" ]] || continue
    max_freq=$(cat "${cpu_dir}/cpufreq/scaling_max_freq")
    if echo "${max_freq}" | sudo -n tee "${cpu_dir}/cpufreq/scaling_min_freq" >/dev/null 2>&1; then
      pinned=1
    else
      echo "  [warn] failed to pin $(basename "${cpu_dir}") min frequency to max"
    fi
  done

  if [[ "${pinned}" == "1" ]]; then
    echo "  [setup] CPU scaling_min_freq pinned to scaling_max_freq"
  fi
}

setup_jetson_performance() {
  local mode=${JETSON_NVP_MODE:-0}

  if command -v nvpmodel >/dev/null 2>&1; then
    sudo -n nvpmodel -m "${mode}" >/dev/null 2>&1 \
      || echo "  [warn] nvpmodel -m ${mode} failed"
  else
    echo "  [warn] nvpmodel not found"
  fi

  if command -v jetson_clocks >/dev/null 2>&1; then
    sudo -n jetson_clocks >/dev/null 2>&1 \
      || echo "  [warn] jetson_clocks failed"
  else
    echo "  [warn] jetson_clocks not found"
  fi

  set_cpu_governor_performance
}

print_cpu_governor_summary() {
  local cpu0=/sys/devices/system/cpu/cpu0/cpufreq

  if [[ -r "${cpu0}/scaling_governor" ]]; then
    echo "[setup] cpu0_governor=$(cat "${cpu0}/scaling_governor")"
  fi
  if [[ -r "${cpu0}/scaling_cur_freq" && -r "${cpu0}/scaling_min_freq" && -r "${cpu0}/scaling_max_freq" ]]; then
    echo "[setup] cpu0_freq_khz=$(cat "${cpu0}/scaling_cur_freq") min=$(cat "${cpu0}/scaling_min_freq") max=$(cat "${cpu0}/scaling_max_freq")"
  fi
}

setup_platform_performance() {
  local platform=${1:-generic}
  local platform_lc=${platform,,}

  echo "[setup] Performance mode requested for platform=${platform}"
  case "${platform_lc}" in
    *jetson*)
      setup_jetson_performance
      ;;
    *)
      set_cpu_governor_performance
      ;;
  esac
  pin_cpu_min_to_max_freq
  print_cpu_governor_summary
}
