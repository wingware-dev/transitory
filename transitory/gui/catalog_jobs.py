"""Run catalog lookups off the GUI thread.

``JobRunner.submit(fn, on_done, on_error)`` calls ``fn()`` in a worker
thread and delivers its result (or exception) to the callbacks on the GUI
thread.  One worker per runner, so a widget's lookups run in order, and
the catalog client serialises requests across runners anyway.
"""

from __future__ import annotations

import logging
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
