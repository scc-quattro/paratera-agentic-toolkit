"""Run SSH/file transfers only on the running container registered in a lease.

Connection passwords remain in ignored 0600 local files and are supplied to
OpenSSH through SSH_ASKPASS, never through command-line arguments or logs.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from urllib.parse import urlparse

from paratera_lease import (ROOT, LEASES, api, matching_service, save, validate,
                           workspace_lock)


def heartbeat(path, phase):
    with workspace_lock():
        lease = json.loads(path.read_text())
        validate(lease)
        if lease.get('closed') or lease.get('stop_requested') or lease.get('stop_job'):
            raise ValueError('Lease is closed or stopping')
        if time.time() >= lease['deadline'] - 60:
            raise ValueError('Lease is about to expire')
        lease.update(heartbeat=time.time(), phase=phase)
        save(path, lease)
        return lease


def ssh_target(url):
    """Parse the SDK's connection instruction as data, never as shell code."""
    if url.startswith('ssh://'):
        parsed = urlparse(url)
        user, host, port = parsed.username, parsed.hostname, parsed.port or 22
    else:
        words = shlex.split(url)
        if len(words) == 1 and re.fullmatch(r'[A-Za-z0-9_-]+@[A-Za-z0-9.-]+(?::[0-9]+)?', words[0]):
            words = ['ssh', words[0]]
        if not words or words[0] != 'ssh':
            raise ValueError('Unrecognized SSH instruction')
        destination = next((word for word in words[1:] if '@' in word), None)
        if not destination:
            raise ValueError('SSH destination missing')
        user, host = destination.rsplit('@', 1)
        port = int(words[words.index('-p') + 1]) if '-p' in words else 22
        if ':' in host:
            host, raw_port = host.rsplit(':', 1)
            if not raw_port.isdigit():
                raise ValueError('Invalid SSH port')
            port = int(raw_port)
    # The platform gateway uses user@serviceUuid as the SSH login name.
    if not user or not re.fullmatch(r'[A-Za-z0-9_.@-]+', user):
        raise ValueError('Invalid SSH user')
    if not host or not re.fullmatch(r'[A-Za-z0-9.-]+', host) or not 0 < port < 65536:
        raise ValueError('Invalid SSH host or port')
    return user, host, port


def connection(lease):
    row = matching_service(lease)
    if row is None or row.get('serviceStatus') != 'Running':
        raise ValueError('Owned container is not Running')
    data = api('ackcs', 'DescribeServicesSSH', {
        'zoneCode': lease['zoneCode'], 'serviceUuids': [lease['serviceUuid']]})
    entries = data.get('sshes', [])
    matches = [entry for entry in entries if entry.get('serviceUuid') == lease['serviceUuid']]
    if len(matches) != 1 or not matches[0].get('password') or not matches[0].get('url'):
        raise ValueError('SSH connection is not available yet')
    user, host, port = ssh_target(matches[0]['url'])
    private = ROOT / '.paratera/connections'
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = private / (lease['serviceUuid'] + '.json')
    value = dict(user=user, host=host, port=port, password=matches[0]['password'])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    os.chmod(path, 0o600)
    return path, value


