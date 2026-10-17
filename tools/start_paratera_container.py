"""Restart one owned stopped single-GPU container only after a live independent watchdog."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from controller import prepare_controller, verify_watchdog, check_submission_window
from plan_policy import validate_plan, lease_deadline, check_live_quote, check_catalog
from paratera_api import credentials, filter_response, sdk_client
from paratera_lease import (ROOT, LEASES, WORKSPACE, api, matching_service, save,
                           validate, workspace_lock)


def check_source(source, row, plan):
    validate_plan(plan)
    validate(source)
    if not (source.get('closed') and source.get('stop_job_success')
            and source.get('stoppedMode') == 'STOP_CHARGING' and source.get('saveEnv')):
        raise ValueError('Source lease must attest a successful saved, stopped instance')
    if not row or row.get('serviceStatus') != 'Stopped':
        raise ValueError('Owned instance is not stopped')
    instance = row.get('instanceType', {})
    if (instance.get('serviceGpus') != 1.0
            or instance.get('serviceModel') != source['serviceModel']):
        raise ValueError('A full single-GPU container is required')
    payload = plan['payload']
    expected = dict(zoneCode=source['zoneCode'], aliasName=source['aliasName'],
                    serviceModel=source['serviceModel'], imageUuid=row['image']['imageUuid'],
                    billingType='PostPaid', autoContinue=False, count=1)
    if payload != expected:
        raise ValueError('Quote payload differs from the owned instance configuration')


def start(source_path, plan_path):
    source_path = source_path.resolve()
    if not source_path.is_relative_to(LEASES.resolve()) or source_path.is_symlink():
        raise ValueError('Source must be in this workspace ledger')
    plan = json.loads(plan_path.read_text())
    validate_plan(plan)
    prepare_controller()
    with workspace_lock():
        source = json.loads(source_path.read_text())
        for path in LEASES.glob('*.json'):
            if not json.loads(path.read_text()).get('closed'):
                raise ValueError('Another workspace task still owns a lease')
        check_source(source, matching_service(source), plan)
    client_class = sdk_client('ackcs')
    from common.ClientProfileMain import ClientProfile
    from common.HttpProfileMain import HttpProfile
    access, secret = credentials(ROOT / '.paratera/credentials.json')
    profile = HttpProfile(verifyCert=True, connectTimeout=10, readTimeout=30)
    client = client_class(profile, ClientProfile(access, secret))
    product = sdk_client('ack_product')(profile, ClientProfile(access, secret))
    check_catalog(plan['payload'], product.GenericRequest({}, 'DescribeACKServiceTypes', lambda x: x))
    quote = client.GenericRequest(plan['payload'], 'InquiryPriceCreateServices', lambda x: x)
    check_live_quote(plan, quote)
    save(plan_path.with_name('restart-quote-final.json'), filter_response(quote))
    now = time.time()
    task_name = 'agent-cloud-task-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    path = LEASES / (task_name + '.json')
    lease = dict(workspace=WORKSPACE, resource_type='ACKCS', task='agent-task',
                 **{k: source[k] for k in ('serviceUuid', 'aliasName', 'zoneCode',
                                           'billingType', 'serviceModel', 'preflight_alias_absent')},
                 created_at=now, deadline=lease_deadline(plan, now), heartbeat=now, phase='preparing-start',
                 quote=plan, source_lease=str(source_path.relative_to(ROOT)),
                 resumed_from_stopped_charging=True, watchdog_unit=task_name+'-watchdog')
    validate(lease)
    with workspace_lock():
        for previous in LEASES.glob('*.json'):
            if not json.loads(previous.read_text()).get('closed'):
                raise ValueError('A competing lease appeared')
        check_source(source, matching_service(source), plan)
        save(path, lease)
    unit = lease['watchdog_unit']
    verify_watchdog(lease, path, 'Stopped')
    with workspace_lock():
        lease = json.loads(path.read_text())
        check_submission_window(lease)
        check_source(source, matching_service(source), plan)
        lease.update(heartbeat=time.time(), phase='starting', start_attempted_at=time.time())
        save(path, lease)
        # Durable intent precedes the one and only restart request. Do not retry
        # an uncertain response; reconcile this same registered UUID instead.
        try:
            jobs = api('ackcs', 'StartServices', dict(zoneCode=lease['zoneCode'],
                                                     serviceUuids=[lease['serviceUuid']]))
        except Exception as error:
            lease['start_error'] = type(error).__name__
            save(path, lease)
            raise
        own = [job for job in jobs if job.get('resourceUuid') == lease['serviceUuid']]
        if len(own) != 1 or not own[0].get('jobUuid'):
            raise ValueError('Unexpected restart receipt; reconcile the existing resource')
        lease.update(start_job=own[0]['jobUuid'], heartbeat=time.time())
        save(path, lease)
    print(json.dumps({'lease': str(path.relative_to(ROOT)), 'serviceUuid': lease['serviceUuid'],
                      'watchdog_unit': unit, 'hourly_price_CNY': plan['hourly_price'],
                      'deadline_utc': datetime.fromtimestamp(lease['deadline'], timezone.utc).isoformat()}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-lease', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    try:
        start(args.source_lease, args.plan)
    except Exception as error:
        parser.exit(1, f'Start did not complete ({type(error).__name__}); inspect the safe lease state.\n')


if __name__ == '__main__':
    main()
