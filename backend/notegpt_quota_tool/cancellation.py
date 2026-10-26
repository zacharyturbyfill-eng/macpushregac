"""Cooperative stop helpers for blocking IO and owned child processes."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, wait
from pathlib import Path
from typing import Any, Callable


class OperationStopped(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Stopped by user")


def check_stop(should_stop: Callable[[], bool]) -> None:
    if should_stop():
        raise OperationStopped()


def completed_futures(futures: Any, should_stop: Callable[[], bool]):
    pending = set(futures)
    try:
        while pending and not should_stop():
            done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                if not should_stop():
                    yield future
    finally:
        for future in pending:
            future.cancel()


def interruptible_sleep(seconds: float, should_stop: Callable[[], bool]) -> None:
    deadline = time.monotonic() + max(0, seconds)
    while True:
        check_stop(should_stop)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def interruptible_call(call: Callable[..., Any], should_stop: Callable[[], bool],
                       *args: Any, **kwargs: Any) -> Any:
    """Detach only the bounded HTTP call, never a whole worker or its writes.

    An already-sent request cannot be revoked remotely. Its late response is
    discarded; the caller cannot retry or continue processing after Stop.
    """
    check_stop(should_stop)
    finished = threading.Event()
    result: list[Any] = []
    errors: list[BaseException] = []

    def request() -> None:
        try:
            check_stop(should_stop)
            value = call(*args, **kwargs)
            result.append(value)
            if should_stop() and hasattr(value, "close"):
                value.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    threading.Thread(target=request, name="cancellable-http", daemon=True).start()
    while not finished.wait(0.1):
        check_stop(should_stop)
    check_stop(should_stop)
    if errors:
        raise errors[0]
    return result[0]


def run_process(command: Any, *, should_stop: Callable[[], bool],
                timeout: float, capture_output: bool = False,
                output_path: Any = None, **kwargs: Any) -> subprocess.CompletedProcess:
    if output_path is not None:
        output = Path(output_path)
        temporary = output.with_name(f".nq-{uuid.uuid4().hex[:16]}{output.suffix}")
        if str(command[-1]) != str(output):
            raise ValueError("Expected output as final process argument")
        try:
            result = run_process(
                [*command[:-1], str(temporary)], should_stop=should_stop,
                timeout=timeout, capture_output=capture_output, **kwargs,
            )
            check_stop(should_stop)
            if result.returncode == 0:
                temporary.replace(output)
            return result
        finally:
            temporary.unlink(missing_ok=True)
    check_stop(should_stop)
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with subprocess.Popen(command, **kwargs) as proc:
        deadline = time.monotonic() + timeout
        try:
            while True:
                check_stop(should_stop)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                    check_stop(should_stop)
                    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise
        except BaseException:
            proc.kill()
            proc.communicate()
            raise


def copy_file(source: Any, output: Any, should_stop: Callable[[], bool]) -> None:
    output = Path(output)
    temporary = output.with_name(f".nq-{uuid.uuid4().hex[:16]}{output.suffix}")
    try:
        check_stop(should_stop)
        with open(source, "rb") as reader, temporary.open("wb") as writer:
            while True:
                check_stop(should_stop)
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
        check_stop(should_stop)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


class BrowserStopGuard:
    """Interrupt Playwright on its owning driver PID, never cross-thread APIs."""

    def __init__(self, playwright: Any, should_stop: Callable[[], bool]) -> None:
        self.should_stop = should_stop
        self.proc = playwright._impl_obj._connection._transport._proc
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="browser-stop", daemon=True)

    def __enter__(self) -> "BrowserStopGuard":
        check_stop(self.should_stop)
        self.thread.start()
        return self

    def _watch(self) -> None:
        while not self.done.wait(0.1):
            if not self.should_stop():
                continue
            # Keep the process object to avoid killing a reused PID after exit.
            if self.proc.returncode is None:
                try:
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=3, creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        os.kill(self.proc.pid, signal.SIGTERM)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            return

    def __exit__(self, *_args: Any) -> None:
        self.done.set()
        self.thread.join(timeout=4)
