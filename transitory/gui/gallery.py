"""Results gallery: a scrollable section per analysed star with its plots.py figures.

``GalleryWidget.add_analysis`` adds (or replaces) a star's section;
``save_all`` writes every star's figures as PNGs via ``plots.save_all``.
Figures are built only when their section is in or near view.
"""

from __future__ import annotations

import logging
from pathlib import Path

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
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


class FigureSlot(QLabel):
    """Placeholder the size a GalleryCanvas will be, shown until the figure is built."""

    ASPECT = plots.FIG_SIZE[1] / plots.FIG_SIZE[0]

    def __init__(self, title: str):
        super().__init__(f"{title}\n\nLoading plot…")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: white; color: #999;")
        self.setMinimumWidth(MIN_FIGURE_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.heightForWidth(MIN_FIGURE_WIDTH))

    def heightForWidth(self, width: int) -> int:
        return round(width * self.ASPECT)

    def resizeEvent(self, event):
        height = self.heightForWidth(event.size().width())
        if self.height() != height:
            self.setFixedHeight(height)
        super().resizeEvent(event)


def has_plots(analysis: StarAnalysis) -> bool:
    return analysis.raw is not None and len(analysis.raw) > 0


def _summary_text(analysis: StarAnalysis) -> str:
    parts = []
    if analysis.raw is not None:
        parts.append(f"{analysis.raw.mission}, {len(analysis.raw):,} points, "
                     f"{analysis.raw.baseline_days:.1f} d")
        if analysis.raw.source:
            parts.append(f"from {analysis.raw.source}")
    n = len(analysis.planets)
    parts.append("no candidates" if n == 0 else plots.candidate_count_text(
        n, sum(1 for p in analysis.planets if p.is_duo_transit)))
    return " · ".join(parts)


