"""Results gallery: a scrollable section per analysed star with its plots.py figures.

``GalleryWidget.add_analysis`` adds (or replaces) a star's section;
``save_all`` writes every star's figures as PNGs via ``plots.save_all``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import plots
from ..models import StarAnalysis

log = logging.getLogger(__name__)

MIN_FIGURE_WIDTH = 520  # px; below this the gallery scrolls sideways instead of shrinking text


class GalleryCanvas(FigureCanvasQTAgg):
    """A figure canvas that scales the whole figure with its width.

    The stock canvas keeps dpi fixed and changes the figure's size in
    inches, which squeezes plots.py's point-sized fonts and footer.  Here
    the dpi follows the width so the layout stays as designed, and the
    height follows the figure's aspect ratio.
    """

    def __init__(self, figure):
        self._size_in = tuple(figure.get_size_inches())
        super().__init__(figure)
        self.setMinimumWidth(MIN_FIGURE_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.heightForWidth(MIN_FIGURE_WIDTH))

    @property
    def aspect(self) -> float:
        return self._size_in[1] / self._size_in[0]

    def heightForWidth(self, width: int) -> int:
        return round(width * self.aspect)

    def resizeEvent(self, event):
        width = event.size().width()
        if width > 0:
            # Logical dpi that maps the widget's width onto the figure's design width.
            # _original_dpi is what matplotlib rescales from when the pixel ratio changes.
            dpi = width / self._size_in[0]
            self.figure._original_dpi = dpi
            self.figure._set_dpi(dpi * self.device_pixel_ratio, forward=False)
            height = self.heightForWidth(width)
            if self.height() != height:
                self.setFixedHeight(height)  # may resize (and re-enter) right away
        super().resizeEvent(event)
        # The base class sizes the figure from this event's height, which is stale
        # when setFixedHeight above changed it; the design size is the right one.
        self.figure.set_size_inches(*self._size_in, forward=False)

    def wheelEvent(self, event):
        event.ignore()  # let the gallery scroll; the figures have no wheel interaction


def has_plots(analysis: StarAnalysis) -> bool:
    return analysis.raw is not None and len(analysis.raw) > 0


def _summary_text(analysis: StarAnalysis) -> str:
    parts = []
    if analysis.raw is not None:
        parts.append(f"{analysis.raw.mission}, {len(analysis.raw):,} points, "
                     f"{analysis.raw.baseline_days:.1f} d")
    n = len(analysis.planets)
    parts.append("no candidates" if n == 0 else f"{n} candidate{'s' if n != 1 else ''}")
    return " · ".join(parts)


class StarSection(QFrame):
    """Header (name, summary, confidence badges, errors, save button) and the star's figures."""

    saveRequested = Signal(object)  # StarAnalysis

    def __init__(self, analysis: StarAnalysis, parent: QWidget | None = None):
        super().__init__(parent)
        self.analysis = analysis
        self.setObjectName("starSection")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)

        header = QHBoxLayout()
        self.toggle = QToolButton()
        self.toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(True)
        self.toggle.setAutoRaise(True)
        self.toggle.setToolTip("Show or hide this star's plots")
        self.toggle.toggled.connect(self.set_expanded)
        header.addWidget(self.toggle)

        title = QLabel(f"<b>{analysis.target.label}</b>")
        title.setObjectName("starTitle")
        title.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(title)
        self.summary = QLabel(_summary_text(analysis))
        self.summary.setStyleSheet("color: #555;")
        header.addWidget(self.summary)
        for i, planet in enumerate(analysis.planets):
            conf = planet.vetting.confidence if planet.vetting is not None else "none"
            badge = QLabel(f" {plots.planet_letter(i)}: {conf} ")
            badge.setObjectName("confidenceBadge")
            badge.setStyleSheet(f"background: {plots.confidence_color(planet)}; color: white;"
                                " border-radius: 3px; font-size: 11px;")
            header.addWidget(badge)
        header.addStretch(1)
        self.save_button = QPushButton("Save PNGs…")
        self.save_button.setToolTip("Save this star's figures as PNG files")
        self.save_button.setEnabled(has_plots(analysis))
        self.save_button.clicked.connect(lambda: self.saveRequested.emit(self.analysis))
        header.addWidget(self.save_button)
        layout.addLayout(header)

        if analysis.errors:
            errors = QLabel("\n".join(analysis.errors))
            errors.setObjectName("starErrors")
            errors.setWordWrap(True)
            errors.setStyleSheet("color: #b00020;")
            layout.addWidget(errors)

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        # A star with no light curve has nothing to plot; its header and errors say why.
        self.figures = plots.gallery_figures(analysis) if has_plots(analysis) else []
        self.canvases: list[GalleryCanvas] = []
        for item in self.figures:
            canvas = GalleryCanvas(item.figure)
            canvas.setObjectName(f"figure-{item.key}")
            canvas.setToolTip(item.title)
            self.canvases.append(canvas)
            body_layout.addWidget(canvas)
        layout.addWidget(self.body)

    @property
    def key(self) -> str:
        return self.analysis.target.name

    def set_expanded(self, expanded: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.body.setVisible(expanded)


class GalleryWidget(QWidget):
    """The Gallery tab: a toolbar with Save all, then one StarSection per star."""

    saved = Signal(list)  # paths written

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("gallery")
        self._sections: dict[str, StarSection] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 0)
        self.status = QLabel()
        self.status.setObjectName("galleryStatus")
        bar.addWidget(self.status, 1)
        self.save_all_button = QPushButton("Save all PNGs…")
        self.save_all_button.setObjectName("saveAllButton")
        self.save_all_button.clicked.connect(lambda: self._save_interactive(None))
        bar.addWidget(self.save_all_button)
        outer.addLayout(bar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self.sections_layout = QVBoxLayout(content)
        self.sections_layout.setSpacing(10)
        self.empty_label = QLabel("Plots for each analysed star will appear here.\n\n"
                                  "Select stars and press Submit.")
        self.empty_label.setObjectName("galleryEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color: #777; font-size: 14px; padding: 40px;")
        self.sections_layout.addWidget(self.empty_label)
        self.sections_layout.addStretch(1)
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll, 1)
        self._update_status()

    # --- contents ---------------------------------------------------------

    def sections(self) -> list[StarSection]:
        return list(self._sections.values())

    def analyses(self) -> list[StarAnalysis]:
        return [s.analysis for s in self._sections.values()]

    def section(self, star_name: str) -> StarSection | None:
        return self._sections.get(star_name)

    def add_analysis(self, analysis: StarAnalysis) -> StarSection:
        """Add a star's section at the end, or replace its section in place if already shown."""
        old = self._sections.get(analysis.target.name)
        section = StarSection(analysis)
        section.saveRequested.connect(lambda a: self._save_interactive([a]))
        if old is not None:
            index = self.sections_layout.indexOf(old)
            self.sections_layout.removeWidget(old)
            old.deleteLater()
        else:
            index = self.sections_layout.count() - 1  # before the stretch
        self.sections_layout.insertWidget(index, section)
        self._sections[analysis.target.name] = section
        self._update_status()
        return section

    def set_analyses(self, analyses: list[StarAnalysis]) -> None:
        self.clear()
        for analysis in analyses:
            self.add_analysis(analysis)

    def clear(self) -> None:
        for section in self._sections.values():
            self.sections_layout.removeWidget(section)
            section.deleteLater()
        self._sections.clear()
        self._update_status()

    def scroll_to(self, star_name: str) -> bool:
        section = self._sections.get(star_name)
        if section is None:
            return False
        self.scroll.verticalScrollBar().setValue(section.y())
        return True

    def _update_status(self) -> None:
        n = len(self._sections)
        self.empty_label.setVisible(n == 0)
        self.save_all_button.setEnabled(any(has_plots(s.analysis) for s in self._sections.values()))
        planets = sum(len(s.analysis.planets) for s in self._sections.values())
        self.status.setText("" if n == 0 else
                            f"{n} star{'s' if n != 1 else ''}, {planets} candidate"
                            f"{'s' if planets != 1 else ''}")

    # --- saving -----------------------------------------------------------

    def save_all(self, directory: Path | str, analyses: list[StarAnalysis] | None = None) -> list[Path]:
        """Save the figures of ``analyses`` (default: every star shown) as PNGs in ``directory``.

        Stars without a light curve are skipped: they have no plots to save.
        """
        paths: list[Path] = []
        for analysis in self.analyses() if analyses is None else analyses:
            if has_plots(analysis):
                paths.extend(plots.save_all(analysis, directory))
        self.saved.emit(paths)
        return paths

    def _save_interactive(self, analyses: list[StarAnalysis] | None) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Save PNGs to folder")
        if not directory:
            return
        try:
            paths = self.save_all(directory, analyses)
        except OSError as exc:
            log.exception("saving gallery PNGs to %s failed", directory)
            QMessageBox.warning(self, "Save failed", f"Could not save PNGs to {directory}:\n{exc}")
            return
        self.status.setText(f"Saved {len(paths)} PNG{'s' if len(paths) != 1 else ''} to {directory}")
