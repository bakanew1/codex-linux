"""Read-only extensions within the existing read_file tool."""
import base64,errno,time
from common import Rejected,relative_path,integer
MAX_READ_BYTES=64*1024
MAX_BATCH_ITEMS=50
MAX_BATCH_BYTES=512*1024
ITEM_ERRORS={"NOT_FOUND","UNSAFE_PATH","UNAVAILABLE","FILE_CHANGED"}

def item(a):
 if type(a) is not dict:raise Rejected("INVALID_REQUEST")
 if set(a)=={"path","offset","length"}:
  return {"path":relative_path(a["path"],False),"offset":integer(a["offset"],0,2**63-1),
          "length":integer(a["length"],1,MAX_READ_BYTES)}
 if set(a)=={"path","tail"}:
  return {"path":relative_path(a["path"],False),"tail":integer(a["tail"],1,MAX_READ_BYTES)}
 raise Rejected("INVALID_REQUEST")
def cost(a):
 return sum(cost(x) for x in a["files"]) if "files" in a else a.get("tail",a.get("length",0))
def validate(a):
 if set(a)!={"files"}:return item(a)
 if type(a["files"]) is not list or not 1<=len(a["files"])<=MAX_BATCH_ITEMS:raise Rejected("INVALID_REQUEST")
 result={"files":[item(x) for x in a["files"]]}
 if cost(result)>MAX_BATCH_BYTES:raise Rejected("INVALID_REQUEST")
 return result
def read_many(workspace,a):
 rows=[];total=0;deadline=time.monotonic()+10
 for query in a["files"]:
  if time.monotonic()>deadline:raise Rejected("TIMEOUT")
  try:
   row=workspace.read(query)
   raw=base64.b64decode(row["data_base64"],validate=True)
   total+=len(raw)
   # Text batches avoid transferring a second, base64 copy of the same text.
   if row["text_utf8"] is not None:row=dict(row,data_base64=None)
  except FileNotFoundError:row={"path":query["path"],"error":"NOT_FOUND"}
  except OSError as exc:
   row={"path":query["path"],"error":"UNSAFE_PATH" if exc.errno in (errno.ELOOP,errno.ENOTDIR,errno.EXDEV) else "UNAVAILABLE"}
  except Rejected as exc:
   if exc.code not in ITEM_ERRORS:raise
   row={"path":query["path"],"error":exc.code}
  rows.append(row)
 return {"files":rows,"total_bytes":total}
def project_batch(args,value):
 from common import project
 try:
  if set(value)!={"files","total_bytes"} or type(value["files"]) is not list or len(value["files"])!=len(args["files"]):raise ValueError()
  if type(value["total_bytes"]) is not int:raise ValueError()
  total=0
  for query,row in zip(args["files"],value["files"]):
   if type(row) is not dict or row.get("path")!=query["path"]:raise ValueError()
   if set(row)=={"path","error"}:
    if row["error"] not in ITEM_ERRORS:raise ValueError()
    continue
   if set(row)!={"path","offset","size","data_base64","text_utf8","eof"}:raise ValueError()
   if isinstance(row["text_utf8"],str) and row["data_base64"] is None:
    raw=row["text_utf8"].encode("utf-8")
    checked=dict(row,data_base64=base64.b64encode(raw).decode("ascii"))
   elif row["text_utf8"] is None and isinstance(row["data_base64"],str):
    raw=base64.b64decode(row["data_base64"],validate=True);checked=row
   else:raise ValueError()
   project("read_file",query,checked);total+=len(raw)
  if total!=value["total_bytes"] or total>MAX_BATCH_BYTES:raise ValueError()
  return value
 except (ValueError,KeyError,TypeError,Rejected):raise Rejected("REMOTE_REJECTED")
def schema():
    fields={"path":{"type":"string"},"offset":{"type":"integer","minimum":0},
            "length":{"type":"integer","minimum":1,"maximum":MAX_READ_BYTES},
            "tail":{"type":"integer","minimum":1,"maximum":MAX_READ_BYTES}}
    one={"type":"object","properties":dict(fields),"required":["path"],"additionalProperties":False}
    return {"type":"object","properties":dict(fields,files={"type":"array","items":one,"minItems":1,"maxItems":MAX_BATCH_ITEMS}),
            "additionalProperties":False,
            "description":"Exactly one shape: {path,offset,length}, {path,tail}, or {files:[forward or tail items]}. Server rejects all mixed/extra fields."}
