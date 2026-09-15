#!/usr/bin/env python3
"""Send START only after binding the callback port, and require a READY reply."""
import argparse
import os
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.schedule import append_event, request_time


def start_publisher(host, port, ack_port, message, timeout):
    deadline = time.monotonic() + timeout
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('', ack_port))
        listener.listen(1)
        # nc on the publisher consumes a command after the sender closes its write end.
        with socket.create_connection((host, port), timeout=min(timeout, 5)) as sender:
            sender.sendall((message + '\n').encode())
            sender.shutdown(socket.SHUT_WR)
        listener.settimeout(max(0.01, deadline - time.monotonic()))
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(max(0.01, deadline - time.monotonic()))
            reply = bytearray()
            while b'\n' not in reply and len(reply) < 1024:
                part = connection.recv(1024)
                if not part:
                    break
                reply.extend(part)
        if reply.strip() != b'READY':
            raise RuntimeError(f'publisher did not confirm READY: {reply.decode(errors="replace")!r}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--ack-port', type=int, required=True)
    parser.add_argument('--message', required=True)
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--timing-log')
    args = parser.parse_args()
    try:
        trigger = os.environ.get('RP_EXP_SCHEDULE_TRIGGER')
        if trigger and args.message.split()[0] == trigger:
            schedule = request_time(os.environ['RP_EXP_SCHEDULE_DIR'], args.message)
            append_event(args.timing_log, event='armed', command=args.message, **schedule)
            args.message = f'AT {schedule["target_ns"]} {args.message}'
        start_publisher(args.host, args.port, args.ack_port, args.message, args.timeout)
        if args.message.startswith('AT '):
            append_event(args.timing_log, event='publisher_ready', reply_ns=time.time_ns())
    except (OSError, RuntimeError) as error:
        print(f'[ERROR] publisher synchronization failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
