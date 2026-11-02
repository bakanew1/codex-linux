import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch
import common
import broker
import client_mcp
import remote_gate

class ReviewRegressions(unittest.TestCase):

    def test_mcp_configuration_failure_keeps_stdout_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, 'client_mcp.py', '--config', str(Path(directory) / 'missing.json'), '--mcp'], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('CONFIGURATION', result.stderr)

    def test_timeout_includes_descendant_output_pipe(self):
        child = 'import time; time.sleep(2)'
        parent = "import subprocess,sys;subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);print('partial',flush=True)"
        start = time.monotonic()
        with self.assertRaises(common.Rejected) as error:
            common.bounded_run([sys.executable, '-c', parent], b'', 0.2)
        self.assertEqual(error.exception.code, 'TIMEOUT')
        self.assertLess(time.monotonic() - start, 1.5)

    def test_http_concurrency_is_bounded(self):
        import socket

        class Fake:
            token = 'T' * 40
        server = broker.make_server(Fake(), 0)
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        held = []
        try:
            for _ in range(16):
                connection = socket.create_connection(server.server_address, timeout=2)
                connection.sendall(b'POST /v1/query HTTP/1.1\r\n')
                held.append(connection)
            time.sleep(0.15)
            with socket.create_connection(server.server_address, timeout=1) as extra:
                extra.settimeout(0.7)
                try:
                    data = extra.recv(1)
                except ConnectionResetError:
                    data = b''
                self.assertEqual(data, b'')
        finally:
            for connection in held:
                connection.close()
            server.shutdown()
            server.server_close()
            runner.join(timeout=2)

    def test_mcp_lifecycle_blocks_tools_until_ready(self):
        session = client_mcp.MCPSession()
        tool = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'list_directory', 'arguments': {'path': '.', 'offset': 0, 'limit': 20}}}
        with patch('client_mcp.call', return_value={'reachable': True}) as invoke:
            self.assertIn('error', client_mcp.rpc(tool, {}, session))
            init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}
            self.assertIn('result', client_mcp.rpc(init, {}, session))
            self.assertIn('error', client_mcp.rpc(tool, {}, session))
            invoke.assert_not_called()
            client_mcp.rpc({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, {}, session)
            self.assertFalse(client_mcp.rpc(tool, {}, session)['result']['isError'])
            self.assertIn('error', client_mcp.rpc(init, {}, session))

    def test_private_directory_is_new_and_never_reused(self):
        import private_storage
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((ValueError, OSError)):
                private_storage.create_private_directory(directory)
            path = private_storage.create_private_directory(Path(directory) / 'protected')
            self.assertTrue(path.is_dir())
            with self.assertRaises((ValueError, OSError)):
                private_storage.create_private_directory(path)

    @unittest.skipUnless(os.name == 'posix', 'POSIX creation permissions test')
    def test_posix_private_permissions_exist_before_token_write(self):
        with tempfile.TemporaryDirectory() as parent:
            original = json.dump

            def check_before_write(value, file, **kwargs):
                self.assertEqual(os.fstat(file.fileno()).st_mode & 63, 0)
                return original(value, file, **kwargs)
            old_mask = os.umask(0)
            try:
                with patch('broker.json.dump', side_effect=check_before_write), patch('sys.stdout', new_callable=io.StringIO):
                    broker.initialize(Path(parent) / 'private')
            finally:
                os.umask(old_mask)

    @unittest.skipUnless(os.name == 'nt', 'Windows DACL creation test')
    def test_windows_private_directory_has_protected_nonpublic_dacl(self):
        import ctypes as c
        from ctypes import wintypes as w
        import private_storage
        a = c.WinDLL('advapi32', use_last_error=True)
        k = c.WinDLL('kernel32', use_last_error=True)
        a.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]
        a.GetNamedSecurityInfoW.restype = w.DWORD
        a.GetSecurityDescriptorControl.argtypes = [c.c_void_p, c.POINTER(w.WORD), c.POINTER(w.DWORD)]
        a.GetSecurityDescriptorControl.restype = w.BOOL
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.c_void_p]
        a.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = w.BOOL
        k.LocalFree.argtypes = [c.c_void_p]
        k.LocalFree.restype = c.c_void_p
        with tempfile.TemporaryDirectory() as parent:
            path = private_storage.create_private_directory(Path(parent) / 'private')
            descriptor = c.c_void_p()
            text = w.LPWSTR()
            try:
                self.assertEqual(a.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, c.byref(descriptor)), 0)
                control, revision = (w.WORD(), w.DWORD())
                self.assertTrue(a.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)))
                self.assertTrue(control.value & 4096)
                self.assertTrue(a.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, c.byref(text), None))
                sddl = c.wstring_at(text)
                for broad_group in ('WD', 'AU', 'BU'):
                    self.assertNotIn(';;;' + broad_group + ')', sddl)
            finally:
                if text:
                    k.LocalFree(c.cast(text, c.c_void_p))
                if descriptor:
                    k.LocalFree(descriptor)

    @unittest.skipUnless(os.name == 'nt', 'Windows suspended-child containment test')
    def test_job_assignment_failure_never_runs_child(self):
        import process_guard
        with tempfile.TemporaryDirectory() as directory:
            flag = Path(directory) / 'should-not-exist'
            group = process_guard.ProcessGroup()
            try:
                code = 'from pathlib import Path;Path(' + repr(str(flag)) + ").write_text('executed')"
                with patch.object(group.k, 'AssignProcessToJobObject', return_value=0):
                    with self.assertRaises(OSError):
                        group.spawn([sys.executable, '-c', code])
                self.assertFalse(flag.exists())
                self.assertTrue(all((stream.closed for stream in (group.process.stdin, group.process.stdout, group.process.stderr))))
            finally:
                group.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows descendant termination test')
    def test_timeout_terminates_descendant_not_only_parent(self):
        import ctypes as c
        from ctypes import wintypes as w
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'child.pid'
            child = 'import os,time;from pathlib import Path;Path(' + repr(str(pid_file)) + ').write_text(str(os.getpid()));time.sleep(10)'
            parent = "import subprocess,sys;subprocess.Popen([sys.executable,'-c'," + repr(child) + '])'
            with self.assertRaises(common.Rejected) as error:
                common.bounded_run([sys.executable, '-c', parent], b'', 0.8)
            self.assertEqual(error.exception.code, 'TIMEOUT')
            self.assertTrue(pid_file.exists(), 'Test child did not start')
            pid = int(pid_file.read_text())
            k = c.WinDLL('kernel32', use_last_error=True)
            k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            k.OpenProcess.restype = w.HANDLE
            k.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
            k.GetExitCodeProcess.restype = w.BOOL
            k.CloseHandle.argtypes = [w.HANDLE]
            handle = k.OpenProcess(4096, False, pid)
            if handle:
                try:
                    status = w.DWORD()
                    self.assertTrue(k.GetExitCodeProcess(handle, c.byref(status)))
                    self.assertNotEqual(status.value, 259)
                finally:
                    k.CloseHandle(handle)
