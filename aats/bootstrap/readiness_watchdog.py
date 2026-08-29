"""Minimal out-of-process hard-deadline watchdog for runtime ownership.

This module intentionally imports only the Python standard library.  It is
started with ``python -m`` so the watchdog does not duplicate the AATS runtime's
large import graph.  The child opens a stable OS handle to its parent before it
announces READY, then owns the hard deadline independently of the parent's GIL
and asyncio loop.
"""
from __future__ import annotations

import argparse
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Protocol


_PROTOCOL = "AATS_RDW_V1"
_MAX_FRAME_BYTES = 512
_DEADLINE_POLL_SECONDS = 0.01


def _lease_clock_ns() -> int:
    if os.name == "nt":
        import ctypes
        get_tick_count_64 = ctypes.windll.kernel32.GetTickCount64
        get_tick_count_64.argtypes = ()
        get_tick_count_64.restype = ctypes.c_ulonglong
        return int(get_tick_count_64()) * 1_000_000
    clock_boottime = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_boottime is None:
        raise RuntimeError("CLOCK_BOOTTIME is required for readiness watchdog")
    return time.clock_gettime_ns(clock_boottime)


class _ParentGuard(Protocol):
    def is_alive(self) -> bool: ...

    def terminate(self, exit_code: int) -> None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _PidFdParentGuard:
    pidfd: int

    def is_alive(self) -> bool:
        import select

        poller = select.poll()
        poller.register(self.pidfd, select.POLLIN)
        return not bool(poller.poll(0))

    def terminate(self, exit_code: int) -> None:
        del exit_code
        signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)

    def close(self) -> None:
        os.close(self.pidfd)


class _WindowsParentGuard:
    def __init__(self, pid: int, *, expected_creation_token: int) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.WaitForSingleObject.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
        )
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.TerminateProcess.argtypes = (
            wintypes.HANDLE,
            wintypes.UINT,
        )
        self._kernel32.TerminateProcess.restype = wintypes.BOOL
        self._kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        self._kernel32.GetProcessTimes.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        process_terminate = 0x0001
        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        self._wait_object_0 = 0x00000000
        self._wait_timeout = 0x00000102
        self._wait_failed = 0xFFFFFFFF
        self._handle = self._kernel32.OpenProcess(
            process_terminate | process_query_limited_information | synchronize,
            False,
            int(pid),
        )
        if not self._handle:
            error = ctypes.get_last_error()
            raise OSError(error, "OpenProcess failed for watchdog parent")
        if self._creation_token() != int(expected_creation_token):
            self.close()
            raise RuntimeError("watchdog parent identity mismatch")

    def _creation_token(self) -> int:
        from ctypes import wintypes

        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not self._kernel32.GetProcessTimes(
            self._handle,
            self._ctypes.byref(creation),
            self._ctypes.byref(exit_time),
            self._ctypes.byref(kernel),
            self._ctypes.byref(user),
        ):
            error = self._ctypes.get_last_error()
            raise OSError(error, "GetProcessTimes failed for watchdog parent")
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)

    def is_alive(self) -> bool:
        wait_result = self._kernel32.WaitForSingleObject(self._handle, 0)
        if wait_result == self._wait_timeout:
            return True
        if wait_result == self._wait_object_0:
            return False
        # WAIT_FAILED/unknown is not evidence that the protected parent has
        # exited. Fail closed in the watchdog process itself; merely raising
        # would leave a GIL-starved parent alive after the child exits.
        error = self._ctypes.get_last_error()
        if not self._kernel32.TerminateProcess(self._handle, 1):
            terminate_error = self._ctypes.get_last_error()
            raise OSError(
                terminate_error or error,
                "WaitForSingleObject failed and watchdog could not terminate parent",
            )
        return False

    def terminate(self, exit_code: int) -> None:
        if not self._kernel32.TerminateProcess(self._handle, int(exit_code)):
            error = self._ctypes.get_last_error()
            if self.is_alive():
                raise OSError(error, "TerminateProcess failed for watchdog parent")

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def windows_process_creation_token(pid: int) -> int:
    """Return the stable Windows creation FILETIME for PID identity fencing."""

    if os.name != "nt":
        raise RuntimeError("Windows process identity is only available on Windows")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, "OpenProcess failed for process identity")
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            error = ctypes.get_last_error()
            raise OSError(error, "GetProcessTimes failed for process identity")
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def _open_parent_guard(
    parent_pid: int,
    *,
    inherited_pidfd: int | None = None,
    expected_creation_token: int | None = None,
) -> _ParentGuard:
    if os.name == "nt":
        if expected_creation_token is None:
            raise RuntimeError("Windows watchdog parent identity is required")
        return _WindowsParentGuard(
            parent_pid,
            expected_creation_token=expected_creation_token,
        )
    if inherited_pidfd is not None:
        if not hasattr(signal, "pidfd_send_signal"):
            os.close(inherited_pidfd)
            raise RuntimeError("pidfd readiness watchdog support is required")
        return _PidFdParentGuard(inherited_pidfd)
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("pidfd readiness watchdog support is required")
    return _PidFdParentGuard(os.pidfd_open(int(parent_pid), 0))


