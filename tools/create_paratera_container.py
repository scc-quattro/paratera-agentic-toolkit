"""Create one quoted single-GPU container after starting an independent watchdog.

The plan records verified CNY/hour units, the exact quoted payload, the matching
SDK quote, and the public pricing sources. It must contain no credentials.
"""

import argparse
import json
from pathlib import Path
import time

from paratera_api import credentials, sdk_client, filter_response
from paratera_lease import (ROOT, LEASES, WORKSPACE, api, save, matching_service,
                           validate, workspace_lock)
from controller import prepare_controller, verify_watchdog, check_submission_window
from plan_policy import validate_plan, lease_deadline, check_live_quote, check_catalog


def create(plan_path):
    plan = json.loads(plan_path.read_text())
    validate_plan(plan)
    prepare_controller()
    payload = plan['payload']
    cls = sdk_client('ack_product')
    from common.ClientProfileMain import ClientProfile
    from common.HttpProfileMain import HttpProfile
    # Use only the official SDK's fixed, checksum-verified clients.
    access, secret = credentials(ROOT / '.paratera/credentials.json')
    profile = HttpProfile(verifyCert=True, connectTimeout=10, readTimeout=30)
    product = cls(profile, ClientProfile(access, secret))
    catalog = product.GenericRequest({}, 'DescribeACKServiceTypes', lambda value: value)
    check_catalog(payload, catalog)
    quoted = sdk_client('ackcs')(profile, ClientProfile(access, secret))
    quote = quoted.GenericRequest(payload, 'InquiryPriceCreateServices', lambda value: value)
    check_live_quote(plan, quote)
    save(plan_path.with_name('container-quote-final.json'), filter_response(quote))
    now = time.time()
    deadline = lease_deadline(plan, now)
    lease = dict(workspace=WORKSPACE, resource_type='ACKCS', task='agent-task',
                 **{key: payload[key] for key in ['aliasName', 'zoneCode', 'billingType', 'serviceModel']},
                 serviceUuid=None, created_at=now, deadline=deadline, heartbeat=now,
                 phase='preparing-create', quote=plan, preflight_alias_absent=True)
    validate(lease)
    path = LEASES / (payload['aliasName'] + '.json')
    unit = payload['aliasName'] + '-watchdog'
    with workspace_lock():
        for previous in LEASES.glob('*.json'):
            if not json.loads(previous.read_text()).get('closed'):
                raise ValueError('Another workspace lease is still open')
        if matching_service(lease) is not None:
            raise ValueError('Creation alias already exists')
        lease['watchdog_unit'] = unit
        save(path, lease)
    verify_watchdog(lease, path, 'NotFound')
    with workspace_lock():
        lease = json.loads(path.read_text())
        check_submission_window(lease)
        lease.update(heartbeat=time.time(), phase='creating', create_attempted_at=time.time())
        save(path, lease)
        # The intent is durable before the one and only creation request.
        # On timeout the watchdog reconciles this alias, without a duplicate create.
        try:
            jobs = api('ackcs', 'CreateServices', payload)
        except Exception as error:
            lease['create_error'] = type(error).__name__
            save(path, lease)
            raise
        own = [job for job in jobs if job.get('resourceUuid', '').startswith('ackcs-')]
        if len(own) != 1 or not own[0].get('jobUuid'):
            lease['create_error'] = 'UnexpectedReceipt'
            save(path, lease)
            raise ValueError('Unexpected create receipt; reconcile before any further purchase')
        lease.update(serviceUuid=own[0]['resourceUuid'], create_job=own[0]['jobUuid'],
                     heartbeat=time.time())
        save(path, lease)
    print(json.dumps({'lease': str(path.relative_to(ROOT)), 'serviceUuid': lease['serviceUuid'],
                      'create_job': lease['create_job'], 'watchdog_unit': unit,
                      'currency': plan['currency'], 'hourly_price': plan['hourly_price'],
                      'deadline_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(lease['deadline']))}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    try:
        create(args.plan)
    except Exception as error:
        parser.exit(1, f'Create did not complete ({type(error).__name__}); inspect safe lease state.\n')


if __name__ == '__main__':
    main()
