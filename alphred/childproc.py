"""TUI 세션에 종속되는 자식 프로세스 관리.

`alphred` TUI 가 백그라운드 `serve`(그리고 serve 가 낳는 hermes 게이트웨이)를
**창 없이** 띄우고, TUI 프로세스가 정상/강제/크래시 어느 식으로 끝나든 자식 트리가
함께 죽도록 한다.

Windows: Job Object(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)에 자식을 편입한다. TUI 프로세스가
끝나면 OS 가 Job 핸들을 닫으며 Job 내 전체 트리(serve + 그 자식 hermes)를 종료한다.
자식이 낳은 손자 프로세스는 기본적으로 같은 Job 을 상속하므로 한 Job 으로 트리가 정리된다.

POSIX: 새 세션(start_new_session)으로 띄우고, 종료 시 프로세스 그룹째 신호를 보낸다.
"""
from __future__ import annotations

import os
import subprocess

CREATE_NO_WINDOW = 0x08000000

_job_handle = None  # Windows: 프로세스 수명 동안 살려두는 kill-on-close Job 핸들


def _ensure_job():
    """프로세스당 1회, kill-on-close Job Object 를 생성해 반환(Windows 전용)."""
    global _job_handle
    if _job_handle is not None:
        return _job_handle
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(wintypes.ULONG)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    _job_handle = job
    return job


def _assign_to_job(proc) -> None:
    """자식 프로세스를 kill-on-close Job 에 편입(Windows). 실패해도 치명적이지 않다."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    kernel32.OpenProcess.restype = wintypes.HANDLE
    h = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel32.AssignProcessToJobObject(_ensure_job(), h):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(h)


def spawn_managed(args, *, log_path, env=None):
    """창 없이 자식 프로세스를 띄우고 TUI 수명에 묶는다. Popen 반환.

    log_path 로 stdout/stderr 를 append 리다이렉트하고 stdin 은 DEVNULL.
    """
    log = open(log_path, "ab")
    kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)
    if os.name == "nt":
        try:
            _assign_to_job(proc)
        except Exception:
            pass  # Job 편입 실패 시에도 명시적 terminate_managed 로 정리되도록 진행
    return proc


def terminate_managed(proc, timeout: float = 8.0) -> None:
    """관리 중인 자식(과 그 트리)을 정리한다. 정상 종료는 graceful, 미종료 시 강제."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(proc.pid), __import__("signal").SIGTERM)
            except Exception:
                proc.terminate()
        else:
            proc.terminate()  # serve 의 finally 가 hermes 를 정리하도록 graceful 우선
        try:
            proc.wait(timeout=timeout)
            return
        except Exception:
            pass
        # 미종료 → 트리 강제 종료
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), __import__("signal").SIGKILL)
            except Exception:
                proc.kill()
    except Exception:
        pass
