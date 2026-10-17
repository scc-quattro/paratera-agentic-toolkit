"""Independent container-cloud lease watchdog; never deletes instances or disks.

Run with systemd --user on the controller, outside the bridge process group.
Only the instance identified by this workspace's lease UUID/zone/alias is stopped.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from paratera_api import ROOT, credentials, sdk_client


LOCK = ROOT / '.cache/paratera/resources.lock'
LEASES = ROOT / 'output/paratera/leases'
WORKSPACE = hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()


class CloudAPIError(RuntimeError):
    """A reviewed response code/message, with credentials removed."""

    def __init__(self, action, code, message, access, secret):
        super().__init__(f'{action} failed (code {code})')
        self.action, self.code = action, code
        cleaned = str(message).replace(access, '<redacted>').replace(secret, '<redacted>')
        self.safe_message = re.sub(r'[A-Za-z0-9_+/=-]{32,}', '<redacted>', cleaned)[:300]


@contextmanager
def workspace_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(temporary, path)


def validate(lease):
    if lease.get('workspace') != WORKSPACE:
        raise ValueError('Lease belongs to a different workspace')
    if lease.get('resource_type') != 'ACKCS' or lease.get('billingType') != 'PostPaid':
        raise ValueError('Only postpaid container leases are supported')
    if not re.fullmatch(r'agent-cloud-[a-z0-9-]{1,48}', lease.get('aliasName', '')) or not lease.get('zoneCode'):
        raise ValueError('Lease zone/alias missing or invalid')
    if lease.get('serviceUuid') and not lease['serviceUuid'].startswith('ackcs-'):
        raise ValueError('Unexpected container UUID')
    if not 0 < lease['deadline'] - lease['created_at'] <= 7200:
        raise ValueError('Lease duration must be at most 120 minutes')
    if not lease.get('preflight_alias_absent'):
        raise ValueError('Creation intent lacks the alias preflight')


def api(service, action, payload):
    allowed = {
        'ackcs': {'DescribeServices', 'StopServices', 'CreateServices', 'StartServices',
                  'DescribeServicesSSH'},
        'ack_job': {'DescribeJobs'},
    }
    if action not in allowed.get(service, set()):
        raise ValueError('Unsupported cloud action')
    client_class = sdk_client(service)
    from common.ClientProfileMain import ClientProfile
    from common.HttpProfileMain import HttpProfile
    access, secret = credentials(ROOT / '.paratera/credentials.json')
    client = client_class(HttpProfile(verifyCert=True, connectTimeout=10, readTimeout=30),
                          ClientProfile(access, secret))
    response = client.GenericRequest(payload, action, lambda data: data)
    if response.get('code') != 200:
        raise CloudAPIError(action, response.get('code'), response.get('message', ''), access, secret)
    return response['data']


def matching_service(lease, query=api):
    request = {key: lease[key] for key in ('zoneCode', 'aliasName')}
    if lease.get('serviceUuid'):
        request['serviceUuid'] = lease['serviceUuid']
    request.update(pageNum=1, pageSize=200)
    data = query('ackcs', 'DescribeServices', request)
    rows = data.get('rows', []) if isinstance(data, dict) else data
    matches = [row for row in rows
               if row.get('aliasName') == lease['aliasName']
               and row.get('zone', {}).get('zoneCode') == lease['zoneCode']
               and (not lease.get('serviceUuid') or row.get('serviceUuid') == lease['serviceUuid'])]
    if len(matches) > 1:
        raise ValueError('Ambiguous owned instance; refusing to stop')
    if not matches:
        return None
    row = matches[0]
    if row.get('billingType') != 'PostPaid':
        raise ValueError('Instance billing identity differs from lease')
    return row


def reason_to_stop(lease, now):
    if lease.get('stop_requested'):
        return lease['stop_requested']
    if now >= lease['deadline']:
        return 'deadline'
    if now - lease.get('heartbeat', lease['created_at']) >= 300:
        return 'idle-or-lost-owner'
    return None


def tick(lease, now, query=api, persist=None):
    validate(lease)
    lease['watchdog_last_check'] = now
    if lease.get('closed'):
        return True
    # Queries are always scoped by the unique, pre-registered creation alias.
    row = matching_service(lease, query)
    if row is None:
        lease['observed_status'] = 'NotFound'
        if lease.get('creation_rejected'):
            lease.update(closed=True, closed_reason='creation-rejected-no-resource')
            return True
        return False  # creation may have been accepted before a timeout
    resource = row['serviceUuid']
    if not lease.get('serviceUuid'):
        lease['serviceUuid'] = resource
        lease['reconciled_after_uncertain_create'] = True
    lease['observed_status'] = row.get('serviceStatus')
    if lease.get('start_job') and not lease.get('start_job_success'):
        jobs = query('ack_job', 'DescribeJobs', {'jobUuid': lease['start_job']})
        own = [job for job in jobs if job.get('resourceUuid') == resource
               and job.get('jobUuid') == lease['start_job']]
        if len(own) == 1 and own[0].get('done'):
            lease['start_job_success'] = bool(own[0].get('success'))
            if not own[0].get('success'):
                lease['stop_requested'] = 'start-job-failed'
                if (row.get('serviceStatus') == 'Stopped'
                        and lease.get('resumed_from_stopped_charging')):
                    lease.update(closed=True, stopped_at=now, stoppedMode='STOP_CHARGING',
                                 saveEnv=True, closed_reason='start-failed-still-stopped')
                    return True
    job_id = lease.get('stop_job')
    if job_id:
        jobs = query('ack_job', 'DescribeJobs', {'jobUuid': job_id})
        own = [job for job in jobs if job.get('resourceUuid') == resource
               and job.get('jobUuid') == job_id]
        if len(own) == 1 and own[0].get('done'):
            lease['stop_job_success'] = bool(own[0].get('success'))
            if not own[0].get('success'):
                lease.pop('stop_job', None)
                lease['stop_failures'] = lease.get('stop_failures', 0) + 1
        if row.get('serviceStatus') == 'Stopped' and lease.get('stop_job_success'):
            lease.update(closed=True, stopped_at=now, stoppedMode='STOP_CHARGING', saveEnv=True)
            return True
    reason = reason_to_stop(lease, now)
    if (reason and row.get('serviceStatus') == 'Running' and not lease.get('stop_job')
            and not lease.get('stop_uncertain')):
        if lease.get('stop_failures', 0) >= 3:
            lease['cleanup_error'] = 'Stop failed three times; manual intervention required'
            return False
        # If the connection is lost after submission, retain this marker and
        # reconcile status rather than blindly submitting a duplicate stop.
        lease['stop_uncertain'] = True
        lease['stop_reason'] = reason
        if persist is not None:
            persist(lease)
        jobs = query('ackcs', 'StopServices', {
            'zoneCode': lease['zoneCode'], 'serviceUuids': [resource],
            'stoppedMode': 'STOP_CHARGING', 'saveEnv': True,
        })
        own = [job for job in jobs if job.get('resourceUuid') == resource]
        if len(own) != 1 or not own[0].get('jobUuid'):
            raise ValueError('Unexpected stop receipt; reconcile without resending')
        lease.update(stop_job=own[0]['jobUuid'], stop_reason=reason, stop_submitted_at=now,
                     stop_uncertain=False)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['watch', 'heartbeat', 'stop', 'status', 'once', 'wait'])
    parser.add_argument('lease', type=Path)
    parser.add_argument('--phase', default='working')
    parser.add_argument('--timeout', type=int, default=180, help='bounded wait for confirmed stop')
    args = parser.parse_args()
    path = args.lease.resolve()
    if not path.is_relative_to(LEASES.resolve()) or path.is_symlink():
        parser.error('Lease must be a regular file under output/paratera/leases')
    if not 1 <= args.timeout <= 7200:
        parser.error('--timeout must be between 1 and 7200 seconds')
    wait_deadline = time.monotonic() + args.timeout
    while True:
        with workspace_lock():
            lease = json.loads(path.read_text())
            validate(lease)
            now = time.time()
            if args.action == 'heartbeat':
                if lease.get('closed') or lease.get('stop_job') or lease.get('stop_requested') or now >= lease['deadline']:
                    parser.error('Lease is stopping, closed, or expired')
                lease.update(heartbeat=now, phase=args.phase)
                save(path, lease)
                return
            if args.action == 'stop':
                lease['stop_requested'] = args.phase
                save(path, lease)
                return
            if args.action == 'status':
                print(json.dumps(lease, ensure_ascii=False, indent=2))
                return
            if args.action == 'wait':
                if lease.get('closed'):
                    confirmed = (lease.get('stop_job_success') is True
                                 and lease.get('observed_status') == 'Stopped'
                                 and lease.get('stoppedMode') == 'STOP_CHARGING'
                                 and lease.get('saveEnv') is True)
                    print(json.dumps(dict(closed=True, confirmed_saved_stop=confirmed)))
                    if not confirmed:
                        parser.exit(1, 'Lease closed without a confirmed successful stop.\n')
                    return
                if time.monotonic() >= wait_deadline:
                    parser.exit(1, 'Stop not confirmed before timeout; watchdog remains active.\n')
                # The independent service owns API polling; waiting here does
                # not renew the task heartbeat or stop a live workload itself.
                finished = False
            else:
                try:
                    finished = tick(lease, now, persist=lambda value: save(path, value))
                    lease.pop('watchdog_error', None)
                except Exception as error:
                    finished = False
                    # Avoid SDK exception text, signed headers, credentials, or passwords.
                    lease['watchdog_error'] = type(error).__name__
                    lease['watchdog_error_at'] = now
                save(path, lease)
        if finished or args.action == 'once':
            return
        time.sleep(min(5, max(0, wait_deadline-time.monotonic())) if args.action == 'wait' else 15)


if __name__ == '__main__':
    main()
