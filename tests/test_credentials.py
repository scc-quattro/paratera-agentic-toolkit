"""Credential-file permissions and response filtering stay independent of SDK."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from paratera_api import credentials, filter_response
from bootstrap import verify_wheel


class PrivacyTests(unittest.TestCase):
    def test_recursive_whitelist_removes_login_fields(self):
        response = dict(code=200, message='sensitive server text', data=dict(rows=[dict(
            serviceUuid='ackcs-example', password='never-output', sshUrl='never-output',
            image=dict(imageUuid='example-image', token='never-output'))],
            cashBalance=100, accessKey='never-output'))
        safe = filter_response(response)
        self.assertNotIn('never-output', json.dumps(safe))
        self.assertNotIn('cashBalance', json.dumps(safe))
        self.assertEqual(safe['data']['rows'][0]['serviceUuid'], 'ackcs-example')

    def test_partial_environment_is_not_mixed_with_file_credentials(self):
        with patch.dict(os.environ, {'PARATERA_ACCESS_KEY': 'test-access'}, clear=True):
            with self.assertRaises(ValueError):
                credentials(Path('/nonexistent'))

    def test_private_file_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory)/'private.json'
            path.write_text(json.dumps(dict(access_key='test-access', secret_access_key='test-secret')))
            path.chmod(0o644)
            with self.assertRaises(ValueError): credentials(path)
            path.chmod(0o600)
            self.assertEqual(credentials(path), ('test-access', 'test-secret'))
            with patch.dict(os.environ, {'PARATERA_ACCESS_KEY': 'env-access',
                                         'PARATERA_SECRET_ACCESS_KEY': 'env-secret'}):
                self.assertEqual(credentials(path), ('env-access', 'env-secret'))
            link = Path(directory)/'link.json'; link.symlink_to(path)
            with self.assertRaises(ValueError): credentials(link)

    def test_tampered_sdk_is_rejected_before_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'fake.whl'; path.write_bytes(b'not-the-sdk')
            with self.assertRaises(ValueError): verify_wheel(path)


if __name__ == '__main__':
    unittest.main()
