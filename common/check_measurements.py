#!/usr/bin/env python3
"""Reject incomplete measurement evidence before a run is marked complete."""
from pathlib import Path
import json
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.metrics import bandwidth, interval, resource_usage, rate_observer, timed_subscribers, fidelity_subscriber


def check(folder):
    folder = Path(folder)
    profile = folder / 'execution_profile.txt'
    target_ns = None
    if profile.exists() and profile.read_text().strip() == 'master-scheduled-v1':
        events = [json.loads(line) for line in (folder / 'timing.jsonl').read_text().splitlines()]
        armed = [event for event in events if event.get('event') == 'armed']
        ready = [event for event in events if event.get('event') == 'publisher_ready']
        if len(armed) != 1 or len(ready) != 1 or type(armed[0].get('target_ns')) is not int:
            raise ValueError(f'Missing/duplicate master scheduling evidence: {folder}')
        target_ns = armed[0]['target_ns']
    sub = folder / ('sub_points_front.log' if (folder / 'sub_points_front.log').exists() else 'sub.log')
    if (folder / 'netdev.log').exists():
        metadata = (folder / 'platform.log').read_text()
        if 'scenario=S7\n' in metadata:
            if not (folder / 'sub_points_front.log').is_file():
                raise ValueError(f'Missing primary S7 subscriber: {folder}')
            other_topics = timed_subscribers(folder / 'sub.log', 8)
            expected_topics = {'/cmd_vel', '/imu', '/points/rear', '/depth/image_raw',
                               '/camera/front/compressed', '/camera/left/compressed',
                               '/camera/right/compressed', '/camera/rear/compressed'}
            if other_topics != expected_topics or timed_subscribers(sub) != {'/points/front'}:
                raise ValueError(f'Incorrect S7 subscriber topics: {folder}')
        else:
            timed_subscribers(sub)
        start, end = interval(sub)
        if target_ns is not None:
            warmup = re.search(r'warmup_sec=(\d+)', sub.read_text())
            first_arrival_ms = start - int(warmup[1]) * 1000
            if (first_arrival_ms + 1) * 1_000_000 < target_ns:
                raise ValueError(f'Subscriber received data before the scheduled start: {folder}')
        bandwidth(folder / 'netdev.log', start, end)
        if re.search(r'^condition=(?:topic_hz|rp_hz)$', metadata, re.M):
            rate_observer(folder / 'obs.log')
        if (folder / 'observer_cpu.log').exists():
            baseline = 'condition=baseline\n' in (folder / 'platform.log').read_text()
            resource_usage(folder / 'observer_cpu.log', start, end, baseline)
    elif (folder / 'control.log').exists():
        _seqs, sequence_expected = fidelity_subscriber(folder / 'sub.log')
        metadata = (folder / 'platform.log').read_text()
        loss = re.search(r'^loss_pct=(\d+)$', metadata, re.M)
        expected = re.search(r'^expected=(\d+)$', metadata, re.M)
        control = (folder / 'control.log').read_text()
        if not loss or not expected or f'loss_acknowledged={loss[1]}\n' not in control or f'publisher_completed={expected[1]}\n' not in control:
            raise ValueError(f'Missing loss/publisher evidence: {folder}')
        if sequence_expected != int(expected[1]):
            raise ValueError(f'Inconsistent configured publisher/subscriber count: {folder}')
    else:
        raise ValueError(f'Missing measurement evidence: {folder}')


if __name__ == '__main__':
    check(sys.argv[1])
