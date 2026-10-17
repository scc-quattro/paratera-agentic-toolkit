"""Pure, offline validation of a bounded single-container spending plan."""

from decimal import Decimal, InvalidOperation
import math
import re
import time
from urllib.parse import urlparse


PAYLOAD_FIELDS = {'zoneCode', 'serviceModel', 'imageUuid', 'aliasName',
                  'billingType', 'autoContinue', 'count'}


def positive_decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError('Expected a positive finite monetary amount')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Expected a positive finite monetary amount') from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError('Expected a positive finite monetary amount')
    return amount


def validate_plan(plan):
    payload = plan.get('payload', {})
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS:
        raise ValueError('Plan must contain the exact supported creation payload')
    if (type(payload['count']) is not int or payload['count'] != 1
            or payload['billingType'] != 'PostPaid' or payload['autoContinue'] is not False):
        raise ValueError('Exactly one non-renewing postpaid container is required')
    for key in ('zoneCode', 'serviceModel', 'imageUuid'):
        if not isinstance(payload[key], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', payload[key]):
            raise ValueError('Invalid plan identifier')
    if not isinstance(payload['aliasName'], str) or not re.fullmatch(
            r'agent-cloud-[a-z0-9-]{1,48}', payload['aliasName']):
        raise ValueError('Use a unique agent-cloud- alias with lowercase letters, digits and hyphens')
    minutes = plan.get('max_minutes')
    if type(minutes) is not int or not 5 <= minutes <= 120:
        raise ValueError('max_minutes must be an integer between 5 and 120')
    if plan.get('currency') != 'CNY' or plan.get('unit') != 'hour':
        raise ValueError('Verify the price currency and hourly unit before creating a plan')
    sources = plan.get('pricing_sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('Record the official pricing sources')
    for source in sources:
        parsed = urlparse(source) if isinstance(source, str) else None
        if (not parsed or parsed.scheme != 'https' or parsed.hostname != 'ai.paratera.com'
                or not parsed.path.startswith('/document/') or parsed.username or parsed.password):
            raise ValueError('Pricing sources must be official HTTPS documentation URLs')
    price = positive_decimal(plan.get('hourly_price'))
    budget = positive_decimal(plan.get('max_compute_cost'))
    estimate = price * Decimal(minutes) / Decimal(60)
    if estimate > budget:
        raise ValueError('Estimated lease compute charge exceeds max_compute_cost')
    deadline = plan.get('task_deadline')
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError('task_deadline must be a finite Unix timestamp')
    return estimate


def lease_deadline(plan, now=None):
    validate_plan(plan)
    now = time.time() if now is None else now
    deadline = min(now + plan['max_minutes'] * 60,
                   plan.get('task_deadline', now + plan['max_minutes'] * 60))
    if deadline - now < 300:
        raise ValueError('At least five minutes must remain before startup')
    return deadline


def check_live_quote(plan, response):
    validate_plan(plan)
    data = response.get('data', {})
    if response.get('code') != 200 or positive_decimal(data.get('originPrice')) != positive_decimal(plan['hourly_price']):
        raise ValueError('The complete live quote changed or failed; no mutation allowed')
    # The platform freezes one hour even for shorter postpaid tasks. A zero
    # balance is not a valid positive amount, so compare it separately.
    try:
        balance = Decimal(str(data.get('cashBalance', 0)))
    except InvalidOperation:
        raise ValueError('Cannot verify available balance') from None
    if not balance.is_finite() or balance < positive_decimal(plan['hourly_price']):
        raise ValueError('Insufficient available balance for the one-hour freeze')


def check_catalog(payload, response):
    rows = [row for row in response.get('data', [])
            if row.get('serviceModel') == payload['serviceModel']
            and row.get('zone', {}).get('zoneCode') == payload['zoneCode']]
    if response.get('code') != 200 or len(rows) != 1 or rows[0].get('serviceGpus') != 1.0:
        raise ValueError('The live catalog must identify exactly one full GPU')
    return rows[0]
