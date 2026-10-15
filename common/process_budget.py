"""Bound an owned subprocess tree, including on Windows parent termination."""
import os
import signal
import subprocess
import time


class WindowsJob:
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            import ctypes
            from ctypes import wintypes

            class Accounting(ctypes.Structure):
                _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                    (name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]

            # Wait for owned descendants as well as the main child before
            # releasing logs/returning to another scheduled invocation.
            self.kernel.TerminateJobObject(self.handle, 124)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                accounting = Accounting()
                if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                            ctypes.sizeof(accounting), None) or not accounting.active:
                    break
                time.sleep(0.01)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_bounded(command, *, cwd, stdout, stderr, timeout, env=None):
    job = WindowsJob()
    process = None
    assigned = False
    try:
        with open(stdout, "ab", buffering=0) as out, open(stderr, "ab", buffering=0) as err:
            process = subprocess.Popen(command, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                env=env, start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            job.assign(process)
            assigned = os.name == "nt"
            try:
                return {"exit_code": process.wait(timeout=timeout), "timed_out": False}
            except subprocess.TimeoutExpired:
                return {"exit_code": 124, "timed_out": True}
    finally:
        # Closing the job also removes leftover owned browser descendants after
        # a normal exit; never enumerate/kill unrelated browser processes.
        job.close()
        if process is not None:
            if assigned:
                # Job closure requests termination asynchronously. Give Windows
                # time to signal the process handle before another TerminateProcess.
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait()
