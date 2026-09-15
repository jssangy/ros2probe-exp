#!/usr/bin/env python3
"""Exercise installed workloads on localhost; retain evidence outside paper results."""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from ament_index_python.packages import get_package_prefix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    out = args.output_dir or Path(tempfile.mkdtemp(prefix='ros2probe-smoke-'))
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        parser.error('output directory must be empty')
    env = dict(os.environ, ROS_DOMAIN_ID='191', ROS_LOCALHOST_ONLY='1',
               RMW_IMPLEMENTATION='rmw_fastrtps_cpp', ROS_LOG_DIR=str(out / 'ros-log'))
    active = []
    checks = []

    def start(name, command, extra=None):
        with (out / (name + '.log')).open('w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       env=env | (extra or {}), start_new_session=True)
        active.append(process)
        return process

    def node(package, executable, *arguments):
        return [str(Path(get_package_prefix(package)) / 'lib' / package / executable),
                *map(str, arguments)]

    def finish(process, timeout=15):
        code = process.wait(timeout=timeout)
        if code:
            raise RuntimeError(f'child exited {code}: {process.args}')

    def stop(process):
        # Signal the complete group even if its launch/CLI parent already exited.
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def received(name):
        text = (out / (name + '.log')).read_text()
        match = re.search(r'FINAL \[\d+s\]: recv (\d+) / expected (\d+)', text)
        if not match or int(match[1]) <= 0:
            raise RuntimeError(f'missing nonempty subscriber result: {name}.log')
        return {'received': int(match[1]), 'expected': int(match[2])}

    def passed(name, **values):
        checks.append({'check': name, 'status': 'pass', **values})
        print(f'[PASS] {name}: {values}', flush=True)

    print(f'Evidence: {out}', flush=True)
    try:
        # Use the isolated smoke-test domain for observers and workloads alike.
        discovery = out / 'discovery'
        discovery.mkdir()
        target = int(time.time()) + 2
        nodes = [start(f'discovery-{i}', node('rclcpp_discovery_traffic', 'discovery_test',
                       2, target, 3, 0, i, 2),
                       {'DISCOVERY_TOPIC_REPORT_DIR': str(discovery)}) for i in (0, 1)]
        for process in nodes:
            finish(process)
        for path in discovery.glob('*_topic_endpoint_count.csv'):
            with path.open() as stream:
                rows = list(csv.DictReader(stream))
            if not any(int(row['total_pub']) >= 2 and int(row['total_sub']) >= 2 for row in rows):
                raise RuntimeError(f'discovery endpoints did not match: {path}')
        if len(list(discovery.glob('*_topic_endpoint_count.csv'))) != 2:
            raise RuntimeError('missing discovery reports')
        passed('discovery: two processes / two endpoints per process')

        sub = start('stress-sub', node('rp_exp_overhead', 'stress_sub', 100, 2, 5, '/stress'))
        pub = start('stress-pub', node('rp_exp_overhead', 'stress_pub', 100, 65536, 0, '/stress'))
        finish(sub)
        stop(pub)
        passed('resource workload: ST100, shortened 2s + 5s', **received('stress-sub'))

        bag = out / 'fidelity-bag'
        recorder = start('rosbag2', ['ros2', 'bag', 'record', '/drop_image',
                                     '--storage', 'mcap', '-o', str(bag)])
        time.sleep(2)
        if recorder.poll() is not None:
            raise RuntimeError('rosbag2 failed to start')
        sub = start('fidelity-sub', node('rp_exp_fidelity', 'drop_image_sub',
                                        30, 90, 1024, '/drop_image', 8))
        pub = start('fidelity-pub', node('rp_exp_fidelity', 'drop_image_pub',
                                        30, 1024, 90, '/drop_image', 5, 3, 2))
        finish(pub)
        finish(sub)
        stop(recorder)
        passed('fidelity workload: 90 sequence messages, 3s reader stabilization', **received('fidelity-sub'))
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from sensor_msgs.msg import Image
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id='mcap'),
                    rosbag2_py.ConverterOptions('', ''))
        sequences = set()
        while reader.has_next():
            topic, data, _ = reader.read_next()
            if topic == '/drop_image':
                msg = deserialize_message(data, Image)
                sequences.add(int.from_bytes(bytes(msg.data[:8]), 'little'))
        if sequences != set(range(1, 91)):
            raise RuntimeError(f'MCAP round trip expected sequences 1..90, got {len(sequences)}')
        passed('rosbag2 MCAP write/read/deserialize', sequences=len(sequences))
        del reader

        for qos, suffix in [('best_effort', ''), ('reliable', '_rel')]:
            name = 's1-' + qos
            sub = start(name, node('rp_exp_probe_effect', 's2_sub' + suffix))
            pub = start(name + '-pub', node('rp_exp_probe_effect', 's2_pub' + suffix))
            finish(sub, 80)
            stop(pub)
            passed('probe S1 /imu: 60s from first arrival, ' + qos, **received(name))
    except Exception as error:
        checks.append({'status': 'fail', 'error': str(error)})
        raise
    finally:
        for process in reversed(active):
            stop(process)
        (out / 'report.json').write_text(json.dumps({
            'scope': 'localhost functionality only; no rp eBPF or two-host measurements',
            'ros_distro': os.environ.get('ROS_DISTRO'), 'checks': checks,
        }, indent=2) + '\n')


if __name__ == '__main__':
    main()
