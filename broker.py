"""Run manually under a separate protected OS identity. Loopback only."""
import argparse
import collections
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from common import MAX_BODY, MAX_RESULT, Rejected, project, request

from common import bounded_run

class Broker:
    def __init__(self, config):
        alias = config.get("host_alias", "")
        gate = config.get("remote_gate", "")
        gate_config = config.get("remote_config", "")
        remote_python = config.get("remote_python", "/usr/bin/python3")
        ssh = config.get("ssh_executable", "")
        token = config.get("token", "")
        if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", alias)
                or not isinstance(token, str) or len(token) < 32
                or not os.path.isabs(ssh) or not Path(ssh).is_file()):
            raise ValueError("Invalid private configuration")
        for path in (gate, gate_config, remote_python):
            if (not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path)
                    or ".." in path.split("/")):
                raise ValueError("Invalid remote installation path")
        self.argv = [ssh, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                     "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
                     "-o", "NumberOfPasswordPrompts=0", "-o", "LogLevel=ERROR",
                     "--", alias, remote_python + " -E -s " + gate + " --config " + gate_config]
        self.token = token
        self.lock = threading.BoundedSemaphore(2)
        self.rate_lock = threading.Lock()
        self.calls = collections.deque()

    def query(self, value):
        op, args = request(value)
        with self.rate_lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] >= 60:
                self.calls.popleft()
            if len(self.calls) >= 20:
                raise Rejected("RATE_LIMIT")
            self.calls.append(now)
        if not self.lock.acquire(blocking=False):
            raise Rejected("BUSY")
        try:
            raw = bounded_run(self.argv, json.dumps(value).encode("utf-8"), 25)
            try:
                parsed = json.loads(raw)
            except (ValueError, UnicodeError):
                raise Rejected("REMOTE_REJECTED")
            if type(parsed) is dict and set(parsed) == {"error"}:
                raise Rejected(parsed["error"] if isinstance(parsed["error"], str) else "")
            return project(op, args, parsed)
        finally:
            self.lock.release()

def make_server(broker, port):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass

        def send_json(self, status, data):
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            expected_host = "127.0.0.1:" + str(self.server.server_port)
            if self.headers.get("Host") != expected_host or "Origin" in self.headers:
                return self.send_json(403, {"ok": False, "error": "NOT_AUTHORIZED"})
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + broker.token).encode("utf-8")):
                return self.send_json(401, {"ok": False, "error": "NOT_AUTHORIZED"})
            if self.path != "/v1/query":
                return self.send_json(404, {"ok": False, "error": "NOT_FOUND"})
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/json":
                return self.send_json(400, {"ok": False, "error": "INVALID_REQUEST"})
            try:
                n = int(self.headers.get("Content-Length", "-1"))
                if not 0 < n <= MAX_BODY:
                    raise Rejected("INVALID_REQUEST")
                raw = self.rfile.read(n)
                if len(raw) != n:
                    raise Rejected("INVALID_REQUEST")
                value = json.loads(raw)
                data = broker.query(value)
                return self.send_json(200, {"ok": True, "data": data})
            except Rejected as exc:
                return self.send_json(400, {"ok": False, "error": exc.code})
            except Exception:
                return self.send_json(400, {"ok": False, "error": "UNAVAILABLE"})

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, *args, **kwargs):
            self.connection_slots = threading.BoundedSemaphore(16)
            super().__init__(*args, **kwargs)

        def process_request(self, connection, address):
            if not self.connection_slots.acquire(blocking=False):
                self.shutdown_request(connection)
                return
            try:
                super().process_request(connection, address)
            except Exception:
                self.connection_slots.release()
                self.shutdown_request(connection)

        def process_request_thread(self, connection, address):
            try:
                super().process_request_thread(connection, address)
            finally:
                self.connection_slots.release()

        def handle_error(self, connection, address):
            # Never dump raw request/header/config details into console logs.
            if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
                print("HTTP_WORKER_ERROR", file=sys.stderr)
    return Server(("127.0.0.1", port), Handler)

def initialize(directory):
    from private_storage import create_private_directory
    # Directory permissions are established before the capability token exists.
    path = create_private_directory(directory)
    names = ("broker.private.json", "client.local.json")
    if any((path / n).exists() for n in names):
        raise ValueError("Refusing to overwrite existing configuration")
    token = secrets.token_urlsafe(32)
    ssh = (str(Path(os.environ.get("SystemRoot", "C:/Windows")) /
               "System32/OpenSSH/ssh.exe") if os.name == "nt" else "/usr/bin/ssh")
    cfg = {"host_alias": "hpc", "ssh_executable": ssh,
           "remote_python": "/usr/bin/python3",
           "remote_gate": "/opt/hpc-workspace/remote_gate.py",
           "remote_config": "/opt/hpc-workspace/remote.private.json",
           "token": token, "port": 8765}
    client = {"url": "http://127.0.0.1:8765/v1/query", "token": token}
    for name, value in zip(names, (cfg, client)):
        # Owner-only permissions apply at creation, not after writing the token.
        fd = os.open(str(path / name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
    print("Configuration created in a fresh protected directory; protect the broker code separately.")
    print("Do not distribute generated configuration files.")

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    init = sub.add_parser("init")
    init.add_argument("--directory", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        if args.mode == "init":
            initialize(args.directory)
            return 0
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        broker = Broker(cfg)
        port = cfg.get("port", 8765)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Invalid port")
        server = make_server(broker, port)
        print("Workspace broker listening on loopback. Ctrl+C to stop.", file=sys.stderr)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Broker startup failed. Check private configuration locally.", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
