"""Check gateway URL parsing without making any SSH or API connections."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from paratera_remote import ssh_target


class SSHTargetTests(unittest.TestCase):
    def test_gateway_login_name_contains_container_uuid(self):
        self.assertEqual(ssh_target('ssh://pod@ackcs-test@ssh.example.org:2345'),
                         ('pod@ackcs-test', 'ssh.example.org', 2345))

    def test_shell_command_and_bare_destination(self):
        self.assertEqual(ssh_target('ssh -p 2345 pod@ssh.example.org'),
                         ('pod', 'ssh.example.org', 2345))
        self.assertEqual(ssh_target('pod@ssh.example.org:2345'),
                         ('pod', 'ssh.example.org', 2345))

    def test_rejects_shell_metacharacters_and_invalid_ports(self):
        for url in ['ssh://pod@bad;host:2345', 'ssh://bad;user@host:22',
                    'ssh://pod@host:65536', 'sh -c something', 'pod@host:0']:
            with self.assertRaises(ValueError):
                ssh_target(url)


if __name__ == '__main__':
    unittest.main()
