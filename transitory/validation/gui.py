"""The dashboard's Validation menu: run the known-planet validation and show its report.

``ValidationController(window, menu)`` adds the actions.  The run happens
in a worker thread on the window's light-curve source: in demo mode the
synthetic demo stars with their injected planets, otherwise the live set
against the NASA Exoplanet Archive, with the window's sectors / quarters
cap.  Progress goes to the status bar; the report opens when it is written.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..pipeline import AnalysisCancelled
from ..sources import SyntheticSource
from . import report
from .targets import validation_targets
from .validate import archive_provider, run_validation, synthetic_provider, synthetic_targets

log = logging.getLogger(__name__)

MAX_IMAGE_WIDTH = 1000  # px, for report figures in the dialog


def latest_report() -> Path | None:
    """The newest written report: this machine's reports/validation, else the committed copy."""
    candidates = [report.default_directory() / report.REPORT_NAME, report.DOCS_DIR / report.REPORT_NAME]
    existing = [p for p in candidates if p.is_file()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None


class ReportDialog(QDialog):
    """Shows a validation report's Markdown, figures included."""

    def __init__(self, path: Path | str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("validationReportDialog")
        self.path = Path(path)
        self.setWindowTitle(f"Validation report — {self.path}")
        self.resize(1100, 820)
        self.browser = QTextBrowser()
        self.browser.setObjectName("validationReport")
        self.browser.setOpenExternalLinks(True)
        # As a Markdown resource, relative image paths resolve against the report's folder.
        self.browser.setSource(QUrl.fromLocalFile(str(self.path.resolve())),
                               QTextDocument.ResourceType.MarkdownResource)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.folder_button = QPushButton("Open folder")
        self.folder_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent))))
        buttons.addButton(self.folder_button, QDialogButtonBox.ButtonRole.ActionRole)
        layout = QVBoxLayout(self)
        layout.addWidget(self.browser, 1)
        layout.addWidget(buttons)
        self._image_width = 0

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # The viewport takes its new width when the layout runs, after this event.
        QTimer.singleShot(0, self.fit_images)

    def fit_images(self) -> None:
        """Scale the report's figures to the view's width (Markdown has no image sizes)."""
        width = max(300, min(MAX_IMAGE_WIDTH, self.browser.viewport().width() - 40))
        if width == self._image_width:
            return
        self._image_width = width
        doc = self.browser.document()
        # One edit block, so the layout sees one change: re-laid out per figure, the
        # blocks after the figures (and after some tables) were left with no lines.
        edit = QTextCursor(doc)
        edit.beginEditBlock()
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                fmt = fragment.charFormat()
                if fmt.isImageFormat():
                    image = fmt.toImageFormat()
                    image.setWidth(width)  # the PNGs are all wider than this (150 dpi)
                    image.clearProperty(QTextFormat.Property.ImageHeight)  # keep the aspect ratio
                    cursor = QTextCursor(doc)
                    cursor.setPosition(fragment.position())
                    cursor.setPosition(fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor)
                    cursor.setCharFormat(image)
                it += 1
            block = block.next()
        edit.endEditBlock()


class _Signals(QObject):
    progress = Signal(str, float)
    done = Signal(object)  # Path of the written report
    failed = Signal(str)


class _ValidationJob(QRunnable):
    def __init__(self, fn, signals: _Signals):
        super().__init__()
        self.fn = fn
        self.signals = signals

    def run(self) -> None:
        try:
            path = self.fn()
        except Exception as exc:  # reported on the GUI thread
            log.exception("validation failed")
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.signals.done.emit(path)


