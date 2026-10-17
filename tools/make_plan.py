"""Fetch a current catalog and complete quote; write a local plan without renting."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import secrets

from paratera_api import ROOT, credentials, filter_response, sdk_client
from paratera_lease import save
from plan_policy import validate_plan, check_catalog, positive_decimal


def make_plan(args):
    if not args.verified_cny_per_hour:
        raise ValueError('Verify the official currency and hourly unit before planning')
    alias = args.alias or ('agent-cloud-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                           + '-' + secrets.token_hex(3))
    payload = dict(zoneCode=args.zone, serviceModel=args.model, imageUuid=args.image,
                   aliasName=alias, billingType='PostPaid', autoContinue=False, count=1)
    # Validate identifiers, duration, budget and evidence before any network call.
    plan = dict(payload=payload, currency='CNY', unit='hour', hourly_price='1',
                max_minutes=args.minutes, max_compute_cost=args.max_compute_cost,
                pricing_sources=args.pricing_source)
    # A placeholder rate must not decide whether the real price fits the budget.
    preliminary = dict(plan, max_compute_cost='1000000')
    validate_plan(preliminary)
    positive_decimal(plan['max_compute_cost'])
    product_class = sdk_client('ack_product')
    from common.ClientProfileMain import ClientProfile
    from common.HttpProfileMain import HttpProfile
    access, secret = credentials(ROOT / '.paratera/credentials.json')
    profile = HttpProfile(verifyCert=True, connectTimeout=10, readTimeout=30)
    auth = ClientProfile(access, secret)
    catalog = product_class(profile, auth).GenericRequest({}, 'DescribeACKServiceTypes', lambda x: x)
    row = check_catalog(payload, catalog)
    response = sdk_client('ackcs')(profile, auth).GenericRequest(payload, 'InquiryPriceCreateServices', lambda x: x)
    if response.get('code') != 200:
        raise ValueError('Price query failed')
    plan['hourly_price'] = str(positive_decimal(response['data'].get('originPrice')))
    estimate = validate_plan(plan)
    plan.update(quoted_at=datetime.now(timezone.utc).isoformat(),
                estimated_compute_cost=str(estimate),
                catalog=filter_response(row), live_quote=filter_response(response))
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / 'plans').resolve()):
        raise ValueError('Plans must be saved under the ignored plans/ directory')
    if output.exists():
        raise ValueError('Preserve the previous quote; use a new plan filename')
    save(output, plan)
    print(json.dumps(dict(plan=str(output.relative_to(ROOT)), aliasName=alias,
                          currency='CNY', unit='hour', hourly_price=plan['hourly_price'],
                          estimated_compute_cost=str(estimate),
                          max_compute_cost=plan['max_compute_cost'],
                          mutation_sent=False), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zone', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--alias', help='reuse the exact owned alias only for a restart plan')
    parser.add_argument('--minutes', type=int, required=True)
    parser.add_argument('--max-compute-cost', required=True, help='CNY compute budget; storage is separate')
    parser.add_argument('--verified-cny-per-hour', action='store_true')
    parser.add_argument('--pricing-source', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        make_plan(args)
    except Exception as error:
        parser.exit(1, f'Plan refused ({type(error).__name__}); no purchase was sent.\n')


if __name__ == '__main__':
    main()
