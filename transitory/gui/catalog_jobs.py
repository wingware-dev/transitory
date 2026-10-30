"""Run catalog lookups off the GUI thread.

``JobRunner.submit(fn, on_done, on_error)`` calls ``fn()`` in a worker
thread and delivers its result (or exception) to the callbacks on the GUI
thread.  One worker per runner, so a widget's lookups run in order, and
the catalog client serialises requests across runners anyway.

``DetachedJobRunner`` does the same on a daemon thread, for jobs (sky
images) that may stall on a network timeout but must never hold up closing
the window or quitting: a thread pool's teardown waits for its jobs, a
daemon thread is abandoned.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

log = logging.getLogger(__name__)


class _Relay(QObject):
    """Lives on the GUI thread; signals emitted from the worker arrive there queued."""

    done = Signal(object)
    failed = Signal(object)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any], relay: _Relay):
        super().__init__()
        self.fn = fn
        self.relay = relay

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # delivered to on_error on the GUI thread, which reports it
            self.relay.failed.emit(exc)
        else:
            self.relay.done.emit(result)


class JobRunner(QObject):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._relays: set[_Relay] = set()

    @property
    def busy(self) -> bool:
        return bool(self._relays)

    def submit(self, fn: Callable[[], Any], on_done: Callable[[Any], None],
               on_error: Callable[[Exception], None]) -> None:
        relay = _Relay(self)
        self._relays.add(relay)

        def finish(callback, value):
            self._relays.discard(relay)
            relay.deleteLater()
            callback(value)

        relay.done.connect(lambda value: finish(on_done, value))
        relay.failed.connect(lambda exc: finish(on_error, exc))
        self.pool.start(_Job(fn, relay))

    def wait(self, msecs: int = 30000) -> bool:
        """Block until queued jobs have run (their callbacks still need the event loop)."""
        return self.pool.waitForDone(msecs)


class DetachedJobRunner(QObject):
    """A ``JobRunner`` whose jobs never hold up closing or quitting.

    Jobs run one at a time, in order, on a daemon thread that exists only
    while there are jobs.  Nothing waits for a job under way: when its
    widget closes, call ``discard``, and no callback of the jobs so far
    runs.  A result that arrives for a deleted runner is dropped too.
    """

    def __init__(self, parent: QObject | None = None, name: str = "detached-jobs"):
        super().__init__(parent)
        self.name = name
        self._jobs: collections.deque[tuple[Callable[[], Any], _Relay]] = collections.deque()
        self._lock = threading.Condition()
        self._running = False
        self._relays: set[_Relay] = set()

    @property
    def busy(self) -> bool:
        return bool(self._relays)

    def submit(self, fn: Callable[[], Any], on_done: Callable[[Any], None],
               on_error: Callable[[Exception], None]) -> None:
        relay = _Relay(self)
        self._relays.add(relay)

        def finish(callback, value):
            relay.deleteLater()
            if relay not in self._relays:  # discarded while it ran
                if isinstance(value, Exception):
                    log.warning("%s: a discarded job failed: %s", self.name, value)
                else:
                    log.debug("%s: result dropped, its job was discarded", self.name)
                return
            self._relays.discard(relay)
            callback(value)

        relay.done.connect(lambda value: finish(on_done, value))
        relay.failed.connect(lambda exc: finish(on_error, exc))
        with self._lock:
            self._jobs.append((fn, relay))
            if not self._running:
                self._running = True
                threading.Thread(target=self._work, name=self.name, daemon=True).start()

    def discard(self) -> None:
        """Drop every job submitted so far, for a close: no callback of theirs will run.

        Jobs not yet started never run; the one under way runs on, unwaited
        for, and its result is dropped.  The runner takes new jobs as before.
        """
        with self._lock:
            queued = [relay for _fn, relay in self._jobs]
            self._jobs.clear()
        self._relays.clear()
        for relay in queued:
            relay.deleteLater()  # the one under way is deleted when its result arrives

    def wait(self, msecs: int = 30000) -> bool:
        """Block until queued jobs have run (their callbacks still need the event loop)."""
        deadline = time.monotonic() + msecs / 1000
        with self._lock:
            while self._running:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._lock.wait(left)
        return True

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._jobs:
                    self._running = False
                    self._lock.notify_all()
                    return
                fn, relay = self._jobs.popleft()
            try:
                result = fn()
            except Exception as exc:  # delivered to on_error on the GUI thread, which reports it
                signal, value = relay.failed, exc
            else:
                signal, value = relay.done, result
            try:
                signal.emit(value)
            except RuntimeError as exc:  # the runner, and so the relay, was deleted meanwhile
                log.debug("%s: result dropped, its runner is gone (%s)", self.name, exc)