class StarSection(QFrame):
    """Header (name, summary, confidence badges, errors, save button) and the star's figures."""

    saveRequested = Signal(object)  # StarAnalysis
    expandedChanged = Signal(bool)

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

        # Figures are built on demand (see GalleryWidget._build_visible); until then
        # same-sized placeholders hold their place, so scrolling never jumps.
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        # A star with no light curve has nothing to plot; its header and errors say why.
        self.specs = plots.gallery_figure_specs(analysis) if has_plots(analysis) else []
        self.figures: list[plots.GalleryFigure] = []
        self.canvases: list[GalleryCanvas] = []
        self.slots: list[FigureSlot] = []
        for spec in self.specs:
            slot = FigureSlot(spec.title)
            slot.setObjectName(f"slot-{spec.key}")
            self.slots.append(slot)
            self.body_layout.addWidget(slot)
        layout.addWidget(self.body)

    @property
    def key(self) -> str:
        return self.analysis.target.name

    @property
    def figure_keys(self) -> list[str]:
        """Keys of this star's figures ("lightcurve", ...), built or not."""
        return [s.key for s in self.specs]

    @property
    def built(self) -> bool:
        return len(self.canvases) == len(self.specs)

    @property
    def expanded(self) -> bool:
        return self.toggle.isChecked()

    def build(self) -> None:
        """Make the figures and swap them in for the placeholders (once)."""
        if self.built:
            return
        for spec, slot in zip(self.specs, self.slots):
            figure = plots.GalleryFigure(spec.key, spec.title, spec.build())
            canvas = GalleryCanvas(figure.figure)
            canvas.setObjectName(f"figure-{spec.key}")
            canvas.setToolTip(spec.title)
            # Start at the placeholder's size, so the section's height never dips meanwhile.
            canvas.setFixedHeight(slot.height())
            canvas.resize(slot.size())
            self.body_layout.replaceWidget(slot, canvas)
            slot.deleteLater()
            self.figures.append(figure)
            self.canvases.append(canvas)
        self.slots = []

    def set_expanded(self, expanded: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.body.setVisible(expanded)
        self.expandedChanged.emit(expanded)


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

        # Figures are built lazily: what is in view at once, then neighbours one
        # section per event-loop pass, so adding many results stays responsive.
        self._build_timer = QTimer(self)
        self._build_timer.setSingleShot(True)
        self._build_timer.setInterval(0)
        self._build_timer.timeout.connect(self._build_step)
        # scroll_to's target, kept at the top while layout settles, until the user scrolls
        self._pinned: StarSection | None = None
        self._pinning = False
        bar_v = self.scroll.verticalScrollBar()
        bar_v.rangeChanged.connect(self._apply_pin)
        bar_v.valueChanged.connect(self._unpin_on_user_scroll)
        bar_v.valueChanged.connect(self._schedule_build)
        bar_v.rangeChanged.connect(self._schedule_build)
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
        section.expandedChanged.connect(self._schedule_build)
        if old is not None:
            index = self.sections_layout.indexOf(old)
            self.sections_layout.removeWidget(old)
            old.deleteLater()
        else:
            index = self.sections_layout.count() - 1  # before the stretch
        self.sections_layout.insertWidget(index, section)
        section.show()  # now, not on Qt's queued show, so layout can place it right away
        self._sections[analysis.target.name] = section
        self._update_status()
        self._schedule_build()
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
        # Settle the layout (and build the target) first, so its position is right now;
        # the pin keeps it right while later layout (a new scrollbar, say) moves it.
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        if section.expanded and self.scroll.viewport().isVisible():
            section.build()
            QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        if self._pinned is not None:
            self._pinned.removeEventFilter(self)
        self._pinned = section
        section.installEventFilter(self)  # re-pin whenever the section moves
        self._apply_pin()
        return True

    def eventFilter(self, obj, event):
        if obj is self._pinned and event.type() == QEvent.Type.Move:
            self._apply_pin()
        return super().eventFilter(obj, event)

    def _apply_pin(self, *_args) -> None:
        if self._pinned is None:
            return
        if self._pinned not in self._sections.values():
            self._pinned = None
            return
        self._pinning = True
        try:
            self.scroll.verticalScrollBar().setValue(self._pinned.y())
        finally:
            self._pinning = False

    def _unpin_on_user_scroll(self, *_args) -> None:
        if not self._pinning and self._pinned is not None:
            self._pinned.removeEventFilter(self)
            self._pinned = None

    # --- lazy figure building ---------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_build()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_build()

    def _schedule_build(self, *_args) -> None:
        self._build_timer.start()

    def _unbuilt_near_view(self, neighbours: bool) -> list[StarSection]:
        """Unbuilt, expanded sections in view, plus with ``neighbours`` the one above and below.

        Neighbours come by order, not distance, since one section can be taller than the view.
        """
        # Just-added sections have no real geometry until the pending layout requests run
        # (and the scroll area grows its content to fit them); run them now.
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        top = self.scroll.verticalScrollBar().value()
        bottom = top + self.scroll.viewport().height()
        sections = [s for s in self._sections.values() if s.isVisibleTo(self.scroll.widget())]
        in_view = [i for i, s in enumerate(sections)
                   if s.geometry().bottom() >= top and s.geometry().top() <= bottom]
        if not in_view:
            return []
        wanted = list(in_view)
        if neighbours:
            wanted += [i for i in (in_view[0] - 1, in_view[-1] + 1) if 0 <= i < len(sections)]
        return [sections[i] for i in wanted if not sections[i].built and sections[i].expanded]

    def build_visible(self) -> int:
        """Build every section now in view (not neighbours); returns how many were built."""
        if not self.scroll.viewport().isVisible():
            return 0
        in_view = self._unbuilt_near_view(neighbours=False)
        for section in in_view:
            section.build()
        return len(in_view)

    def _build_step(self) -> None:
        if self.build_visible():
            # building replaced placeholders; layout may shift a little, so look again
            self._build_timer.start()
            return
        if not self.scroll.viewport().isVisible():
            return
        nearby = self._unbuilt_near_view(neighbours=True)
        if nearby:
            nearby[0].build()
            if len(nearby) > 1:
                self._build_timer.start()

    def _update_status(self) -> None:
        n = len(self._sections)
        self.empty_label.setVisible(n == 0)
        self.save_all_button.setEnabled(any(has_plots(s.analysis) for s in self._sections.values()))
        planets = [p for s in self._sections.values() for p in s.analysis.planets]
        self.status.setText("" if n == 0 else
                            f"{n} star{'s' if n != 1 else ''}, " + plots.candidate_count_text(
                                len(planets), sum(1 for p in planets if p.is_duo_transit)))

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
