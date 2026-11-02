"""Create a fresh private configuration directory before generating any token."""
import os
from pathlib import Path

def create_private_directory(path):
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ValueError("Private directory must be new")
    if os.name != "nt":
        os.mkdir(str(path), 0o700)  # Exclusive creation; never change an existing folder.
        return path
    import ctypes as c
    from ctypes import wintypes as w
    k = c.WinDLL("kernel32", use_last_error=True)
    a = c.WinDLL("advapi32", use_last_error=True)
    k.GetCurrentProcess.argtypes = []
    k.GetCurrentProcess.restype = w.HANDLE
    k.LocalFree.argtypes = [c.c_void_p]
    k.LocalFree.restype = c.c_void_p
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    a.OpenProcessToken.restype = w.BOOL
    a.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    a.GetTokenInformation.restype = w.BOOL
    a.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    a.ConvertSidToStringSidW.restype = w.BOOL
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD)]
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    class TokenUser(c.Structure):
        _fields_ = [("sid", c.c_void_p), ("attributes", w.DWORD)]
    class Attributes(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]
    k.CreateDirectoryW.argtypes = [w.LPCWSTR, c.POINTER(Attributes)]
    k.CreateDirectoryW.restype = w.BOOL
    token = w.HANDLE()
    sid_text = w.LPWSTR()
    descriptor = c.c_void_p()
    try:
        if not a.OpenProcessToken(k.GetCurrentProcess(), 8, c.byref(token)):
            raise OSError("Cannot establish private storage")
        needed = w.DWORD()
        a.GetTokenInformation(token, 1, None, 0, c.byref(needed))
        if not needed.value:
            raise OSError("Cannot establish private storage")
        buffer = c.create_string_buffer(needed.value)
        if not a.GetTokenInformation(token, 1, buffer, needed, c.byref(needed)):
            raise OSError("Cannot establish private storage")
        user = c.cast(buffer, c.POINTER(TokenUser)).contents
        if not a.ConvertSidToStringSidW(user.sid, c.byref(sid_text)):
            raise OSError("Cannot establish private storage")
        sid = c.wstring_at(sid_text)
        # Protected DACL, inherited by child files. No grant to Users/Everyone.
        sddl = "D:P(A;OICI;FA;;;" + sid + ")(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, c.byref(descriptor), None):
            raise OSError("Cannot establish private storage")
        attributes = Attributes(c.sizeof(Attributes), descriptor, False)
        if not k.CreateDirectoryW(str(path), c.byref(attributes)):
            raise OSError("Private directory must be new and support access controls")
    finally:
        if descriptor.value:
            k.LocalFree(descriptor)
        if sid_text:
            k.LocalFree(c.cast(sid_text, c.c_void_p))
        if token:
            k.CloseHandle(token)
    return path
