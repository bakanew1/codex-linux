"""Own-child process containment. No administrator privileges or external helpers."""
import os
import signal
import subprocess
import threading

class ProcessGroup:
    def __init__(self):
        self.process = None
        self.job = None
        self.lock = threading.RLock()
        if os.name == "nt":
            self._windows_job()

    def _windows_job(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        k = c.WinDLL("kernel32", use_last_error=True)
        self.k = k
        k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.TerminateJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        class Basic(c.Structure):
            _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong),
                        ("flags", w.DWORD), ("minimum_working_set", c.c_size_t),
                        ("maximum_working_set", c.c_size_t), ("active_processes", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class Counters(c.Structure):
            _fields_ = [(n, c.c_ulonglong) for n in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", Counters),
                        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                        ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t)]
        job = k.CreateJobObjectW(None, None)
        if not job:
            raise OSError("Process containment unavailable")
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(job, 9, c.byref(info), c.sizeof(info)):
            k.CloseHandle(job)
            raise OSError("Process containment unavailable")
        self.job = job

    def _resume_primary_thread(self, pid):
        c, w, k = self.c, self.w, self.k
        class Entry(c.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("tid", w.DWORD),
                        ("pid", w.DWORD), ("base_priority", w.LONG),
                        ("delta_priority", w.LONG), ("flags", w.DWORD)]
        k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        k.CreateToolhelp32Snapshot.restype = w.HANDLE
        k.Thread32First.argtypes = [w.HANDLE, c.POINTER(Entry)]
        k.Thread32First.restype = w.BOOL
        k.Thread32Next.argtypes = [w.HANDLE, c.POINTER(Entry)]
        k.Thread32Next.restype = w.BOOL
        k.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenThread.restype = w.HANDLE
        k.ResumeThread.argtypes = [w.HANDLE]
        k.ResumeThread.restype = w.DWORD
        snapshot = k.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot in (None, c.c_void_p(-1).value):
            raise OSError("Cannot enumerate own suspended child")
        try:
            entry = Entry()
            entry.size = c.sizeof(entry)
            found = k.Thread32First(snapshot, c.byref(entry))
            while found:
                if entry.pid == pid:
                    thread = k.OpenThread(0x0002, False, entry.tid)
                    if not thread:
                        raise OSError("Cannot open own suspended child")
                    try:
                        count = k.ResumeThread(thread)
                        if count == 0xFFFFFFFF or count == 0:
                            raise OSError("Cannot resume own suspended child")
                        return
                    finally:
                        k.CloseHandle(thread)
                found = k.Thread32Next(snapshot, c.byref(entry))
            raise OSError("Own suspended child thread missing")
        finally:
            k.CloseHandle(snapshot)

    def spawn(self, argv):
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                  "stderr": subprocess.PIPE, "shell": False}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000004  # CREATE_SUSPENDED
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(argv, **kwargs)
        try:
            if os.name == "nt":
                # CPython exposes the owned process handle. Fail closed if unavailable.
                handle = getattr(self.process, "_handle", None)
                if handle is None or not self.k.AssignProcessToJobObject(self.job, int(handle)):
                    raise OSError("Cannot contain own suspended child")
                self._resume_primary_thread(self.process.pid)
        except Exception:
            self.process.kill()
            self.process.wait()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            self.close()
            raise OSError("Process containment unavailable")
        return self.process

    def terminate(self):
        with self.lock:
            if self.process is None:
                return
            if os.name == "nt":
                if self.job:
                    self.k.TerminateJobObject(self.job, 1)
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def close(self):
        with self.lock:
            self.terminate()
            if self.job:
                self.k.CloseHandle(self.job)
                self.job = None
