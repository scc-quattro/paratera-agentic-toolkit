"""Reject ambiguous spending plans and stale live prices, without networking."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from plan_policy import validate_plan, lease_deadline, check_live_quote, check_catalog


def sample_plan():
    return dict(payload=dict(zoneCode='example-zone', serviceModel='example-full-gpu',
                             imageUuid='example-image', aliasName='agent-cloud-test',
                             billingType='PostPaid', autoContinue=False, count=1),
                currency='CNY', unit='hour', hourly_price='2.4', max_minutes=30,
                max_compute_cost='1.2',
                pricing_sources=['https://ai.paratera.com/document/container'])


class PlanTests(unittest.TestCase):
    def test_decimal_cost_and_exact_budget_boundary(self):
        self.assertEqual(validate_plan(sample_plan()), Decimal('1.2'))
        with self.assertRaises(ValueError):
            validate_plan(dict(sample_plan(), max_compute_cost='1.1999'))

    def test_rejects_nonfinite_zero_negative_and_boolean_money(self):
        for field in ('hourly_price', 'max_compute_cost'):
            for value in (None, 0, -1, True, 'NaN', 'Infinity', float('nan')):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_plan(dict(sample_plan(), **{field: value}))

    def test_rejects_unverified_units_sources_and_duration(self):
        for field, value in [('currency', 'USD'), ('unit', 'month'), ('pricing_sources', []),
                             ('pricing_sources', ['https://example.org/price']),
                             ('pricing_sources', ['https://ai.paratera.com.evil.test/document/a']),
                             ('max_minutes', 4), ('max_minutes', 121), ('max_minutes', 5.5),
                             ('max_minutes', True), ('task_deadline', float('inf'))]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_plan(dict(sample_plan(), **{field: value}))

    def test_exact_payload_prevents_extra_purchase_options(self):
        for changes in ({'count': 2}, {'count': True}, {'billingType': 'PrePaid'},
                        {'autoContinue': True}, {'autoContinue': 0}, {'extraDisk': 100},
                        {'aliasName': 'agent-cloud-../../other'}, {'imageUuid': 'bad;command'}):
            plan = deepcopy(sample_plan()); plan['payload'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_plan(plan)

    def test_task_deadline_can_shorten_but_never_extend_lease(self):
        plan = dict(sample_plan(), task_deadline=5000)
        self.assertEqual(lease_deadline(plan, 1000), 2800)
        self.assertEqual(lease_deadline(dict(plan, task_deadline=1400), 1000), 1400)
        with self.assertRaises(ValueError):
            lease_deadline(dict(plan, task_deadline=1299), 1000)

    def test_any_price_change_or_failed_quote_prevents_mutation(self):
        good = dict(code=200, data=dict(originPrice=2.4, cashBalance=10))
        check_live_quote(sample_plan(), good)
        for response in (dict(code=500), dict(code=200, data=dict(originPrice=2.5, cashBalance=10)),
                         dict(code=200, data=dict(originPrice=2.3, cashBalance=10)),
                         dict(code=200, data=dict(originPrice=2.4, cashBalance=0)),
                         dict(code=200, data=dict(originPrice=2.4, cashBalance='NaN'))):
            with self.subTest(response=response), self.assertRaises(ValueError):
                check_live_quote(sample_plan(), response)

    def test_catalog_requires_unambiguous_exact_zone_and_one_complete_gpu(self):
        payload = sample_plan()['payload']
        row = dict(serviceModel=payload['serviceModel'], zone=dict(zoneCode=payload['zoneCode']), serviceGpus=1)
        self.assertEqual(check_catalog(payload, dict(code=200, data=[row])), row)
        for rows in ([], [row, row], [dict(row, serviceGpus=.5)], [dict(row, serviceGpus=2)],
                     [dict(row, zone=dict(zoneCode='other'))]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                check_catalog(payload, dict(code=200, data=rows))


if __name__ == '__main__':
    unittest.main()