def run(args):
    path = args.lease.resolve()
    if not path.is_relative_to(LEASES.resolve()):
        raise ValueError('Lease path must be in the workspace ledger')
    lease = heartbeat(path, args.phase)
    with workspace_lock():
        credential_path, target = connection(lease)
    environment = dict(os.environ, SSH_ASKPASS=str(ROOT / 'tools/paratera_askpass.py'),
                       SSH_ASKPASS_REQUIRE='force', DISPLAY=':0',
                       PARATERA_CONNECTION_FILE=str(credential_path))
    command = ['ssh', '-F', '/dev/null', '-q', '-p', str(target['port']),
               '-o', 'StrictHostKeyChecking=accept-new',
               '-o', 'UserKnownHostsFile=' + str(ROOT / '.paratera/known_hosts'),
               '-o', 'PreferredAuthentications=password,keyboard-interactive',
               '-o', 'PubkeyAuthentication=no', '-o', 'NumberOfPasswordPrompts=1',
               '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15',
               '-o', 'ServerAliveCountMax=3', '-l', target['user'], target['host']]
    source = None
    archive = None
    if args.action == 'upload':
        bundle = args.bundle.resolve()
        if not bundle.is_relative_to((ROOT / 'transfer').resolve()):
            raise ValueError('Only the prepared container bundle may be uploaded')
        remote = args.destination
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', remote) or '..' in Path(remote).parts:
            raise ValueError('Invalid upload destination')
        command += ['mkdir -p ' + shlex.quote(remote) + ' && tar -xf - -C ' + shlex.quote(remote)]
        source = subprocess.Popen(['tar', '-cf', '-', '-C', str(bundle), '.'], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        input_stream = source.stdout
    elif args.action == 'download':
        remote = args.remote_directory
        if (not remote or not re.fullmatch(r'/[A-Za-z0-9_./-]+', remote)
                or '..' in Path(remote).parts):
            raise ValueError('Only absolute normalized task output paths may be downloaded')
        archive = args.archive.resolve()
        if not archive.is_relative_to((ROOT/'output/paratera/results').resolve()):
            raise ValueError('Download archive must be under output/paratera/results')
        if archive.exists():
            raise ValueError('Preserve existing evidence; use a new archive path')
        archive.parent.mkdir(parents=True, exist_ok=True)
        command += ['tar -czf - -C ' + shlex.quote(remote) + ' .']
        input_stream = open(os.devnull, 'rb')
    else:
        command += ['bash -se']
        input_stream = args.script.open('rb')
    log = args.log.resolve()
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        with log.open('wb') as output:
            binary = archive.with_suffix(archive.suffix+'.part').open('wb') if archive else None
            try:
                # Keep stderr separate from a binary download stream.
                process = subprocess.Popen(command, stdin=input_stream,
                                           stdout=binary if binary else output,
                                           stderr=output if binary else subprocess.STDOUT,
                                           env=environment, start_new_session=True)
            finally:
                if binary:
                    binary.close()
        input_stream.close()
        try:
            while process.poll() is None:
                heartbeat(path, args.phase)
                print(json.dumps({'phase': args.phase, 'elapsed_seconds': round(time.time()-started),
                                  'log': str(log.relative_to(ROOT))}), flush=True)
                time.sleep(20)
        except BaseException:
            process.terminate()
            process.wait(timeout=30)
            raise
        if source:
            source.wait(timeout=30)
        status = process.returncode or (source.returncode if source else 0)
        if archive and status == 0:
            os.replace(archive.with_suffix(archive.suffix+'.part'), archive)
        print(json.dumps({'phase': args.phase, 'exit_code': status,
                          'elapsed_seconds': round(time.time()-started)}), flush=True)
        return status
    finally:
        if source and source.poll() is None:
            source.terminate()
            source.wait(timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'upload', 'download'])
    parser.add_argument('--lease', type=Path, required=True)
    parser.add_argument('--phase', required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--script', type=Path)
    parser.add_argument('--bundle', type=Path, default=ROOT / 'transfer/bundle')
    parser.add_argument('--destination', default='/tmp/agent-cloud-bundle')
    parser.add_argument('--remote-directory')
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    if args.action == 'run' and not args.script:
        parser.error('--script is required for run')
    if args.action == 'download' and (not args.archive or not args.remote_directory):
        parser.error('--archive and --remote-directory are required for download')
    try:
        raise SystemExit(run(args))
    except Exception as error:
        # Neither SSH target nor credential material is included in errors.
        parser.exit(1, f'Remote operation failed ({type(error).__name__}); details withheld.\n')


if __name__ == '__main__':
    main()
