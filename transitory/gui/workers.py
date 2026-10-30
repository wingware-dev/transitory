"""Run ``analyze_star`` for the submitted stars off the GUI thread.

``AnalysisRunner.submit(targets)`` queues one job per star on a
``QThreadPool``; progress, results and failures arrive on the GUI thread as
signals.  ``cancel()`` drops queued stars and stops running ones at their
next progress report (``AnalysisCancelled``).  Requests to MAST are serial
anyway (``pipeline/fetch.py`` holds a lock), so a few workers only overlap
the number crunching.

``ProgressPanel`` shows the runner in the window: one row per star with its
current stage or outcome (stage errors in red), an overall bar and Cancel.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import plots
from ..models import StarAnalysis, StarTarget
from ..pipeline import AnalysisCancelled
from ..pipeline.run import analyze_star
from ..sources import LightCurveSource

log = logging.getLogger(__name__)

DEFAULT_WORKERS = 2

# analyze_star(target, source, progress_cb) -> StarAnalysis; tests swap in stand-ins.
Analyzer = Callable[..., StarAnalysis]


class _Signals(QObject):
    """Owned by the runner on the GUI thread; emits from workers arrive there queued."""

    progress = Signal(int, str, float)  # job id, message, fraction
    finished = Signal(int, object)  # job id, StarAnalysis
    failed = Signal(int, str)  # job id, error text
    cancelled = Signal(int)  # job id


class _AnalysisJob(QRunnable):
    def __init__(self, job_id: int, target: StarTarget, source: LightCurveSource,
                 analyzer: Analyzer, signals: _Signals, cancel: threading.Event):
        super().__init__()
        self.setAutoDelete(True)
        self.job_id = job_id
        self.target = target
        self.source = source
        self.analyzer = analyzer
        self.signals = signals
        self.cancel = cancel

    def _progress(self, message: str, fraction: float) -> None:
        if self.cancel.is_set():
            raise AnalysisCancelled()
        self.signals.progress.emit(self.job_id, message, float(fraction))

    def run(self) -> None:
        if self.cancel.is_set():
            self.signals.cancelled.emit(self.job_id)
            return
        try:
            result = self.analyzer(self.target, self.source, progress_cb=self._progress)
        except AnalysisCancelled:
            self.signals.cancelled.emit(self.job_id)
        except Exception as exc:  # analyze_star records stage errors itself; this is anything else
            log.exception("analysing %s failed", self.target.name)
            self.signals.failed.emit(self.job_id, f"{type(exc).__name__}: {exc}")
        else:
            self.signals.finished.emit(self.job_id, result)


class AnalysisRunner(QObject):
    """Analyses stars in a thread pool; one signal per event, all on the GUI thread."""

    queued = Signal(object)  # StarTarget
    starProgress = Signal(str, str, float)  # star name, message, fraction 0..1
    starFinished = Signal(object)  # StarAnalysis (stage errors in .errors)
    starFailed = Signal(str, str)  # star name, error text (analysis raised)
    starCancelled = Signal(str)  # star name
    idle = Signal()  # the last queued star is done, failed or cancelled

    def __init__(self, source: LightCurveSource | None, max_workers: int = DEFAULT_WORKERS,
                 analyzer: Analyzer = analyze_star, parent: QObject | None = None):
        super().__init__(parent)
        self.source = source
        self.analyzer = analyzer
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(1, max_workers))
        self._signals = _Signals(self)
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.failed.connect(self._on_failed)
        self._signals.cancelled.connect(self._on_cancelled)
        self._next_id = 0
        self._jobs: dict[int, tuple[StarTarget, threading.Event]] = {}

    @property
    def busy(self) -> bool:
        return bool(self._jobs)

    def pending_names(self) -> list[str]:
        return [target.name for target, _ in self._jobs.values()]

    def submit(self, targets: list[StarTarget]) -> list[StarTarget]:
        """Queue ``targets``; stars already queued or running are skipped.  Returns those queued."""
        if self.source is None:
            raise RuntimeError("no data source: start with --demo, or install the MAST dependencies")
        new_run = getattr(self.source, "new_run", None)
        if not self.busy and new_run is not None:
            new_run()  # stars added to a busy run join it; an idle runner starts afresh
        pending = set(self.pending_names())
        queued = []
        for target in targets:
            if target.name in pending:
                continue
            pending.add(target.name)
            job_id = self._next_id
            self._next_id += 1
            cancel = threading.Event()
            self._jobs[job_id] = (target, cancel)
            queued.append(target)
            self.queued.emit(target)
            self.pool.start(_AnalysisJob(job_id, target, self.source, self.analyzer,
                                         self._signals, cancel))
        return queued

    def cancel(self) -> None:
        """Stop everything: queued stars never start, running ones stop at their next step."""
        for _, event in self._jobs.values():
            event.set()

    def wait(self, msecs: int = -1) -> bool:
        """Block until the workers are done (their signals still need the event loop)."""
        return self.pool.waitForDone(msecs)

    # -- worker events, on the GUI thread -------------------------------------------

    def _target(self, job_id: int) -> StarTarget | None:
        job = self._jobs.get(job_id)
        return job[0] if job else None

    def _on_progress(self, job_id: int, message: str, fraction: float) -> None:
        target = self._target(job_id)
        if target is not None and not self._jobs[job_id][1].is_set():
            self.starProgress.emit(target.name, message, fraction)

    def _done(self, job_id: int) -> StarTarget | None:
        job = self._jobs.pop(job_id, None)
        return job[0] if job else None

    def _after(self) -> None:
        if not self._jobs:
            self.idle.emit()

    def _on_finished(self, job_id: int, result: StarAnalysis) -> None:
        if self._done(job_id) is not None:
            self.starFinished.emit(result)
            self._after()

    def _on_failed(self, job_id: int, error: str) -> None:
        target = self._done(job_id)
        if target is not None:
            self.starFailed.emit(target.name, error)
            self._after()

    def _on_cancelled(self, job_id: int) -> None:
        target = self._done(job_id)
        if target is not None:
            self.starCancelled.emit(target.name)
            self._after()


# -- the panel ---------------------------------------------------------------------

ERROR_COLOR = QColor(192, 57, 43)
DONE_COLOR = QColor(39, 119, 63)
MUTED_COLOR = QColor(119, 119, 119)


def outcome_text(analysis: StarAnalysis) -> str:
    """One line for a finished star: its candidates, or the stage errors."""
    if analysis.errors:
        return "Error — " + "; ".join(analysis.errors)
    n = len(analysis.planets)
    strong = sum(1 for p in analysis.planets if p.vetting is not None and p.vetting.confidence == "strong")
    if n == 0:
        return "Done — no candidates"
    duos = sum(1 for p in analysis.planets if p.is_duo_transit)
    return f"Done — {plots.candidate_count_text(n, duos)} ({strong} strong)"


class ProgressPanel(QWidget):
    """Per-star status rows, an overall progress bar and a Cancel button for a runner."""

    COL_STAR, COL_STATUS = 0, 1

    def __init__(self, runner: AnalysisRunner, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("progressPanel")
        self.runner = runner
        self._items: dict[str, QTreeWidgetItem] = {}
        self._fractions: dict[str, float] = {}

        self.summary = QLabel("Submit stars to analyse them.")
        self.summary.setObjectName("progressSummary")
        self.bar = QProgressBar()
        self.bar.setObjectName("overallProgress")
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.clear_button = QPushButton("Clear finished")
        self.clear_button.setObjectName("clearFinishedButton")
        self.clear_button.clicked.connect(self.clear_finished)
        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(self.bar, 1)
        top.addWidget(self.cancel_button)
        top.addWidget(self.clear_button)

        self.tree = QTreeWidget()
        self.tree.setObjectName("progressList")
        self.tree.setHeaderLabels(["Star", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setSectionResizeMode(self.COL_STAR, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)

        runner.queued.connect(self._queued)
        runner.starProgress.connect(self._progress)
        runner.starFinished.connect(self._finished)
        runner.starFailed.connect(lambda name, error: self._ended(name, f"Error — {error}", ERROR_COLOR))
        runner.starCancelled.connect(lambda name: self._ended(name, "Cancelled", MUTED_COLOR))
        runner.idle.connect(self._update)

    def status(self, star_name: str) -> str | None:
        item = self._items.get(star_name)
        return item.text(self.COL_STATUS) if item else None

    def cancel(self) -> None:
        self.runner.cancel()
        self.cancel_button.setEnabled(False)
        self.summary.setText("Cancelling…")

    def clear_finished(self) -> None:
        pending = set(self.runner.pending_names())
        for name in [n for n in self._items if n not in pending]:
            item = self._items.pop(name)
            self._fractions.pop(name, None)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        self._update()

    def _queued(self, target: StarTarget) -> None:
        item = self._items.get(target.name)
        if item is None:
            item = QTreeWidgetItem([target.name, ""])
            self._items[target.name] = item
            self.tree.addTopLevelItem(item)
        self._set(item, "Queued", MUTED_COLOR)
        self._fractions[target.name] = 0.0
        self._update()

    def _progress(self, name: str, message: str, fraction: float) -> None:
        item = self._items.get(name)
        if item is None:
            return
        prefix = f"{name}: "
        text = message[len(prefix):] if message.startswith(prefix) else message
        self._set(item, f"{fraction:4.0%}  {text}", None)
        self._fractions[name] = max(0.0, min(1.0, fraction))
        self._update()

    def _finished(self, analysis: StarAnalysis) -> None:
        self._ended(analysis.target.name, outcome_text(analysis),
                    ERROR_COLOR if analysis.errors else DONE_COLOR)

    def _ended(self, name: str, text: str, color: QColor) -> None:
        item = self._items.get(name)
        if item is not None:
            self._set(item, text, color)
        self._fractions[name] = 1.0
        self._update()

    def _set(self, item: QTreeWidgetItem, text: str, color: QColor | None) -> None:
        item.setText(self.COL_STATUS, text)
        item.setToolTip(self.COL_STATUS, text)
        brush = QBrush(color) if color is not None else QBrush()
        item.setForeground(self.COL_STATUS, brush)

    def _update(self) -> None:
        pending = self.runner.pending_names()
        self.cancel_button.setEnabled(bool(pending))
        self.clear_button.setEnabled(len(self._items) > len(pending))
        if pending:
            batch = [self._fractions.get(n, 0.0) for n in self._items]
            done = len(self._items) - len(pending)
            self.bar.setValue(int(1000 * sum(batch) / max(1, len(batch))))
            self.summary.setText(f"Analysing: {done} of {len(self._items)} stars done")
        else:
            self.bar.setValue(1000 if self._items else 0)
            self.summary.setText("Submit stars to analyse them." if not self._items else
                                 f"Finished {len(self._items)} star{'s' if len(self._items) != 1 else ''}")
