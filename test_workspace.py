import base64
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch, Mock
import broker
import client_mcp
import common
import remote_gate

LIST = {"path": ".", "offset": 0, "limit": 20}
EMPTY = {"entries": [], "truncated": False, "next_offset": None}
def req(op, a):
    return {"operation": op, "arguments": a}

class Protocol(unittest.TestCase):
    def test_only_four_operations(self):
        self.assertEqual({t["name"] for t in client_mcp.TOOLS},
            {"list_directory", "search_files", "read_file", "create_file"})
        for op in ("delete_file", "submit_job", "run_shell", "mkdir", "append_file", "check_jobs"):
            with self.subTest(op=op), self.assertRaises(common.Rejected):
                common.request(req(op, {}))

    def test_write_annotation(self):
        for t in client_mcp.TOOLS:
            self.assertEqual(t["annotations"]["readOnlyHint"], t["name"] != "create_file")
            self.assertEqual(t["annotations"]["idempotentHint"], t["name"] != "create_file")

    def test_paths(self):
        for path in ("../a", "/etc/passwd", "a/../b", "a//b", "a\\b", "", "./a",
                     "a/.", "a\nx", "a\0b", "a/"*17+"b", "x"*513):
            with self.subTest(path=path), self.assertRaises(common.Rejected):
                common.request(req("read_file", {"path":path,"offset":0,"length":10}))
        self.assertEqual(common.relative_path("计算/Li/OUTCAR"), "计算/Li/OUTCAR")
        with self.assertRaises(common.Rejected):
            common.relative_path("\ud800")
        with self.assertRaises(common.Rejected):
            common.request(req("create_file", {"path":"a","content":"\ud800"}))
        with self.assertRaises(common.Rejected):
            common.relative_path(".", False)

    def test_extra_fields_and_bad_types(self):
        for a in (dict(LIST, command="id"), dict(LIST, limit=True), dict(LIST, offset=-1),
                  dict(LIST, limit=101), dict(LIST, path=[])):
            with self.assertRaises(common.Rejected):
                common.request(req("list_directory",a))
        for a in ({"path":"a","content":123},{"path":"a","content":"中"*3000},
                  {"path":"a","content":"","overwrite":True}):
            with self.assertRaises(common.Rejected):
                common.request(req("create_file",a))
        for p in ("../OUTCAR", "/etc/*", "a\\b", ""):
            with self.assertRaises(common.Rejected):
                common.request(req("search_files",{"path":".","pattern":p,"max_depth":2,"limit":10}))

    def test_reproject_rejects_extra_or_outside_data(self):
        for value in (dict(EMPTY, host="secret"),
                      dict(EMPTY, entries=[{"path":"../secret","type":"file"}]),
                      dict(EMPTY, entries=[{"path":"a","type":"symlink"}]),
                      dict(EMPTY, truncated=True),
                      dict(EMPTY, next_offset=True)):
            with self.assertRaises(common.Rejected):
                common.project("list_directory", LIST, value)
        with self.assertRaises(common.Rejected):
            common.project("search_files", {"path":"Li","limit":10},
                           {"entries":[{"path":"Na/a","type":"file"}],"truncated":False})

    def test_read_schema_and_bad_base64(self):
        a={"path":"OUTCAR","offset":0,"length":10}
        value={"path":"OUTCAR","offset":0,"size":3,"data_base64":"YWJj","text_utf8":"abc","eof":True}
        self.assertEqual(common.project("read_file",a,value),value)
        for change in ({"data_base64":"!!"},{"text_utf8":"other"},{"offset":True},{"size":False},
                       {"eof":False},{"data_base64":"YQ=="*30},{"host":"secret"}):
            with self.assertRaises(common.Rejected):
                common.project("read_file",a,dict(value,**change))

    def test_create_receipt(self):
        a={"path":"INCAR","content":"中文\n"}
        good={"path":"INCAR","created":True,"bytes":7}
        self.assertEqual(common.project("create_file",a,good),good)
        for change in ({"bytes":True},{"bytes":6},{"created":"yes"},{"path":"other"}):
            with self.assertRaises(common.Rejected):
                common.project("create_file",a,dict(good,**change))

    def test_unsupported_platform_fails_closed(self):
        with patch("remote_gate.sys.platform","win32"):
            with self.assertRaises(common.Rejected) as error:
                remote_gate.handle(req("list_directory",LIST),{"root":"/approved"})
        self.assertEqual(error.exception.code,"UNSAFE_PATH")

    def test_remote_config_exact(self):
        for cfg in ({}, {"root":"/","scheduler":"slurm"}, []):
            with self.assertRaises(common.Rejected):
                remote_gate.handle(req("list_directory",LIST),cfg)

    def test_blocked_stdin_has_deadline(self):
        started=time.monotonic()
        with self.assertRaises(common.Rejected) as error:
            common.bounded_run([sys.executable,"-c","import time;time.sleep(10)"],
                               b"x"*common.MAX_BODY,.3)
        self.assertEqual(error.exception.code,"TIMEOUT")
        self.assertLess(time.monotonic()-started,2)

    def test_large_input_roundtrip(self):
        data=b"x"*60000
        self.assertEqual(common.bounded_run([sys.executable,"-c",
          "import sys;d=sys.stdin.buffer.read();print(len(d))"],data,5).strip(),b"60000")

    def test_windows_remote_cli_errors_are_public(self):
        if sys.platform=="linux":
            self.skipTest("Windows fail-closed entrypoint")
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"remote.json"
            p.write_text('{"root":"/approved"}',encoding="utf-8")
            result=subprocess.run([sys.executable,"remote_gate.py","--config",str(p)],
                input=json.dumps(req("list_directory",LIST)),text=True,capture_output=True,timeout=5)
            self.assertEqual(json.loads(result.stdout),{"error":"UNSAFE_PATH"})
            self.assertEqual(result.stderr,"")

