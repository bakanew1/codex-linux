"""Strict workspace protocol. Paths are relative to a single approved root."""
import base64

VERSION = "0.3.1"
MAX_BODY = 128*1024
MAX_RESULT = 4*1024*1024
MAX_FILE_BYTES = 8192
ERRORS = {"INVALID_REQUEST", "CONNECTION_FAILED", "TIMEOUT", "OUTPUT_LIMIT",
          "REMOTE_REJECTED", "UNAVAILABLE", "RATE_LIMIT", "BUSY",
          "NOT_AUTHORIZED", "NOT_FOUND", "ALREADY_EXISTS", "UNSAFE_PATH", "FILE_CHANGED"}
class Rejected(Exception):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in ERRORS else "UNAVAILABLE"

def relative_path(value, allow_root=True):
    try:
        encoded_length = len(value.encode("utf-8")) if isinstance(value, str) else 513
    except UnicodeError:
        raise Rejected("INVALID_REQUEST")
    if (not isinstance(value, str) or encoded_length > 512
            or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise Rejected("INVALID_REQUEST")
    if value == "." and allow_root:
        return value
    parts = value.split("/")
    if (not value or len(parts) > 16
            or any(p in ("", ".", "..") or len(p.encode("utf-8")) > 255 for p in parts)):
        raise Rejected("INVALID_REQUEST")
    return value

def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise Rejected("INVALID_REQUEST")
    return value

def request(value):
    if type(value) is not dict or set(value) != {"operation", "arguments"}:
        raise Rejected("INVALID_REQUEST")
    op, a = value["operation"], value["arguments"]
    if type(op) is not str or type(a) is not dict:
        raise Rejected("INVALID_REQUEST")
    if op == "list_directory" and set(a) == {"path", "offset", "limit"}:
        return op, {"path": relative_path(a["path"]), "offset": integer(a["offset"], 0, 4900),
                    "limit": integer(a["limit"], 1, 100)}
    if op == "search_files" and set(a) == {"path", "pattern", "max_depth", "limit"}:
        p = a["pattern"]
        if (not isinstance(p, str) or not 1 <= len(p) <= 128
                or any(c in p for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in p)):
            raise Rejected("INVALID_REQUEST")
        return op, {"path": relative_path(a["path"]), "pattern": p,
                    "max_depth": integer(a["max_depth"], 0, 8), "limit": integer(a["limit"], 1, 100)}
    if op == "read_file":
        from bulk_read import validate
        return op,validate(a)
    if op == "create_file" and set(a) == {"path", "content"}:
        try:
            size = len(a["content"].encode("utf-8")) if isinstance(a["content"], str) else MAX_FILE_BYTES+1
        except UnicodeError:
            raise Rejected("INVALID_REQUEST")
        if size > MAX_FILE_BYTES:
            raise Rejected("INVALID_REQUEST")
        return op, {"path": relative_path(a["path"], False), "content": a["content"]}
    raise Rejected("INVALID_REQUEST")

def project(op, args, value):
    if type(value) is not dict:
        raise Rejected("REMOTE_REJECTED")
    if op=="read_file" and "files" in args:
        from bulk_read import project_batch
        return project_batch(args,value)
    try:
        if op in ("list_directory", "search_files"):
            keys = {"entries", "truncated"} | ({"next_offset"} if op == "list_directory" else set())
            if set(value) != keys or type(value["entries"]) is not list or len(value["entries"]) > args["limit"]:
                raise ValueError()
            if type(value["truncated"]) is not bool:
                raise ValueError()
            for row in value["entries"]:
                if type(row) is not dict or set(row) != {"path", "type"} or row["type"] not in ("file", "directory"):
                    raise ValueError()
                relative_path(row["path"], False)
                if args["path"] != "." and not row["path"].startswith(args["path"] + "/"):
                    raise ValueError()
            if op == "list_directory":
                n = value["next_offset"]
                if n is not None and (type(n) is not int or n != args["offset"] + len(value["entries"]) or n > 5000):
                    raise ValueError()
                if value["truncated"] != (n is not None):
                    raise ValueError()
            return value
        if op == "read_file" and set(value) == {"path", "offset", "size", "data_base64", "text_utf8", "eof"}:
            if type(value["size"]) is not int or value["size"] < 0 or type(value["eof"]) is not bool:
                raise ValueError()
            expected=max(0,value["size"]-args["tail"]) if "tail" in args else args["offset"]
            limit=args["tail"] if "tail" in args else args["length"]
            if value["path"] != args["path"] or type(value["offset"]) is not int or value["offset"] != expected:
                raise ValueError()
            if not isinstance(value["data_base64"], str):
                raise ValueError()
            data = base64.b64decode(value["data_base64"], validate=True)
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                text = None
            if value["text_utf8"] != text or (value["text_utf8"] is not None and not isinstance(value["text_utf8"], str)):
                raise ValueError()
            if len(data) > limit or value["eof"] != (expected + len(data) >= value["size"]):
                raise ValueError()
            return value
        if op == "create_file" and set(value) == {"path", "created", "bytes"}:
            if value["path"] == args["path"] and value["created"] is True and type(value["bytes"]) is int and value["bytes"] == len(args["content"].encode("utf-8")):
                return value
    except (ValueError, TypeError, KeyError, Rejected):
        pass
    raise Rejected("REMOTE_REJECTED")

import subprocess
import threading


def _backend_failure_hint(stderr):
    # Classify internally; never print raw output, connection details or credentials.
    text = stderr.lower()
    rules = [
        (b"permission denied", "AUTHENTICATION"),
        (b"agent refused operation", "AUTHENTICATION"),
        (b"host key verification failed", "HOST_KEY"),
        (b"remote host identification has changed", "HOST_KEY"),
        (b"could not resolve hostname", "NAME_RESOLUTION"),
        (b"connection refused", "NETWORK_REFUSED"),
        (b"connection timed out", "NETWORK_TIMEOUT"),
        (b"no route to host", "NETWORK_ROUTE"),
        (b"modulenotfounderror", "REMOTE_MODULE_MISSING"),
        (b"importerror", "REMOTE_MODULE_IMPORT"),
        (b"no such file or directory", "EXECUTABLE_OR_ENTRY_MISSING"),
    ]
    for fragment, label in rules:
        if fragment in text:
            return label
    return "BACKEND_EXIT_OR_IO"

def bounded_run(argv, data, timeout):
    """Budget covers the process and both output pipes; contain descendants."""
    from process_guard import ProcessGroup
    import time
    if not isinstance(data, bytes) or len(data) > MAX_BODY or timeout <= 0:
        raise Rejected("INVALID_REQUEST")
    group = None
    try:
        group = ProcessGroup()
        p = group.spawn(argv)
    except OSError as start_error:
        if group is not None:
            group.close()
        numbers = []
        for field in ("errno", "winerror"):
            value = getattr(start_error, field, None)
            if type(value) is int:
                numbers.append(field + "=" + str(value))
        import sys
        print("HPC_DIAG stage=PROCESS_START " + " ".join(numbers), file=sys.stderr, flush=True)
        raise Rejected("CONNECTION_FAILED") from None
    deadline = time.monotonic() + timeout
    buffers = [bytearray(), bytearray()]
    exceeded = threading.Event()
    read_error = threading.Event()
    def read_pipe(pipe, buffer):
        try:
            while True:
                block = pipe.read1(4096)
                if not block:
                    break
                if len(buffer) + len(block) > MAX_RESULT:
                    exceeded.set()
                    group.terminate()
                    break
                buffer.extend(block)
        except OSError:
            read_error.set()
        finally:
            pipe.close()
    threads = [threading.Thread(target=read_pipe, args=(pipe, buffers[i]), daemon=True)
               for i, pipe in enumerate((p.stdout, p.stderr))]
    for thread in threads:
        thread.start()
    write_failed = threading.Event()
    def write_input():
        try:
            p.stdin.write(data)
            p.stdin.close()
        except OSError:
            write_failed.set()
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass
    writer = threading.Thread(target=write_input, daemon=True)
    writer.start()
    timed_out = False
    write_error = False
    try:
        p.wait(timeout=max(0, deadline-time.monotonic()))
        writer.join(timeout=max(0, deadline-time.monotonic()))
        for thread in threads:
            thread.join(timeout=max(0, deadline-time.monotonic()))
        if writer.is_alive() or any(thread.is_alive() for thread in threads):
            timed_out = True
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError:
        write_error = True
    finally:
        group.close()
        writer.join(timeout=1)
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=1)
    if exceeded.is_set():
        raise Rejected("OUTPUT_LIMIT")
    if timed_out or writer.is_alive() or any(thread.is_alive() for thread in threads):
        raise Rejected("TIMEOUT")
    if write_error or write_failed.is_set() or read_error.is_set() or p.returncode:
        import sys
        print("HPC_DIAG stage=BACKEND_PROCESS code=" + _backend_failure_hint(bytes(buffers[1])),
              file=sys.stderr, flush=True)
        raise Rejected("CONNECTION_FAILED")
    return bytes(buffers[0])
