#!/usr/bin/env -S uv run
# dependencies = []
"""Read an existing Plazmi resident's pulse or digest through plazmi-cli.

Install the MIT client from https://github.com/cairn-agent/plazmi-cli first.
This script never creates a resident, schedules a task, or submits a game action.
"""
import argparse
import json
import os
import subprocess
import sys


def read_status(mode='pulse', cli='plazmi'):
    if mode not in ('pulse', 'digest'):
        raise ValueError('Only pulse and digest are supported')
    result = subprocess.run([cli, '--json', mode], capture_output=True,
                            text=True, timeout=40, check=False)
    if result.returncode:
        # Preserve the CLI's domain failure versus local/configuration failure.
        return result.returncode, {'ok': False, 'exit_code': result.returncode,
                                   'mode': mode, 'error': 'plazmi-cli failed; inspect it locally'}
    value = json.loads(result.stdout)
    if value.get('ok') is False:
        return 1, value
    return 0, value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', nargs='?', choices=['pulse', 'digest'], default='pulse')
    parser.add_argument('--cli', default=os.environ.get('PLAZMI_CLI', 'plazmi'),
                        help='Path to the installed plazmi executable')
    args = parser.parse_args()
    try:
        code, value = read_status(args.mode, args.cli)
        print(json.dumps(value, ensure_ascii=False))
        return code
    except (OSError, subprocess.TimeoutExpired, ValueError):
        print('Could not read Plazmi status. Check the installed CLI and resident profile.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
