import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mind import lab_access


@unittest.skipUnless(shutil.which('ssh-keygen'), 'requires ssh-keygen')
class LabEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.observe = self.make_public_key('observe')
        self.develop = self.make_public_key('develop')

    def make_public_key(self, name):
        path = self.directory / name
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], check=True)
        return path.with_suffix('.pub')

    def enroll(self, observe, dev=None):
        output = io.StringIO()
        with redirect_stdout(output):
            lab_access.enroll(self.directory, '192.168.1.50', observe, dev, '192.168.1.20')
        return output.getvalue().splitlines()

    def test_pins_persist_while_authorization_is_independent(self):
        first = self.enroll(self.observe)
        both = self.enroll(self.observe, self.develop)
        self.assertEqual(first, both[:1])
        self.assertEqual(both[1], both[0].replace(':2222', ':2223'))
        directory = self.directory / 'lab-access'
        credentials = self.directory / 'credentials'
        credentials.mkdir()
        (credentials / 'access').write_bytes((directory / 'access.json').read_bytes())
        observe, dev = (self.directory / mode for mode in ('observe-run', 'dev-run'))
        observe.mkdir()
        dev.mkdir()
        self.assertIn('PermitRootLogin forced-commands-only', lab_access.ssh_config('observe', observe, credentials))
        self.assertIn('PermitRootLogin prohibit-password', lab_access.ssh_config('dev', dev, credentials))
        metrics_key = (observe / 'authorized-being-observe').read_text()
        root_control_key = (observe / 'authorized-root').read_text()
        dev_key = (dev / 'authorized-root').read_text()
        self.assertIn('from="192.168.1.20",restrict,command=', metrics_key)
        self.assertIn('being-lab-access metrics', metrics_key)
        self.assertIn('being-lab-access control', root_control_key)
        self.assertIn('restrict,pty', dev_key)
        self.assertFalse((dev / 'authorized-being-observe').exists())
        self.assertFalse((directory / 'access.json').stat().st_mode & 0o077)

    def test_invalid_or_reused_keys_do_not_enroll(self):
        with self.assertRaisesRegex(ValueError, 'different'):
            self.enroll(self.observe, self.observe)
        with self.assertRaisesRegex(ValueError, 'Expected one'):
            lab_access.public_key(self.observe.read_text() + 'command="sh" ignored\n')
        with self.assertRaisesRegex(ValueError, 'private IPv4'):
            lab_access.lan_address('8.8.8.8')
        self.assertFalse((self.directory / 'lab-access').exists())

    def test_pair_reader_fails_closed_when_peer_is_absent_invalid_or_unprotected(self):
        self.assertIsNone(lab_access.load_pair(self.directory))
        self.enroll(self.observe, self.develop)
        expected = {'address': '192.168.1.50', 'controller': '192.168.1.20', 'ethernet': None, 'dev_enabled': True}
        self.assertEqual(lab_access.load_pair(self.directory), expected)
        access = self.directory / 'lab-access/access.json'
        original = json.loads(access.read_text())
        for invalid in (None, '8.8.8.8', '192.168.1.50', 0xC0A80114):
            with self.subTest(controller=invalid):
                access.write_text(json.dumps(dict(original, controller=invalid)))
                self.assertIsNone(lab_access.load_pair(self.directory))
        access.write_text(json.dumps(original))
        self.assertEqual(lab_access.load_pair(self.directory), expected)
        access.chmod(0o644)
        self.assertIsNone(lab_access.load_pair(self.directory))


class LabAccessCommandTests(unittest.TestCase):
    def test_metrics_only_forwards_explicit_numeric_arguments(self):
        with patch('mind.hardware_metrics.main') as metrics:
            lab_access.metrics('metrics --seconds 0 --interval 1')
            metrics.assert_called_once_with(['--seconds', '0', '--interval', '1'])
            for command in ('', 'sh', 'metrics --seconds 0 --interval 1; id', 'metrics --seconds ０ --interval 1'):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    lab_access.metrics(command)
            self.assertEqual(metrics.call_count, 1)

    def test_control_rejects_arbitrary_commands(self):
        with patch('mind.lab_access.subprocess.run') as run, redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                lab_access.control('dev-on; id')
            run.assert_not_called()
            lab_access.control('dev-off')
            run.assert_called_once_with(
                ['/usr/bin/systemctl', 'stop', 'being-lab-dev.service'], check=True, timeout=15,
            )

    def test_wired_config_can_be_prepared_without_starting_network_services(self):
        pair = {'ethernet': 'enp1s0'}
        with tempfile.TemporaryDirectory() as directory:
            configuration = Path(directory) / 'network/01-being-lab-ethernet.network'
            with (patch.object(lab_access, 'ETHERNET_NETWORK', configuration),
                  patch.object(lab_access.subprocess, 'run') as run):
                lab_access.configure_ethernet(pair, 'paired-lan')
                self.assertIn('[Match]\nName=enp1s0\nType=ether\n', configuration.read_text())
                self.assertIn('DHCP=ipv4\n', configuration.read_text())
                lab_access.configure_ethernet(pair, 'internet')
                self.assertIn('DHCP=yes\n', configuration.read_text())
                lab_access.configure_ethernet(pair, 'local-only')
                self.assertFalse(configuration.exists())
                run.assert_not_called()

    def test_online_enrollment_queues_policy_refresh_and_offline_enrollment_only_saves(self):
        argv = ['being-lab-access', 'enroll', '192.168.1.50', '--controller-ip', '192.168.1.20',
                '--observe-key', 'observe.pub']
        with tempfile.TemporaryDirectory() as directory:
            mind = Path(directory)
            with (patch.object(lab_access.Path, 'is_mount', return_value=True),
                  patch.object(lab_access.os, 'geteuid', return_value=0),
                  patch.object(lab_access, 'MIND', mind), patch.object(lab_access, 'enroll') as enroll,
                  patch('mind.wifi.offline_mind_mount', return_value=mind),
                  patch.object(lab_access.subprocess, 'run') as run):
                with patch.object(lab_access.sys, 'argv', argv):
                    lab_access.main()
                run.assert_called_once_with(['/usr/sbin/being-network', 'refresh-pair'], check=True, timeout=10)
                run.reset_mock()
                with patch.object(lab_access.sys, 'argv', [*argv, '--offline-mind', str(mind)]):
                    lab_access.main()
                run.assert_not_called()
                self.assertEqual(enroll.call_count, 2)
