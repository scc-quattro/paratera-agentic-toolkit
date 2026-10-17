"""Prepare a user service that survives termination of the invoking agent."""

import json
import os
import subprocess
import time

from paratera_api import ROOT, credentials


def manager_environment():
    return dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}',
                DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')


def prepare_controller():
    # Linger is an administrator/user setup decision; this program never changes
    # login policy. Refuse paid work if the watchdog could disappear on logout.
    result = subprocess.run(['loginctl', 'show-user', str(os.getuid()), '-p', 'Linger', '--value'],
                            env=manager_environment(), capture_output=True, text=True, check=True)
    if result.stdout.strip() != 'yes':
        raise ValueError('Controller needs an existing lingering systemd user manager')
    target = ROOT / '.paratera/credentials.json'
    access, secret = credentials(target)
    # systemd-run does not inherit arbitrary shell variables. Persist credentials
    # privately so the independent watchdog can authenticate after agent exit.
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(dict(access_key=access, secret_access_key=secret), stream)
    os.chmod(target, 0o600)


def start_watchdog(lease, path):
    unit = lease['watchdog_unit']
    log = ROOT / '.cache/paratera' / (unit + '.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    subprocess.run(['systemd-run', '--user', '--unit=' + unit,
                    '--property=WorkingDirectory=' + str(ROOT), '--property=Restart=on-failure',
                    '--property=RestartSec=15', '--property=StandardOutput=append:' + str(log),
                    '--property=StandardError=append:' + str(log),
                    str(ROOT / '.venv/bin/python'), str(ROOT / 'tools/paratera_lease.py'),
                    'watch', str(path)], env=manager_environment(), check=True)


def verify_watchdog(lease, path, expected_status):
    from paratera_lease import save, workspace_lock
    unit = lease['watchdog_unit']
    try:
        start_watchdog(lease, path)
        for _ in range(30):
            with workspace_lock():
                current = json.loads(path.read_text())
            if (current.get('watchdog_last_check', 0) >= lease['created_at']
                    and not current.get('watchdog_error')
                    and current.get('observed_status') == expected_status):
                break
            time.sleep(1)
        else:
            raise ValueError('No independently authenticated watchdog; no mutation sent')
        active = subprocess.run(['systemctl', '--user', 'is-active', unit + '.service'],
                                env=manager_environment(), capture_output=True, text=True)
        if active.returncode or active.stdout.strip() != 'active':
            raise ValueError('Independent watchdog is not active')
    except BaseException:
        # No API mutation is sent by this helper. Once preparation fails, close
        # this unused intent so it does not block the next authorized attempt.
        with workspace_lock():
            current = json.loads(path.read_text())
            if not current.get('create_attempted_at') and not current.get('start_attempted_at'):
                current.update(closed=True, closed_reason='preflight-failed-before-submission')
                save(path, current)
        raise


def check_submission_window(lease):
    if (lease.get('closed') or lease.get('stop_requested')
            or time.time() >= lease['deadline'] - 60):
        raise ValueError('Lease closed, stopping, or too close to expiry; no mutation sent')