class ValidationController(QObject):
    reportWritten = Signal(object)  # Path
    finished = Signal()

    def __init__(self, window: QMainWindow, menu: QMenu):
        super().__init__(window)
        self.window = window
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._signals = _Signals(self)
        self._signals.progress.connect(self._progress)
        self._signals.done.connect(self._done)
        self._signals.failed.connect(self._failed)
        self._cancel = threading.Event()
        self.running = False
        self.dialog: ReportDialog | None = None
        self.output_dir: Path | None = None  # None: reports/validation

        self.run_action = menu.addAction("Run known-planet validation…")
        self.run_action.triggered.connect(lambda: self.start())
        self.cancel_action = menu.addAction("Cancel validation")
        self.cancel_action.setEnabled(False)
        self.cancel_action.triggered.connect(self.cancel)
        menu.addSeparator()
        self.show_action = menu.addAction("Show validation report")
        self.show_action.triggered.connect(lambda: self.show_report())

    @property
    def source(self):
        return getattr(self.window, "source", None)

    def _describe(self) -> str:
        if isinstance(self.source, SyntheticSource):
            return ("Run the pipeline on the demo stars and compare what it finds with the planets "
                    "injected into them. This takes about half a minute.")
        cap = getattr(self.source, "max_products", None)
        return (f"Run the pipeline on {len(validation_targets())} stars with known answers "
                "(planet hosts and null stars) "
                "and compare with the NASA Exoplanet Archive.\n\nThe first run downloads a few hundred MB "
                "of TESS and Kepler light curves from MAST (cached afterwards) and takes a long time; "
                f"it uses {'all sectors / quarters' if cap is None else f'up to {cap} sectors / quarters'} "
                "per star (Settings menu).")

    def start(self, confirm: bool = True) -> bool:
        if self.running or self.source is None:
            return False
        if confirm and QMessageBox.question(self.window, "Known-planet validation",
                                            self._describe() + "\n\nStart now?") \
                != QMessageBox.StandardButton.Yes:
            return False
        source = self.source
        if isinstance(source, SyntheticSource):
            targets, provider = synthetic_targets(source), synthetic_provider(source)
        else:
            targets, provider = validation_targets(), archive_provider()
        cap = getattr(source, "max_products", None)
        out = self.output_dir
        cancel = self._cancel
        cancel.clear()
        signals = self._signals

        def progress(message: str, fraction: float) -> None:
            if cancel.is_set():
                raise AnalysisCancelled()
            signals.progress.emit(message, fraction)

        def work() -> Path:
            run = run_validation(targets, source, provider, progress_cb=progress, max_products=cap)
            return report.write_report(run, out)

        self.running = True
        self.run_action.setEnabled(False)
        self.cancel_action.setEnabled(True)
        self._status("Validation started")
        self.pool.start(_ValidationJob(work, signals))
        return True

    def cancel(self) -> None:
        if not self.running:
            return
        self._cancel.set()
        self._status("Cancelling validation (the report keeps the stars finished so far)…")

    def wait(self, msecs: int = -1) -> bool:
        return self.pool.waitForDone(msecs)

    def show_report(self, path: Path | str | None = None) -> ReportDialog | None:
        path = Path(path) if path is not None else latest_report()
        if path is None:
            QMessageBox.information(self.window, "Validation report",
                                    "No validation report yet: use Validation ▸ Run known-planet validation.")
            return None
        if self.dialog is not None:
            self.dialog.close()
        self.dialog = ReportDialog(path, self.window)
        self.dialog.show()
        return self.dialog

    def _status(self, text: str, timeout: int = 0) -> None:
        self.window.statusBar().showMessage(text, timeout)

    def _progress(self, message: str, fraction: float) -> None:
        self._status(f"Validation {fraction:4.0%}: {message}")

    def _finish(self) -> None:
        self.running = False
        self.run_action.setEnabled(True)
        self.cancel_action.setEnabled(False)
        self.finished.emit()

    def _done(self, path: Path) -> None:
        self._finish()
        self._status(f"Validation report written to {path}", 15000)
        self.reportWritten.emit(path)
        self.show_report(path)

    def _failed(self, error: str) -> None:
        self._finish()
        self._status(f"Validation failed: {error}")
        QMessageBox.warning(self.window, "Validation failed", error)
