import base64,io,json,os,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import common,bulk_read,remote_gate,broker,client_mcp
def req(a):return {"operation":"read_file","arguments":a}
def row(path,raw,offset=0,size=None,compact=False):
 size=len(raw) if size is None else size
 try:text=raw.decode("utf-8")
 except UnicodeError:text=None
 return {"path":path,"offset":offset,"size":size,"data_base64":None if compact and text is not None else base64.b64encode(raw).decode(),
         "text_utf8":text,"eof":offset+len(raw)>=size}
class FakeWorkspace:
 def __init__(self,files):self.files=files
 def read(self,a):
  if a["path"] not in self.files:raise FileNotFoundError()
  raw=self.files[a["path"]];offset=max(0,len(raw)-a["tail"]) if "tail" in a else a["offset"]
  length=a["tail"] if "tail" in a else a["length"]
  return row(a["path"],raw[offset:offset+length],offset,len(raw))
class BatchProtocol(unittest.TestCase):
 def test_only_four_tool_names(self):
  self.assertEqual({x["name"] for x in client_mcp.TOOLS},{"list_directory","search_files","read_file","create_file"})
 def test_single_limit_and_write_limit_separate(self):
  common.request(req({"path":"a","offset":0,"length":65536}))
  with self.assertRaises(common.Rejected):common.request(req({"path":"a","offset":0,"length":65537}))
  with self.assertRaises(common.Rejected):common.request({"operation":"create_file","arguments":{"path":"a","content":"x"*8193}})
 def test_fifty_items_allowed_fifty_one_refused(self):
  query={"path":"a","tail":1024}
  common.request(req({"files":[query]*50}))
  with self.assertRaises(common.Rejected):common.request(req({"files":[query]*51}))
 def test_aggregate_requested_byte_budget(self):
  common.request(req({"files":[{"path":"a","tail":65536}]*8}))
  with self.assertRaises(common.Rejected):common.request(req({"files":[{"path":"a","tail":65536}]*9}))
 def test_bad_paths_types_and_extra_arguments(self):
  for q in ({"path":"../secret","tail":1},{"path":"/secret","tail":1},{"path":"a","tail":True},
            {"path":"a","tail":0},{"path":"a","offset":0,"length":1,"command":"id"},
            {"path":"a","tail":1,"offset":0}):
   with self.assertRaises(common.Rejected):common.request(req({"files":[q]}))
  for value in ([],None,[1]):
   with self.assertRaises(common.Rejected):common.request(req({"files":value}))
 def test_long_unicode_batch_fits_request_envelope(self):
  q={"path":"\u0080"*127+"/"+ "\u0080"*127,"offset":0,"length":1};a={"files":[q]*50}
  common.request(req(a))
  self.assertLess(len(json.dumps(req(a)).encode()),common.MAX_BODY)
 def test_tail_projection(self):
  a={"path":"a","tail":3};v=row("a",b"end",offset=7,size=10)
  self.assertEqual(common.project("read_file",a,v),v)
  with self.assertRaises(common.Rejected):common.project("read_file",a,dict(v,offset=0))
 def test_compact_batch_text_and_binary(self):
  a=common.request(req({"files":[{"path":"a","tail":5},{"path":"b","offset":0,"length":2}]}))[1]
  v=bulk_read.read_many(FakeWorkspace({"a":b"hello","b":b"\xff\x00"}),a)
  self.assertIsNone(v["files"][0]["data_base64"])
  self.assertIsNone(v["files"][1]["text_utf8"])
  self.assertEqual(v["total_bytes"],7)
  common.project("read_file",a,v)
 def test_missing_file_does_not_discard_success(self):
  a={"files":[{"path":"missing","tail":8},{"path":"ok","tail":8}]}
  v=bulk_read.read_many(FakeWorkspace({"ok":b"yes"}),a)
  self.assertEqual(v["files"][0],{"path":"missing","error":"NOT_FOUND"})
  self.assertEqual(v["files"][1]["text_utf8"],"yes")
  common.project("read_file",a,v)
 def test_project_rejects_wrong_order_total_and_error(self):
  a={"files":[{"path":"a","tail":8}]};v=bulk_read.read_many(FakeWorkspace({"a":b"yes"}),a)
  for changed in (dict(v,total_bytes=999),dict(v,files=[{"path":"other","error":"NOT_FOUND"}]),dict(v,files=[{"path":"a","error":"SECRET"}])):
   with self.assertRaises(common.Rejected):common.project("read_file",a,changed)
 def test_duplicate_base64_text_not_accepted_in_batch(self):
  a={"files":[{"path":"a","tail":8}]}
  with self.assertRaises(common.Rejected):common.project("read_file",a,{"files":[row("a",b"yes")],"total_bytes":3})
 def test_utf8_split_binary_fallback_preserves_bytes(self):
  raw="中".encode();a={"files":[{"path":"a","offset":0,"length":2}]}
  v=bulk_read.read_many(FakeWorkspace({"a":raw}),a)
  self.assertEqual(base64.b64decode(v["files"][0]["data_base64"]),raw[:2])
  common.project("read_file",a,v)
 def test_worst_case_control_text_fits_result_budget(self):
  a={"files":[{"path":"a","tail":8192}]*50}
  v=bulk_read.read_many(FakeWorkspace({"a":b"\x00"*8192}),a)
  common.project("read_file",a,v)
  self.assertLess(len(json.dumps(v,ensure_ascii=True).encode()),common.MAX_RESULT)
