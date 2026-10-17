"""Mock the complete create path: never touch cloud, SSH, or systemd."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import create_paratera_container as create
import controller
import paratera_remote as remote
from paratera_lease import WORKSPACE
from start_paratera_container import check_source
from test_plan_policy import sample_plan


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.leases = self.root/'output/paratera/leases'; self.leases.mkdir(parents=True)
        self.plan = sample_plan()
        self.plan_path = self.root/'plan.json'; self.plan_path.write_text(json.dumps(self.plan))
        self.path = self.leases/'agent-cloud-test.json'
        self.mutations = []
        self.ready = True
        self.quote = dict(code=200, data=dict(originPrice=2.4, cashBalance=10))
        owner = self
        class FakeClient:
            def __init__(self, *args): pass
            def GenericRequest(self, payload, action, callback):
                if action == 'DescribeACKServiceTypes':
                    return dict(code=200, data=[dict(serviceModel='example-full-gpu',
                                    zone=dict(zoneCode='example-zone'), serviceGpus=1)])
                if action == 'InquiryPriceCreateServices': return owner.quote
                raise AssertionError('unexpected SDK action')
        def watch(lease, path):
            if not owner.ready: raise RuntimeError('independent manager unavailable')
            lease.update(watchdog_last_check=2000, observed_status='NotFound')
            create.save(path, lease)
        def mutate(service, action, payload):
            on_disk = json.loads(owner.path.read_text())
            self.assertEqual(on_disk['create_attempted_at'], 1000)
            self.assertEqual(on_disk['observed_status'], 'NotFound')
            self.mutations.append((service, action, payload))
            return [dict(resourceUuid='ackcs-example', jobUuid='example-job')]
        modules = {'common.ClientProfileMain': SimpleNamespace(ClientProfile=lambda *a: None),
                   'common.HttpProfileMain': SimpleNamespace(HttpProfile=lambda **kw: None)}
        patches = [patch.object(create, 'ROOT', self.root), patch.object(create, 'LEASES', self.leases),
                   patch.object(create, 'workspace_lock'), patch.object(create, 'prepare_controller'),
                   patch.object(create, 'sdk_client', return_value=FakeClient),
                   patch.object(create, 'credentials', return_value=('test-access', 'test-secret')),
                   patch.object(create, 'matching_service', return_value=None),
                   patch.object(controller, 'start_watchdog', side_effect=watch),
                   patch.object(create, 'api', side_effect=mutate),
                   patch.object(create.time, 'time', return_value=1000),
                   patch.object(controller.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='active\n')),
                   patch.dict(sys.modules, modules)]
        for item in patches:
            item.start(); self.addCleanup(item.stop)

    def test_single_create_after_independent_ready_check_and_durable_intent(self):
        create.create(self.plan_path)
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(self.mutations[0][1], 'CreateServices')
        lease = json.loads(self.path.read_text())
        self.assertEqual(lease['serviceUuid'], 'ackcs-example')
        self.assertEqual(lease['deadline'], 2800)

    def test_unavailable_watchdog_prevents_purchase(self):
        self.ready = False
        with self.assertRaises(RuntimeError): create.create(self.plan_path)
        self.assertFalse(self.mutations)
        self.assertTrue(json.loads(self.path.read_text())['closed'])

    def test_changed_live_quote_prevents_purchase(self):
        self.quote['data']['originPrice'] = 2.5
        with self.assertRaises(ValueError): create.create(self.plan_path)
        self.assertFalse(self.mutations)
        self.assertFalse(self.path.exists())

    def test_open_lease_prevents_second_purchase(self):
        create.create(self.plan_path)
        with self.assertRaises(ValueError): create.create(self.plan_path)
        self.assertEqual(len(self.mutations), 1)

    def test_lost_create_receipt_keeps_intent_and_forbids_retry(self):
        with patch.object(create, 'api', side_effect=TimeoutError) as api:
            with self.assertRaises(TimeoutError): create.create(self.plan_path)
            self.assertEqual(api.call_count, 1)
            with self.assertRaises(ValueError): create.create(self.plan_path)
            self.assertEqual(api.call_count, 1)
        lease = json.loads(self.path.read_text())
        self.assertEqual(lease['create_error'], 'TimeoutError')
        self.assertIsNone(lease['serviceUuid'])


class RestartTests(unittest.TestCase):
    def test_restart_requires_saved_stopped_single_gpu_exact_configuration(self):
        plan = sample_plan()
        source = dict(workspace=WORKSPACE, resource_type='ACKCS', billingType='PostPaid',
                      aliasName='agent-cloud-test', zoneCode='example-zone', serviceUuid='ackcs-example',
                      created_at=100, deadline=7300, preflight_alias_absent=True, closed=True,
                      stop_job_success=True, stoppedMode='STOP_CHARGING', saveEnv=True,
                      serviceModel='example-full-gpu')
        row = dict(serviceStatus='Stopped', instanceType=dict(serviceModel='example-full-gpu',
                   serviceGpus=1), image=dict(imageUuid='example-image'))
        check_source(source, row, plan)
        for changes in (dict(closed=False), dict(stop_job_success=False), dict(saveEnv=False)):
            with self.assertRaises(ValueError): check_source(dict(source, **changes), row, plan)
        for count in (.5, 2):
            wrong = deepcopy(row); wrong['instanceType']['serviceGpus'] = count
            with self.assertRaises(ValueError): check_source(source, wrong, plan)
        for field in ('aliasName', 'zoneCode', 'imageUuid', 'serviceModel'):
            wrong = deepcopy(plan); wrong['payload'][field] += '-other'
            with self.assertRaises(ValueError): check_source(source, row, wrong)


class DownloadTests(unittest.TestCase):
    def test_one_ssh_session_and_separate_binary_stderr(self):
        directory = remote.ROOT/'output/paratera/results'; directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as tmp:
            args = SimpleNamespace(lease=remote.ROOT/'output/paratera/leases/fake.json', phase='test',
                                   action='download', remote_directory='/tmp/agent-cloud-results',
                                   archive=Path(tmp)/'result.tar.gz', log=Path(tmp)/'transfer.log')
            def spawn(argv, **kw):
                self.assertIsNot(kw['stdout'], kw['stderr'])
                kw['stdout'].write(b'binary-evidence'); kw['stdout'].flush()
                kw['stderr'].write(b'diagnostic-only'); kw['stderr'].flush()
                return SimpleNamespace(poll=lambda: 0, returncode=0)
            with patch.object(remote, 'heartbeat', return_value={}), \
                 patch.object(remote, 'workspace_lock'), \
                 patch.object(remote, 'connection', return_value=(Path(tmp)/'private.json',
                              dict(user='fake', host='invalid.example', port=22))), \
                 patch.object(remote.subprocess, 'Popen', side_effect=spawn) as proc:
                self.assertEqual(remote.run(args), 0)
                self.assertEqual(proc.call_count, 1)
                with self.assertRaises(ValueError): remote.run(args)
                self.assertEqual(proc.call_count, 1)
            self.assertEqual(args.archive.read_bytes(), b'binary-evidence')
            self.assertEqual(args.log.read_bytes(), b'diagnostic-only')


if __name__ == '__main__':
    unittest.main()
