"""Suspend/attach/resume sequence adapted from GGE preview_process.py."""

import ctypes
import os
from ctypes import wintypes

CREATE_SUSPENDED = 0x00000004


class ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
        ("thread_id", wintypes.DWORD), ("owner_id", wintypes.DWORD),
        ("base_priority", wintypes.LONG), ("priority_delta", wintypes.LONG), ("flags", wintypes.DWORD),
    ]


def resume_worker(pid):
    """Windows exposes the PID but subprocess closes its primary-thread handle."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, result, arguments in (
        ("CreateToolhelp32Snapshot", wintypes.HANDLE, [wintypes.DWORD, wintypes.DWORD]),
        ("Thread32First", wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]),
        ("Thread32Next", wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]),
        ("OpenThread", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        ("ResumeThread", wintypes.DWORD, [wintypes.HANDLE]),
        ("CloseHandle", wintypes.BOOL, [wintypes.HANDLE]),
    ):
        function = getattr(kernel, name)
        function.restype = result
        function.argtypes = arguments
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise OSError("Worker threads unavailable")
    try:
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        available = kernel.Thread32First(snapshot, ctypes.byref(entry))
        threads = []
        while available:
            if entry.owner_id == pid:
                threads.append(entry.thread_id)
            entry.size = ctypes.sizeof(entry)
            available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        if len(threads) != 1:
            raise OSError("Unexpected suspended worker")
        thread = kernel.OpenThread(2, False, threads[0])
        if not thread:
            raise OSError("Worker thread unavailable")
        try:
            if kernel.ResumeThread(thread) != 1:
                raise OSError("Worker could not resume")
        finally:
            kernel.CloseHandle(thread)
    finally:
        kernel.CloseHandle(snapshot)


def tool_environment(executable, *, python_worker=False):
    result = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    system = result.get("SystemRoot", result.get("SYSTEMROOT", ""))
    result["PATH"] = os.pathsep.join([str(executable.parent), os.path.join(system, "System32")])
    if python_worker:
        result.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    return result