class BrokerBudgets(unittest.TestCase):
 def setUp(self):
  self.b=broker.Broker({"host_alias":"hpc","ssh_executable":sys.executable,"remote_gate":"/opt/gate.py","remote_config":"/opt/config.json","token":"T"*40})
 def test_twenty_calls_then_limit(self):
  q={"operation":"list_directory","arguments":{"path":".","offset":0,"limit":1}}
  result=b'{"entries":[],"truncated":false,"next_offset":null}'
  with patch("broker.time.monotonic",return_value=100),patch("broker.bounded_run",return_value=result):
   for _ in range(20):self.b.query(q)
   with self.assertRaises(common.Rejected) as e:self.b.query(q)
  self.assertEqual(e.exception.code,"RATE_LIMIT")
 def test_call_budget_window_reset(self):
  q={"operation":"list_directory","arguments":{"path":".","offset":0,"limit":1}}
  result=b'{"entries":[],"truncated":false,"next_offset":null}'
  with patch("broker.time.monotonic",return_value=100),patch("broker.bounded_run",return_value=result):
   for _ in range(20):self.b.query(q)
  with patch("broker.time.monotonic",return_value=161),patch("broker.bounded_run",return_value=result):
   self.b.query(q)
class BulkHTTP(unittest.TestCase):
 def test_real_http_large_and_batch_responses(self):
  class Backend:
   token="T"*40
   def query(self,value):
    _,a=common.request(value);w=FakeWorkspace({"a":b"X"*65536,"b":"中文".encode()})
    return bulk_read.read_many(w,a) if "files" in a else w.read(a)
  server=broker.make_server(Backend(),0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  cfg={"url":"http://127.0.0.1:%d/v1/query"%server.server_port,"token":"T"*40}
  try:
   self.assertEqual(len(client_mcp.call(cfg,"read_file",{"path":"a","tail":65536})["text_utf8"]),65536)
   a={"files":[{"path":"a","tail":65536},{"path":"b","tail":64}]}
   result=client_mcp.call(cfg,"read_file",a);self.assertEqual(result["total_bytes"],65542)
   self.assertIsNone(result["files"][0]["data_base64"])
  finally:server.shutdown();server.server_close();thread.join()
@unittest.skipUnless(sys.platform=="linux","Requires Linux nofollow filesystem semantics")
class LinuxBatch(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def invoke(self,a):return remote_gate.handle(req(a),{"root":str(self.root)})
 def test_read_fifty_files(self):
  for i in range(50):(self.root/str(i)).write_text("energy="+str(i))
  v=self.invoke({"files":[{"path":str(i),"tail":1024} for i in range(50)]})
  self.assertEqual(len(v["files"]),50);self.assertIn("49",v["files"][-1]["text_utf8"])
 def test_large_tail_and_offset(self):
  data=b"A"*70000+b"FINAL";(self.root/"OUTCAR").write_bytes(data)
  self.assertEqual(self.invoke({"path":"OUTCAR","tail":5})["text_utf8"],"FINAL")
  self.assertEqual(len(base64.b64decode(self.invoke({"path":"OUTCAR","offset":0,"length":65536})["data_base64"])),65536)
 def test_missing_and_symlink_are_per_item_errors(self):
  (self.root/"ok").write_text("yes");(self.root/"escape").symlink_to("/etc/passwd")
  v=self.invoke({"files":[{"path":"missing","tail":8},{"path":"escape","tail":8},{"path":"ok","tail":8}]})
  self.assertEqual([x.get("error") for x in v["files"]],["NOT_FOUND","UNSAFE_PATH",None])
 def test_hardlink_and_fifo_refused(self):
  (self.root/"a").write_text("data");os.link(self.root/"a",self.root/"hard");os.mkfifo(self.root/"fifo")
  v=self.invoke({"files":[{"path":"hard","tail":8},{"path":"fifo","tail":8}]})
  self.assertTrue(all(x["error"]=="UNSAFE_PATH" for x in v["files"]))
 def test_empty_file(self):
  (self.root/"empty").write_bytes(b"")
  v=self.invoke({"files":[{"path":"empty","tail":8}]})
  self.assertEqual(v["total_bytes"],0);self.assertTrue(v["files"][0]["eof"])
 def test_tail_change_rejected(self):
  path=self.root/"a";path.write_bytes(b"abcd");w=remote_gate.Workspace(str(self.root));before=path.stat()
  keys=("st_dev","st_ino","st_nlink","st_size","st_mtime_ns","st_ctime_ns")
  after=SimpleNamespace(**{k:getattr(before,k)+(1 if k=="st_size" else 0) for k in keys})
  try:
   with patch("remote_gate.os.fstat",side_effect=[before,after]):
    with self.assertRaises(common.Rejected) as e:w.read({"path":"a","tail":2})
   self.assertEqual(e.exception.code,"FILE_CHANGED")
  finally:w.close()
 def test_traversal_rejects_entire_batch(self):
  (self.root/"ok").write_text("yes")
  with self.assertRaises(common.Rejected):self.invoke({"files":[{"path":"ok","tail":8},{"path":"../bad","tail":8}]})
if __name__=="__main__":unittest.main()
