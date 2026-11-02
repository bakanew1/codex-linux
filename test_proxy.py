import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
import broker
import client_mcp
import common
import remote_gate

class ProcessTests(unittest.TestCase):

    def test_stderr_is_not_returned(self):
        out = common.bounded_run([sys.executable, '-c', "import sys;sys.stderr.write('PRIVATE');print('OK')"], b'', 5)
        self.assertEqual(out.strip(), b'OK')

    def test_failure_is_sanitized(self):
        with self.assertRaises(common.Rejected) as error:
            common.bounded_run([sys.executable, '-c', "import sys;print('PRIVATE');sys.stderr.write('PRIVATE');sys.exit(3)"], b'', 5)
        self.assertEqual(error.exception.code, 'CONNECTION_FAILED')
        self.assertNotIn('PRIVATE', str(error.exception))

    def test_timeout(self):
        with self.assertRaises(common.Rejected) as error:
            common.bounded_run([sys.executable, '-c', 'import time;time.sleep(5)'], b'', 0.1)
        self.assertEqual(error.exception.code, 'TIMEOUT')

    def test_output_cap_on_each_pipe(self):
        for channel in ('stdout', 'stderr'):
            with self.subTest(channel=channel), self.assertRaises(common.Rejected) as error:
                common.bounded_run([sys.executable, '-c', "import sys;sys.%s.write('X'*4194305);sys.%s.flush()" % (channel, channel)], b'', 5)
            self.assertEqual(error.exception.code, 'OUTPUT_LIMIT')

class BrokerTests(unittest.TestCase):

    def config(self):
        return {'host_alias': 'hpc', 'ssh_executable': sys.executable, 'remote_gate': '/opt/hpc/remote_gate.py', 'remote_config': '/opt/hpc/config.json', 'token': 'T' * 40}

    def test_ssh_has_security_options(self):
        b = broker.Broker(self.config())
        self.assertIn('BatchMode=yes', b.argv)
        self.assertIn('StrictHostKeyChecking=yes', b.argv)
        self.assertIn('ForwardAgent=no', b.argv)
        self.assertIn('ClearAllForwardings=yes', b.argv)
        self.assertIn('--', b.argv)

    def test_config_injection_rejected(self):
        for key, value in [('host_alias', '-oProxyCommand=bad'), ('host_alias', 'x;whoami'), ('remote_gate', '/tmp/x;id'), ('remote_gate', '/opt/../secret.py'), ('ssh_executable', 'relative-executable')]:
            config = self.config()
            config[key] = value
            with self.assertRaises(ValueError):
                broker.Broker(config)

    def test_config_initializer_never_overwrites(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = str(Path(parent) / 'private')
            with patch('sys.stdout', new_callable=io.StringIO):
                broker.initialize(directory)
                config = json.loads((Path(directory) / 'broker.private.json').read_text())
                client = json.loads((Path(directory) / 'client.local.json').read_text())
                self.assertGreaterEqual(len(config['token']), 32)
                self.assertEqual(config['token'], client['token'])
                with self.assertRaises(ValueError):
                    broker.initialize(directory)
