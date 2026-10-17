"""Cloud safety properties tested without credentials or real instances."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from paratera_lease import WORKSPACE, reason_to_stop, tick


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.lease = dict(workspace=WORKSPACE, resource_type='ACKCS', billingType='PostPaid',
                          aliasName='agent-cloud-test', zoneCode='test-zone',
                          serviceUuid='ackcs-test', created_at=100, deadline=7300,
                          heartbeat=100, preflight_alias_absent=True)
        self.row = dict(aliasName=self.lease['aliasName'], zone={'zoneCode': 'test-zone'},
                        serviceUuid='ackcs-test', billingType='PostPaid', serviceStatus='Running')
        self.calls = []
        self.done = False
        self.success = False

    def api(self, service, action, payload):
        self.calls.append((service, action, payload))
        if action == 'DescribeServices':
            return {'rows': [deepcopy(self.row)]}
        if action == 'StopServices':
            return [dict(resourceUuid='ackcs-test', jobUuid='job-test')]
        if action == 'DescribeJobs':
            return [dict(resourceUuid='ackcs-test', jobUuid='job-test',
                         done=self.done, success=self.success)]
        self.fail(action)

    def test_deadline_and_five_minute_owner_loss(self):
        self.assertIsNone(reason_to_stop(self.lease, 399))
        self.assertEqual(reason_to_stop(self.lease, 400), 'idle-or-lost-owner')
        self.assertEqual(reason_to_stop(self.lease, 7300), 'deadline')

    def test_stop_uses_platform_and_preserves_environment(self):
        self.assertFalse(tick(self.lease, 400, self.api))
        self.assertEqual(self.calls[-1], ('ackcs', 'StopServices', {
            'zoneCode': 'test-zone', 'serviceUuids': ['ackcs-test'],
            'stoppedMode': 'STOP_CHARGING', 'saveEnv': True}))
        self.assertFalse(tick(self.lease, 415, self.api))
        self.assertEqual(sum(call[1] == 'StopServices' for call in self.calls), 1)

    def test_only_async_success_and_stopped_close_lease(self):
        tick(self.lease, 400, self.api)
        self.done = self.success = True
        self.assertFalse(tick(self.lease, 415, self.api))
        self.row['serviceStatus'] = 'Stopped'
        self.assertTrue(tick(self.lease, 430, self.api))
        self.assertTrue(self.lease['closed'])

    def test_stopped_alone_does_not_attest_saved_stop_or_complete_job(self):
        self.row['serviceStatus'] = 'Stopped'
        self.assertFalse(tick(self.lease, 430, self.api))
        self.assertFalse(self.lease.get('closed'))

    def test_job_for_another_resource_cannot_confirm_stop(self):
        tick(self.lease, 400, self.api)
        self.row['serviceStatus'] = 'Stopped'
        def unrelated(service, action, payload):
            if action == 'DescribeJobs':
                return [dict(resourceUuid='ackcs-other', jobUuid='job-test', done=True, success=True)]
            return self.api(service, action, payload)
        self.assertFalse(tick(self.lease, 430, unrelated))
        self.assertFalse(self.lease.get('closed'))

    def test_rejects_wrong_workspace_or_overlong_lease(self):
        for field, value in [('workspace', 'other'), ('deadline', 7301),
                             ('billingType', 'PrePaid'), ('preflight_alias_absent', False)]:
            lease = {**self.lease, field: value}
            with self.assertRaises(ValueError):
                tick(lease, 400, self.api)
        self.assertEqual(self.calls, [])

    def test_uuid_zone_and_alias_must_all_match(self):
        for field, value in [('serviceUuid', 'ackcs-other'), ('aliasName', 'agent-cloud-other'),
                             ('zone', {'zoneCode': 'other'})]:
            original = deepcopy(self.row)
            self.row[field] = value
            self.assertFalse(tick(self.lease, 400, self.api))
            self.assertFalse(any(call[1] == 'StopServices' for call in self.calls))
            self.row = original

    def test_uncertain_create_reconciles_only_registered_alias(self):
        self.lease.pop('serviceUuid')
        tick(self.lease, 400, self.api)
        self.assertEqual(self.lease['serviceUuid'], 'ackcs-test')
        self.assertTrue(self.lease['reconciled_after_uncertain_create'])

    def test_uncertain_stop_is_not_blindly_resubmitted(self):
        def broken(service, action, payload):
            if action == 'StopServices':
                raise TimeoutError
            return self.api(service, action, payload)
        with self.assertRaises(TimeoutError):
            tick(self.lease, 400, broken)
        self.assertTrue(self.lease['stop_uncertain'])
        self.assertFalse(tick(self.lease, 415, self.api))
        self.assertFalse(any(call[1] == 'StopServices' for call in self.calls))

    def test_stop_intent_is_durable_before_network_request(self):
        durable = {}
        def persist(value):
            durable.update(deepcopy(value))
        def die_after_submission(service, action, payload):
            if action == 'StopServices':
                self.assertTrue(durable['stop_uncertain'])
                raise SystemExit
            return self.api(service, action, payload)
        with self.assertRaises(SystemExit):
            tick(self.lease, 400, die_after_submission, persist=persist)
        self.assertTrue(durable['stop_uncertain'])

    def test_failed_restart_closes_only_when_resource_is_still_stopped(self):
        self.lease.update(start_job='job-test', resumed_from_stopped_charging=True)
        self.done = True
        self.row['serviceStatus'] = 'Stopped'
        self.assertTrue(tick(self.lease, 115, self.api))
        self.assertTrue(self.lease['closed'])
        self.assertEqual(self.lease['stoppedMode'], 'STOP_CHARGING')
        self.assertFalse(any(c[1] == 'StopServices' for c in self.calls))

    def test_failed_restart_with_running_resource_still_requests_platform_stop(self):
        self.lease.update(start_job='job-test', resumed_from_stopped_charging=True)
        self.done = True
        self.assertFalse(tick(self.lease, 115, self.api))
        self.assertFalse(self.lease.get('closed', False))
        self.assertEqual(self.calls[-1][1], 'StopServices')

    def test_successful_restart_does_not_close_stopped_state_from_stale_query(self):
        self.lease.update(start_job='job-test', resumed_from_stopped_charging=True)
        self.done = self.success = True
        self.row['serviceStatus'] = 'Stopped'
        self.assertFalse(tick(self.lease, 115, self.api))
        self.assertFalse(self.lease.get('closed', False))
        self.assertTrue(self.lease['start_job_success'])


if __name__ == '__main__':
    unittest.main()
