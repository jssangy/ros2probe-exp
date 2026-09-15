#!/usr/bin/python3
"""OpenSSH-only password helper. No password is passed through argv or environment."""
import os
import sys
from testbed import credentials


def main():
    if len(sys.argv) != 2 or 'password' not in sys.argv[1].lower():
        return 1
    value = credentials(os.environ['RP_EXP_CREDENTIALS_FILE'])['slaves'][os.environ['RP_EXP_CREDENTIALS_ROLE']]['ssh']
    if not value:
        return 1
    print(value, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
