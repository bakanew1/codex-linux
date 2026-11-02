"""Linux workspace gate: no deletion, overwrite, shell or job submission."""
import argparse
import base64
import errno
import fnmatch
import json
import os
from pathlib import Path
import stat
import sys
import time
from common import MAX_BODY, MAX_RESULT, Rejected, request, project, relative_path

SCAN_LIMIT = 5000
SCAN_SECONDS = 3

class Workspace:
    def __init__(self, root):
        self.fd = None
        if (sys.platform != "linux" or not hasattr(os, "O_NOFOLLOW")
                or not isinstance(root, str) or not root.startswith("/") or root == "/"):
            raise Rejected("UNSAFE_PATH")
        parts = root.split("/")[1:]
        if any(p in ("", ".", "..") for p in parts):
            raise Rejected("UNSAFE_PATH")
        # Open every component, including configured ancestors, without following links.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            self.device = os.fstat(fd).st_dev
            self.fd, fd = fd, None
        finally:
            if fd is not None:
                os.close(fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def directory(self, path):
        relative_path(path)
        fd = os.dup(self.fd)
        try:
            for part in ([] if path == "." else path.split("/")):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if os.fstat(child).st_dev != self.device:
                        raise Rejected("UNSAFE_PATH")
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
            result, fd = fd, None
            return result
        finally:
            if fd is not None:
                os.close(fd)

    def parent(self, path):
        relative_path(path, False)
        parts = path.split("/")
        return self.directory("/".join(parts[:-1]) or "."), parts[-1]

    def entries(self, fd, prefix, budget):
        result = []
        # scandir(fd) does not resolve paths; metadata checked without following links.
        with os.scandir(fd) as rows:
            for row in rows:
                budget[0] += 1
                if budget[0] > SCAN_LIMIT or time.monotonic() > budget[1]:
                    raise Rejected("OUTPUT_LIMIT")
                name = row.name if prefix == "." else prefix + "/" + row.name
                try:
                    relative_path(name, False)
                    info = row.stat(follow_symlinks=False)
                except (Rejected, FileNotFoundError, UnicodeError):
                    continue
                if info.st_dev != self.device:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    result.append({"path": name, "type": "directory"})
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    result.append({"path": name, "type": "file"})
        return sorted(result, key=lambda r: r["path"])

    def listing(self, a):
        fd = self.directory(a["path"])
        try:
            rows = self.entries(fd, a["path"], [0, time.monotonic()+SCAN_SECONDS])
        finally:
            os.close(fd)
        start = a["offset"]
        end = start + a["limit"]
        chosen = rows[start:end]
        more = end < len(rows)
        return {"entries": chosen, "truncated": more, "next_offset": end if more else None}

    def search(self, a):
        budget = [0, time.monotonic()+SCAN_SECONDS]
        found = []
        truncated = False
        def visit(path, depth):
            nonlocal truncated
            fd = self.directory(path)
            try:
                rows = self.entries(fd, path, budget)
            finally:
                os.close(fd)
            for row in rows:
                if len(found) >= a["limit"]:
                    truncated = True
                    return
                if row["type"] == "file" and fnmatch.fnmatchcase(row["path"].rsplit("/", 1)[-1], a["pattern"]):
                    found.append(row)
                if row["type"] == "directory":
                    if depth < a["max_depth"]:
                        visit(row["path"], depth+1)
                        if truncated:
                            return
                    else:
                        truncated = True
        try:
            visit(a["path"], 0)
        except Rejected as exc:
            if exc.code != "OUTPUT_LIMIT":
                raise
            truncated = True
        return {"entries": found, "truncated": truncated}

    def read(self, a):
        parent, leaf = self.parent(a["path"])
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != self.device:
                raise Rejected("UNSAFE_PATH")
            tail="tail" in a
            offset=max(0,info.st_size-a["tail"]) if tail else a["offset"]
            length=a["tail"] if tail else a["length"]
            os.lseek(fd,offset,os.SEEK_SET)
            data=os.read(fd,length)
            after=os.fstat(fd)
            if tail:
                signature=lambda s:(s.st_dev,s.st_ino,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
                if signature(info)!=signature(after):raise Rejected("FILE_CHANGED")
            size=after.st_size
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                text = None
            return {"path": a["path"], "offset": offset, "size": size,
                    "data_base64": base64.b64encode(data).decode("ascii"), "text_utf8": text,
                    "eof": offset+len(data) >= size}
        finally:
            os.close(fd)

    def create(self, a):
        parent, leaf = self.parent(a["path"])
        try:
            # Kernel atomically refuses every existing name, including links.
            fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                data = a["content"].encode("utf-8")
                view = memoryview(data)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise OSError("short write")
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        return {"path": a["path"], "created": True, "bytes": len(data)}

def handle(value, config):
    op, a = request(value)
    if type(config) is not dict or set(config) != {"root"}:
        raise Rejected("INVALID_REQUEST")
    workspace = None
    try:
        workspace = Workspace(config["root"])
        action = {"list_directory": workspace.listing, "search_files": workspace.search,
                  "read_file": workspace.read, "create_file": workspace.create}[op]
        if op=="read_file" and "files" in a:
            from bulk_read import read_many
            return project(op,a,read_many(workspace,a))
        return project(op, a, action(a))
    except FileExistsError:
        raise Rejected("ALREADY_EXISTS")
    except FileNotFoundError:
        raise Rejected("NOT_FOUND")
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EXDEV):
            raise Rejected("UNSAFE_PATH")
        raise Rejected("UNAVAILABLE")
    finally:
        if workspace is not None:
            workspace.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_BODY+1)
        if len(raw) > MAX_BODY:
            raise Rejected("INVALID_REQUEST")
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        result = handle(json.loads(raw), config)
        body = json.dumps(result, ensure_ascii=True, allow_nan=False).encode("ascii")
        if len(body) > MAX_RESULT:
            raise Rejected("OUTPUT_LIMIT")
    except Rejected as exc:
        body = json.dumps({"error": exc.code}).encode("ascii")
    except Exception:
        body = b'{"error":"UNAVAILABLE"}'
    sys.stdout.buffer.write(body+b"\n")

if __name__ == "__main__":
    main()
