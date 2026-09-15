#!/usr/bin/env python3
"""Read a recording through EOF and require messages on every target topic."""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('topics', nargs='+')
    args = parser.parse_args()
    try:
        if not args.path.exists():
            raise FileNotFoundError(args.path)
        import rosbag2_py
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(args.path), storage_id='mcap'),
                    rosbag2_py.ConverterOptions('', ''))
        missing = set(args.topics)
        messages = 0
        while reader.has_next():
            topic, data, _ = reader.read_next()
            messages += 1
            if not data:
                raise ValueError(f'empty serialized message on {topic}')
            if data:
                missing.discard(topic)
        if missing:
            raise ValueError(f'no messages recorded for: {sorted(missing)}')
        print(f'[bag] read {messages} messages through EOF; all {len(args.topics)} required topics present: {args.path}')
        return 0
    except Exception as error:
        print(f'[ERROR] invalid recording {args.path}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
