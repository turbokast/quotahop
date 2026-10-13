"""Bounded Linux foreground workers; no executor threads, daemon, or disk IPC."""
import ctypes
import json
import os
import selectors
import signal
import time

MAX_RESULT = 1024 * 1024


def run(tasks, workers=4, timeout=18):
    """Return one JSON result per callable (None on failure), always reap children.

    Fork keeps the existing hard DNS/request deadline working in each child.
    Credentials stay in inherited memory, never arguments or temporary files.
    A parent-death signal also stops a worker if the parent is forcibly killed.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    parent_pid = os.getpid()
    results = [None] * len(tasks)
    active = {}
    next_index = 0
    previous_handlers = {}
    cancelled = False

    def interrupted(signum, frame):
        nonlocal cancelled
        # Do not interrupt the tiny fork/register or close/reap critical sections.
        cancelled = True

    def reap(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        while True:
            try:
                os.waitpid(pid, 0)
                return
            except InterruptedError:
                continue

    with selectors.DefaultSelector() as selector:
        try:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous_handlers[sig] = signal.signal(sig, interrupted)
            while next_index < len(tasks) or active:
                if cancelled:
                    raise KeyboardInterrupt
                while next_index < len(tasks) and len(active) < workers:
                    read_fd, write_fd = os.pipe()
                    try:
                        pid = os.fork()
                    except BaseException:
                        os.close(read_fd)
                        os.close(write_fd)
                        raise
                    if pid == 0:
                        try:
                            for sig in previous_handlers:
                                signal.signal(sig, signal.SIG_DFL)
                            if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
                                os._exit(1)
                            os.close(read_fd)
                            for other_fd in active:
                                os.close(other_fd)
                            selector.close()
                            payload = json.dumps(tasks[next_index](), allow_nan=False).encode()
                            if len(payload) > MAX_RESULT:
                                os._exit(1)
                            while payload:
                                payload = payload[os.write(write_fd, payload):]
                            os.close(write_fd)
                            os._exit(0)
                        except BaseException:
                            # Never emit exception text or traceback from a worker.
                            os._exit(1)
                    os.close(write_fd)
                    active[read_fd] = (pid, next_index, time.monotonic() + timeout, bytearray())
                    os.set_blocking(read_fd, False)
                    selector.register(read_fd, selectors.EVENT_READ)
                    next_index += 1
                wait = max(0, min(item[2] for item in active.values()) - time.monotonic())
                ready = {key.fd for key, _ in selector.select(min(wait, 0.2))}
                if cancelled:
                    raise KeyboardInterrupt
                for fd, (pid, index, expires, buffer) in list(active.items()):
                    finished = False
                    if fd in ready:
                        chunk = os.read(fd, 65536)
                        buffer.extend(chunk)
                        if not chunk:
                            try:
                                results[index] = json.loads(buffer)
                            except (ValueError, UnicodeError):
                                pass
                            finished = True
                    if finished or len(buffer) > MAX_RESULT or time.monotonic() >= expires:
                        selector.unregister(fd)
                        os.close(fd)
                        reap(pid)
                        del active[fd]
        finally:
            # Ignore repeated terminal signals while cleaning up; restore the
            # caller's handlers only after every owned child is gone.
            for sig in previous_handlers:
                signal.signal(sig, signal.SIG_IGN)
            for fd, (pid, _, _, _) in active.items():
                os.close(fd)
                reap(pid)
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
    return results
