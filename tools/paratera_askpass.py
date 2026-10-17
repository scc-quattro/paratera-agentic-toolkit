#!/usr/bin/env python3
"""Private password channel for OpenSSH; stdout goes only to SSH_ASKPASS."""

import json
import os
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[1] / '.paratera/connections'
    path = Path(os.environ['PARATERA_CONNECTION_FILE']).resolve()
    if not path.is_relative_to(root.resolve()) or path.stat().st_mode & 0o077:
        return 1
    sys.stdout.write(json.loads(path.read_text())['password'] + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
