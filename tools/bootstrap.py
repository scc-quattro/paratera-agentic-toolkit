"""Create the repository venv and install the unmodified, verified vendor SDK."""

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import venv

from paratera_api import ROOT, SDK_SHA256

SDK_URL = 'https://ai.blsc.cn/mock_data/tool/aicloud_sdk_python-3.0-py3-none-any.whl'


def verify_wheel(path):
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != SDK_SHA256:
            raise ValueError('Official SDK checksum mismatch; installation refused')


def fetch_wheel(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        verify_wheel(path)
        return
    temporary = path.with_suffix('.download')
    try:
        with urllib.request.urlopen(SDK_URL, timeout=30) as response, temporary.open('wb') as stream:
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > 32 * 1024 * 1024:
                    raise ValueError('SDK download exceeded the size limit')
                stream.write(block)
        verify_wheel(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('Python 3.11 or newer is required')
    try:
        wheel = ROOT / '.cache/paratera/aicloud_sdk_python-3.0-py3-none-any.whl'
        fetch_wheel(wheel)
        environment = ROOT / '.venv'
        if not (environment / 'bin/python').exists():
            venv.EnvBuilder(with_pip=True).create(environment)
        python = str(environment / 'bin/python')
        subprocess.run([python, '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.lock')], check=True)
        subprocess.run([python, '-m', 'pip', 'install', '--no-deps', str(wheel)], check=True)
        subprocess.run([python, '-m', 'pip', 'check'], check=True)
        subprocess.run([python, str(ROOT / 'tools/paratera_api.py'), 'region',
                        'DescribeZones', '--check-sdk'], check=True)
    except Exception as error:
        parser.exit(1, f'Bootstrap failed ({type(error).__name__}); no cloud mutation was sent.\n')


if __name__ == '__main__':
    main()