class HTTPIntegration(unittest.TestCase):
    def setUp(self):
        class Fake:
            token="T"*40
            def query(self, v):
                op,a=common.request(v)
                if op=="create_file":
                    return {"path":a["path"],"created":True,"bytes":len(a["content"].encode("utf-8"))}
                return EMPTY
        self.backend=Fake()
        self.server=broker.make_server(self.backend,0)
        self.runner=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.runner.start()
        self.cfg={"url":"http://127.0.0.1:%d/v1/query"%self.server.server_port,"token":"T"*40}
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runner.join()

    def test_read_and_write_transport(self):
        self.assertEqual(client_mcp.call(self.cfg,"list_directory",LIST),EMPTY)
        a={"path":"INCAR","content":"中"*2700}
        self.assertEqual(client_mcp.call(self.cfg,"create_file",a)["bytes"],8100)

    def test_auth(self):
        with self.assertRaises(common.Rejected) as e:
            client_mcp.call(dict(self.cfg,token="X"*40),"list_directory",LIST)
        self.assertEqual(e.exception.code,"NOT_AUTHORIZED")

    def test_http_error_not_success(self):
        err=urllib.error.HTTPError(self.cfg["url"],500,"secret",{},
                                  io.BytesIO(json.dumps({"ok":True,"data":EMPTY}).encode()))
        opener=Mock()
        opener.open.side_effect=err
        with patch("client_mcp.urllib.request.build_opener",return_value=opener):
            with self.assertRaises(common.Rejected) as e:
                client_mcp.call(self.cfg,"list_directory",LIST)
        self.assertEqual(e.exception.code,"REMOTE_REJECTED")

    def test_origin_and_oversized_body(self):
        import urllib.request
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.assertEqual(client_mcp.call(self.cfg,"list_directory",LIST),EMPTY)
        with patch.object(self.backend,"query",wraps=self.backend.query) as query:
            for origin,body in (("https://untrusted.invalid",b"{}"),(None,b"x"*(common.MAX_BODY+1))):
                headers={"Authorization":"Bearer "+self.cfg["token"],"Content-Type":"application/json"}
                if origin:headers["Origin"]=origin
                try:
                    response=opener.open(urllib.request.Request(self.cfg["url"],data=body,headers=headers),timeout=5)
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code,403 if origin else 400);exc.close()
                except urllib.error.URLError as exc:
                    self.assertIsInstance(exc.reason,ConnectionError)
                except ConnectionError:pass
                else:
                    response.close();self.fail("Rejected request unexpectedly succeeded")
                query.assert_not_called()
        self.assertEqual(client_mcp.call(self.cfg,"list_directory",LIST),EMPTY)

    def test_real_stdio_mcp(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"client.json"
            p.write_text(json.dumps(self.cfg),encoding="utf-8")
            msgs=[
                {"jsonrpc":"2.0","id":1,"method":"initialize","params":{
                    "protocolVersion":"2025-06-18","capabilities":{},
                    "clientInfo":{"name":"test","version":"1"}}},
                {"jsonrpc":"2.0","method":"notifications/initialized"},
                {"jsonrpc":"2.0","id":2,"method":"tools/list"},
                {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
                    "name":"list_directory","arguments":LIST}},
                {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{
                    "name":"create_file","arguments":{"path":"INCAR","content":"ENCUT=520\n"}}},
                {"jsonrpc":"2.0","id":5,"method":"tools/call","params":{
                    "name":"delete_file","arguments":{"path":"INCAR"}}},
            ]
            result=subprocess.run([sys.executable,"client_mcp.py","--config",str(p),"--mcp"],
                input="".join(json.dumps(m)+"\n" for m in msgs),capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            responses=[json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(responses[1]["result"]["tools"]),4)
            self.assertFalse(responses[2]["result"]["isError"])
            self.assertFalse(responses[3]["result"]["isError"])
            self.assertTrue(responses[4]["result"]["isError"])

@unittest.skipUnless(sys.platform=="linux","Requires real Linux dir_fd/O_NOFOLLOW semantics")
class LinuxWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.parent=Path(self.tmp.name)
        self.root=self.parent/"workspace"
        self.root.mkdir()
        (self.root/"Li").mkdir()
        (self.root/"Li/OUTCAR").write_bytes(b"0123456789")
        (self.parent/"secret").write_bytes(b"PRIVATE")
        self.cfg={"root":str(self.root)}
    def tearDown(self):
        self.tmp.cleanup()
    def call(self,op,a):
        return remote_gate.handle(req(op,a),self.cfg)
    def test_list_and_search(self):
        r=self.call("list_directory",LIST)
        self.assertEqual(r["entries"],[{"path":"Li","type":"directory"}])
        r=self.call("search_files",{"path":".","pattern":"OUT*","max_depth":2,"limit":10})
        self.assertEqual(r["entries"],[{"path":"Li/OUTCAR","type":"file"}])
    def test_pagination(self):
        for n in range(3):
            (self.root/str(n)).write_text("")
        r=self.call("list_directory",{"path":".","offset":0,"limit":2})
        self.assertTrue(r["truncated"])
        self.assertEqual(r["next_offset"],2)
    def test_offset_read(self):
        r=self.call("read_file",{"path":"Li/OUTCAR","offset":3,"length":4})
        self.assertEqual(base64.b64decode(r["data_base64"]),b"3456")
        self.assertFalse(r["eof"])
    def test_large_file(self):
        p=self.root/"large"
        with p.open("wb") as f:
            f.seek(100*1024*1024)
            f.write(b"END")
        r=self.call("read_file",{"path":"large","offset":100*1024*1024,"length":8192})
        self.assertEqual(base64.b64decode(r["data_base64"]),b"END")
        self.assertTrue(r["eof"])
    def test_new_file_permissions(self):
        self.call("create_file",{"path":"Li/INCAR","content":"ENCUT=520\n"})
        self.assertEqual((self.root/"Li/INCAR").read_bytes(),b"ENCUT=520\n")
        self.assertEqual((self.root/"Li/INCAR").stat().st_mode&0o777,0o600)
    def test_never_overwrite(self):
        with self.assertRaises(common.Rejected) as e:
            self.call("create_file",{"path":"Li/OUTCAR","content":"replaced"})
        self.assertEqual(e.exception.code,"ALREADY_EXISTS")
        self.assertEqual((self.root/"Li/OUTCAR").read_bytes(),b"0123456789")
    def test_symlink_directory(self):
        (self.root/"escape").symlink_to(self.parent,target_is_directory=True)
        for op,a in (("list_directory",dict(LIST,path="escape")),
                     ("read_file",{"path":"escape/secret","offset":0,"length":10}),
                     ("create_file",{"path":"escape/new","content":"x"})):
            with self.subTest(op=op),self.assertRaises(common.Rejected):
                self.call(op,a)
        self.assertFalse((self.parent/"new").exists())
    def test_symlink_file_and_dangling_link(self):
        for name,target in (("link",self.parent/"secret"),("dangling",self.parent/"missing")):
            (self.root/name).symlink_to(target)
            for op,a in (("read_file",{"path":name,"offset":0,"length":10}),
                         ("create_file",{"path":name,"content":"x"})):
                with self.subTest(name=name,op=op),self.assertRaises(common.Rejected):
                    self.call(op,a)
        self.assertEqual((self.parent/"secret").read_bytes(),b"PRIVATE")
    def test_root_symlink_ancestor(self):
        (self.parent/"alias").symlink_to(self.parent,target_is_directory=True)
        with self.assertRaises(common.Rejected):
            remote_gate.handle(req("list_directory",LIST),{"root":str(self.parent/"alias/workspace")})
    def test_hardlink(self):
        os.link(self.parent/"secret",self.root/"hard")
        with self.assertRaises(common.Rejected):
            self.call("read_file",{"path":"hard","offset":0,"length":10})
        self.assertNotIn("hard",[r["path"] for r in self.call("list_directory",LIST)["entries"]])
    def test_fifo_nonblocking(self):
        os.mkfifo(self.root/"pipe")
        start=time.monotonic()
        with self.assertRaises(common.Rejected):
            self.call("read_file",{"path":"pipe","offset":0,"length":10})
        self.assertLess(time.monotonic()-start,1)
    def test_scan_limit(self):
        with patch("remote_gate.SCAN_LIMIT",1):
            for n in range(2):
                (self.root/str(n)).write_text("")
            with self.assertRaises(common.Rejected) as e:
                self.call("list_directory",LIST)
            self.assertEqual(e.exception.code,"OUTPUT_LIMIT")
            r=self.call("search_files",{"path":".","pattern":"*","max_depth":2,"limit":10})
            self.assertTrue(r["truncated"])
    def test_atomic_concurrent_create(self):
        outcomes=[]
        def worker():
            try:
                self.call("create_file",{"path":"new","content":"complete"})
                outcomes.append("ok")
            except common.Rejected as e:
                outcomes.append(e.code)
        threads=[threading.Thread(target=worker) for _ in range(8)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(outcomes.count("ok"),1)
        self.assertEqual(outcomes.count("ALREADY_EXISTS"),7)
        self.assertEqual((self.root/"new").read_text(),"complete")
    def test_existing_directory_required(self):
        with self.assertRaises(common.Rejected):
            self.call("create_file",{"path":"missing/INCAR","content":"x"})
        self.assertFalse((self.root/"missing").exists())
    def test_binary_and_multibyte_reads(self):
        (self.root/"binary").write_bytes(b"\xff\x00")
        r=self.call("read_file",{"path":"binary","offset":0,"length":10})
        self.assertIsNone(r["text_utf8"])
        self.assertEqual(base64.b64decode(r["data_base64"]),b"\xff\x00")
        (self.root/"chinese").write_bytes("中文".encode("utf-8"))
        r=self.call("read_file",{"path":"chinese","offset":0,"length":3})
        self.assertEqual(r["text_utf8"],"中")
        r=self.call("read_file",{"path":"chinese","offset":1,"length":3})
        self.assertIsNone(r["text_utf8"])

    def test_root_slash_refused(self):
        with self.assertRaises(common.Rejected):
            remote_gate.handle(req("list_directory",LIST),{"root":"/"})

class GateLogic(unittest.TestCase):
    def fake_os(self):
        result=Mock()
        result.O_WRONLY=os.O_WRONLY
        result.O_CREAT=os.O_CREAT
        result.O_EXCL=os.O_EXCL
        result.O_RDONLY=os.O_RDONLY
        result.O_DIRECTORY=0x10000
        result.O_NOFOLLOW=0x20000
        result.O_NONBLOCK=0x800
        return result

    def test_create_uses_exclusive_nofollow_and_0600(self):
        w=remote_gate.Workspace.__new__(remote_gate.Workspace)
        w.parent=Mock(return_value=(7,"INCAR"))
        fake=self.fake_os()
        fake.open.return_value=8
        fake.write.return_value=3
        with patch("remote_gate.os",fake):
            receipt=w.create({"path":"Li/INCAR","content":"abc"})
        flags=fake.open.call_args.args[1]
        self.assertTrue(flags&os.O_EXCL)
        self.assertTrue(flags&os.O_CREAT)
        self.assertTrue(flags&fake.O_NOFOLLOW)
        self.assertFalse(flags&os.O_TRUNC)
        self.assertEqual(fake.open.call_args.args[2],0o600)
        self.assertEqual(fake.open.call_args.kwargs,{"dir_fd":7})
        self.assertEqual(receipt["bytes"],3)
        fake.close.assert_any_call(8)
        fake.close.assert_any_call(7)

    def test_existing_name_never_writes_or_unlinks(self):
        w=remote_gate.Workspace.__new__(remote_gate.Workspace)
        w.parent=Mock(return_value=(7,"INCAR"))
        fake=self.fake_os()
        fake.open.side_effect=FileExistsError()
        with patch("remote_gate.os",fake),self.assertRaises(FileExistsError):
            w.create({"path":"Li/INCAR","content":"abc"})
        fake.write.assert_not_called()
        fake.unlink.assert_not_called()
        fake.close.assert_called_once_with(7)

    def test_cross_device_traversal_closes_both_fds(self):
        from types import SimpleNamespace
        w=remote_gate.Workspace.__new__(remote_gate.Workspace)
        w.fd=5
        w.device=1
        fake=self.fake_os()
        fake.dup.return_value=6
        fake.open.return_value=7
        fake.fstat.return_value=SimpleNamespace(st_dev=2)
        with patch("remote_gate.os",fake),self.assertRaises(common.Rejected) as e:
            w.directory("outside")
        self.assertEqual(e.exception.code,"UNSAFE_PATH")
        fake.close.assert_any_call(7)
        fake.close.assert_any_call(6)

    def test_hardlinked_file_rejected_before_read(self):
        import stat
        from types import SimpleNamespace
        w=remote_gate.Workspace.__new__(remote_gate.Workspace)
        w.device=1
        w.parent=Mock(return_value=(7,"OUTCAR"))
        fake=self.fake_os()
        fake.open.return_value=8
        fake.fstat.return_value=SimpleNamespace(st_mode=stat.S_IFREG,st_nlink=2,st_dev=1)
        with patch("remote_gate.os",fake),self.assertRaises(common.Rejected):
            w.read({"path":"Li/OUTCAR","offset":0,"length":100})
        fake.read.assert_not_called()
        fake.close.assert_any_call(7)
        fake.close.assert_any_call(8)