def _write_frame(*parts: object) -> None:
    frame = " ".join(str(part) for part in parts).encode("ascii") + b"\n"
    if len(frame) > _MAX_FRAME_BYTES:
        raise ValueError("watchdog frame exceeds fixed limit")
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


def _read_commands(
    target: queue.Queue[tuple[bytes, int] | None],
) -> None:
    pending = b""
    descriptor = sys.stdin.fileno()
    while True:
        try:
            chunk = os.read(descriptor, _MAX_FRAME_BYTES)
        except OSError:
            target.put(None)
            return
        if not chunk:
            target.put(None)
            return
        pending += chunk
        if len(pending) > _MAX_FRAME_BYTES and b"\n" not in pending:
            target.put((b"", _lease_clock_ns()))
            return
        while b"\n" in pending:
            frame, pending = pending.split(b"\n", 1)
            if len(frame) + 1 > _MAX_FRAME_BYTES:
                target.put((b"", _lease_clock_ns()))
                return
            target.put((frame.rstrip(b"\r"), _lease_clock_ns()))


def _parse_command(
    frame: bytes,
    *,
    nonce: str,
    previous_sequence: int,
) -> tuple[int, str, int | None]:
    try:
        fields = frame.decode("ascii").split(" ")
    except UnicodeDecodeError as exc:
        raise ValueError("non-ASCII watchdog frame") from exc
    if len(fields) not in {4, 5}:
        raise ValueError("malformed watchdog frame")
    protocol, supplied_nonce, raw_sequence, opcode, *deadline_field = fields
    if protocol != _PROTOCOL or supplied_nonce != nonce:
        raise ValueError("watchdog protocol identity mismatch")
    sequence = int(raw_sequence)
    if sequence != previous_sequence + 1:
        raise ValueError("watchdog command sequence mismatch")
    if opcode == "DISARM":
        if deadline_field:
            raise ValueError("DISARM must not include a deadline")
        return sequence, opcode, None
    if opcode not in {"REARM", "FATAL", "SHUTDOWN"} or len(deadline_field) != 1:
        raise ValueError("unsupported watchdog opcode")
    deadline_ns = int(deadline_field[0])
    if deadline_ns <= 0:
        raise ValueError("watchdog deadline must be positive")
    return sequence, opcode, deadline_ns


