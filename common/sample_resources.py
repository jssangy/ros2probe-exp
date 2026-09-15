#!/usr/bin/env python3
"""Sample only observer sessions; CPU=one-core percent, memory=PSS KiB.

Run as root for privileged rp smaps. Shell/sudo launcher overhead is included
consistently with the observer CLI and runtime. A failed PSS read is an error.
"""
import argparse
import os
from pathlib import Path
import time


def snapshot(sessions):
    processes = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            # stat starts at field 3; session=6, utime=14, stime=15, starttime=22.
            if stat[0] == 'Z':
                continue
            processes[int(entry.name)] = (entry, stat)
        except (FileNotFoundError, ProcessLookupError):
            continue
    selected = {pid for pid, (_, stat) in processes.items() if pid in sessions or int(stat[3]) in sessions}
    # sudo's use_pty mode may create another session. Include descendants even
    # when their session ID differs from the launcher session.
    while True:
        children = {pid for pid, (_, stat) in processes.items() if int(stat[1]) in selected}
        if children <= selected:
            break
        selected |= children
    result = {}
    for pid in selected:
        entry, stat = processes[pid]
        try:
            pss = next(int(line.split()[1]) for line in
                       (entry / 'smaps_rollup').read_text().splitlines() if line.startswith('Pss:'))
            result[(pid, int(stat[19]))] = (int(stat[11]) + int(stat[12]), pss)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', nargs='*', type=int, default=[])
    args = parser.parse_args()
    sessions = set(args.sessions)
    tick_hz = os.sysconf('SC_CLK_TCK')
    previous = snapshot(sessions)
    previous_time = time.monotonic()
    print('# timestamp_ms cpu_pct pss_kb alive_pid_count pid_list', flush=True)
    while True:
        time.sleep(1)
        current = snapshot(sessions)
        now = time.monotonic()
        if sessions and not current:
            raise RuntimeError('all observer processes exited during resource sampling')
        delta = sum(max(0, ticks - previous.get(key, (ticks, 0))[0])
                    for key, (ticks, _) in current.items())
        cpu = delta / tick_hz / (now - previous_time) * 100
        pss = sum(memory for _, memory in current.values())
        print(f'{time.time_ns() // 1000000} {cpu:.6f} {pss} {len(current)} '
              + ','.join(str(key[0]) for key in sorted(current)), flush=True)
        previous, previous_time = current, now


if __name__ == '__main__':
    main()