def _run(
    *,
    parent_pid: int,
    initial_deadline_ns: int,
    nonce: str,
    parent_pidfd: int | None = None,
    parent_creation_token: int | None = None,
) -> int:
    guard = _open_parent_guard(
        parent_pid,
        inherited_pidfd=parent_pidfd,
        expected_creation_token=parent_creation_token,
    )
    commands: queue.Queue[tuple[bytes, int] | None] = queue.Queue()
    reader = threading.Thread(
        target=_read_commands,
        args=(commands,),
        name="aats-readiness-watchdog-control-reader",
        daemon=True,
    )
    sequence = 0
    fatal = False
    shutdown = False
    deadline_ns = int(initial_deadline_ns)
    try:
        reader.start()
        _write_frame("READY", nonce, _lease_clock_ns())
        while True:
            remaining_seconds = (deadline_ns - _lease_clock_ns()) / 1_000_000_000
            if remaining_seconds <= 0.0:
                if guard.is_alive():
                    guard.terminate(1)
                return 1
            try:
                received = commands.get(
                    timeout=min(remaining_seconds, _DEADLINE_POLL_SECONDS)
                )
            except queue.Empty:
                if _lease_clock_ns() >= deadline_ns:
                    if guard.is_alive():
                        guard.terminate(1)
                    return 1
                continue
            if received is None:
                # Broken supervision channel while the parent still exists is
                # fail-closed.  If the stable handle says it already exited,
                # leave quietly and never signal a potentially reused PID.
                if guard.is_alive():
                    guard.terminate(1)
                    return 1
                return 0
            frame, received_ns = received
            # 用 reader 实际取得完整 frame 的时刻判断旧 deadline。这样主监控线程
            # 即使随后被暂停，也不会把 deadline 后才到达的 REARM/DISARM 当作及时。
            if received_ns >= deadline_ns:
                if guard.is_alive():
                    guard.terminate(1)
                return 1
            try:
                next_sequence, opcode, requested_deadline_ns = _parse_command(
                    frame,
                    nonce=nonce,
                    previous_sequence=sequence,
                )
            except (TypeError, ValueError):
                if guard.is_alive():
                    guard.terminate(1)
                return 2
            sequence = next_sequence
            now_ns = _lease_clock_ns()
            if now_ns >= deadline_ns:
                if guard.is_alive():
                    guard.terminate(1)
                return 1
            if opcode == "DISARM":
                if fatal:
                    _write_frame("ACK", nonce, sequence, "REJECTED")
                    continue
                _write_frame("ACK", nonce, sequence, "DISARMED")
                return 0
            assert requested_deadline_ns is not None
            if opcode == "FATAL":
                fatal = True
                deadline_ns = min(deadline_ns, requested_deadline_ns)
                if deadline_ns <= now_ns:
                    if guard.is_alive():
                        guard.terminate(1)
                    return 1
                _write_frame("ACK", nonce, sequence, "FATAL", deadline_ns)
                continue
            if opcode == "SHUTDOWN":
                shutdown = True
                deadline_ns = min(deadline_ns, requested_deadline_ns)
                if deadline_ns <= now_ns:
                    if guard.is_alive():
                        guard.terminate(1)
                    return 1
                _write_frame("ACK", nonce, sequence, "SHUTDOWN", deadline_ns)
                continue
            if fatal or shutdown:
                _write_frame("ACK", nonce, sequence, "REJECTED")
                continue
            deadline_ns = requested_deadline_ns
            if deadline_ns <= now_ns:
                if guard.is_alive():
                    guard.terminate(1)
                return 1
            _write_frame("ACK", nonce, sequence, "REARMED", deadline_ns)
    finally:
        guard.close()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--deadline-ns", required=True, type=int)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--parent-pidfd", type=int)
    parser.add_argument("--parent-creation-token", type=int)
    args = parser.parse_args()
    if args.parent_pid <= 0 or args.deadline_ns <= 0:
        return 2
    if len(args.nonce) != 32 or any(
        char not in "0123456789abcdef" for char in args.nonce
    ):
        return 2
    try:
        return _run(
            parent_pid=args.parent_pid,
            initial_deadline_ns=args.deadline_ns,
            nonce=args.nonce,
            parent_pidfd=args.parent_pidfd,
            parent_creation_token=args.parent_creation_token,
        )
    except Exception:
        # No stderr details: the parent only needs a failed READY handshake and
        # must not leak environment/path details through a safety process.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
