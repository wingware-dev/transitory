"""Summary table: one row per planet candidate (or per star with none), sortable, CSV export.

Values come from the fit when there is one, else from the BLS candidate
(Rp/Rs then is sqrt(depth), flagged in the notes).  Rows are colour-coded
by vetting confidence.
"""

from __future__ import annotations

import csv
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import plots
from ..models import DUO_TRANSIT, PlanetResult, StarAnalysis

log = logging.getLogger(__name__)

R_SUN_IN_R_EARTH = 109.076  # IAU nominal solar radius / equatorial Earth radius

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
NAN = float("nan")

ROW_TINTS = {"strong": "#e3f4e6", "weak": "#fdf0dc", DUO_TRANSIT: "#ebe8f3", "none": "#f0f0f0"}
CONFIDENCE_RANK = {"strong": 3, "weak": 2, DUO_TRANSIT: 1, "none": 0}


@dataclass
class ResultRow:
    star: str
    star_key: str  # StarTarget.name, to find the star's gallery section
    planet_index: int | None  # None: a star with no candidates
    period: float = NAN
    period_err: float = NAN
    rp_rs: float = NAN
    rp_rs_err: float = NAN
    rp_earth: float = NAN
    rp_earth_err: float = NAN
    t14_hours: float = NAN
    t14_err: float = NAN
    depth_ppm: float = NAN
    snr: float = NAN
    sde: float = NAN
    confidence: str = "none"
    notes: str = ""

    @property
    def planet(self) -> str:
        return "—" if self.planet_index is None else plots.planet_letter(self.planet_index)


def _finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def _err(errors: dict[str, float], key: str) -> float:
    value = errors.get(key)
    return float(value) if _finite(value) else NAN


def planet_row(analysis: StarAnalysis, index: int, planet: PlanetResult) -> ResultRow:
    cand, fit, vet = planet.candidate, planet.fit, planet.vetting
    row = ResultRow(star=analysis.target.label, star_key=analysis.target.name, planet_index=index,
                    snr=cand.snr, sde=cand.sde)
    notes: list[str] = []
    if fit is not None:
        u = fit.uncertainties
        row.period, row.period_err = fit.period, _err(u, "period")
        row.rp_rs, row.rp_rs_err = fit.rp_rs, _err(u, "rp_rs")
        row.t14_hours, row.t14_err = fit.duration_hours, _err(u, "duration_hours")
        row.depth_ppm = fit.depth * 1e6
    else:
        row.period = cand.period
        row.t14_hours = cand.duration * 24.0
        row.depth_ppm = cand.depth * 1e6
        row.rp_rs = math.sqrt(cand.depth) if cand.depth > 0 else NAN
        notes.append("no fit; Rp/Rs from depth")
    radius = analysis.target.radius_rsun
    if _finite(radius) and _finite(row.rp_rs):
        row.rp_earth = row.rp_rs * radius * R_SUN_IN_R_EARTH
        if _finite(row.rp_rs_err):
            row.rp_earth_err = row.rp_rs_err * radius * R_SUN_IN_R_EARTH
    if vet is not None:
        row.confidence = vet.confidence
        notes.extend(vet.reasons)
    else:
        notes.append("not vetted")
    # Vetting reasons usually state the count already ("17 transits, ...", "only 2 transits ...").
    count = f"{cand.n_transits} transit"
    if cand.n_transits and not any(count in reason for reason in notes):
        notes.insert(0, f"{count}{'s' if cand.n_transits != 1 else ''}")
    row.notes = "; ".join(notes)
    return row


def rows_for(analysis: StarAnalysis) -> list[ResultRow]:
    """Table rows for one star: one per planet, or a single row explaining why there are none."""
    rows = [planet_row(analysis, i, p) for i, p in enumerate(analysis.planets)]
    if analysis.errors:
        errors = "; ".join(analysis.errors)
        for row in rows:
            row.notes = f"{row.notes}; {errors}" if row.notes else errors
    if not rows:
        note = "; ".join(analysis.errors) if analysis.errors else "no transit signal found"
        rows.append(ResultRow(star=analysis.target.label, star_key=analysis.target.name,
                              planet_index=None, notes=note))
    source = analysis.raw.source if analysis.raw is not None else ""
    if source:
        for row in rows:
            row.notes = f"{row.notes}; data from {source}" if row.notes else f"data from {source}"
    return rows


def _num(value: float, digits: int) -> str:
    return f"{value:.{digits}f}" if _finite(value) else "—"


def _pm(value: float, err: float, digits: int) -> str:
    if not _finite(value):
        return "—"
    if _finite(err):
        return f"{value:.{digits}f} ± {err:.{digits}f}"
    return f"{value:.{digits}f}"


def _sig_digits(err: float, default: int, most: int = 7) -> int:
    """Decimals that show two significant figures of ``err``."""
    if not _finite(err) or err <= 0:
        return default
    return max(0, min(most, 1 - math.floor(math.log10(err))))


@dataclass
class Column:
    header: str
    display: Callable[[ResultRow], str]
    sort: Callable[[ResultRow], object]
    tooltip: str = ""


COLUMNS = [
    Column("Star", lambda r: r.star, lambda r: r.star.lower()),
    Column("#", lambda r: r.planet, lambda r: -1 if r.planet_index is None else r.planet_index,
           "Planet letter, in the order the search found them"),
    Column("P (d)", lambda r: _pm(r.period, r.period_err, _sig_digits(r.period_err, 5)),
           lambda r: r.period, "Orbital period ± 1σ"),
    Column("Rp/Rs", lambda r: _pm(r.rp_rs, r.rp_rs_err, _sig_digits(r.rp_rs_err, 4)),
           lambda r: r.rp_rs, "Planet-to-star radius ratio ± 1σ"),
    Column("Rp (R⊕)", lambda r: _pm(r.rp_earth, r.rp_earth_err, 2), lambda r: r.rp_earth,
           "Planet radius in Earth radii, when the stellar radius is known"),
    Column("T14 (h)", lambda r: _pm(r.t14_hours, r.t14_err, 2), lambda r: r.t14_hours,
           "Total transit duration"),
    Column("Depth (ppm)", lambda r: _num(r.depth_ppm, 0), lambda r: r.depth_ppm),
    Column("SNR", lambda r: _num(r.snr, 1), lambda r: r.snr, "Transit signal-to-noise ratio"),
    Column("SDE", lambda r: _num(r.sde, 1), lambda r: r.sde, "BLS signal detection efficiency"),
    Column("Confidence", lambda r: r.confidence, lambda r: CONFIDENCE_RANK.get(r.confidence, 0)),
    Column("Notes", lambda r: r.notes, lambda r: r.notes.lower()),
]
CONFIDENCE_COLUMN = 9
NUMERIC_COLUMNS = range(2, 9)

CSV_FIELDS = ["star", "planet", "period_d", "period_err_d", "rp_rs", "rp_rs_err", "rp_earth",
              "rp_earth_err", "t14_h", "t14_err_h", "depth_ppm", "snr", "sde", "confidence", "notes"]


def _csv_value(value) -> str:
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else ""
    return str(value)


def _csv_record(row: ResultRow) -> list[str]:
    values = [row.star, "" if row.planet_index is None else row.planet, row.period, row.period_err,
              row.rp_rs, row.rp_rs_err, row.rp_earth, row.rp_earth_err, row.t14_hours, row.t14_err,
              row.depth_ppm, row.snr, row.sde, row.confidence, row.notes]
    return [_csv_value(v) for v in values]


class ResultsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[ResultRow] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section].header
        if role == Qt.ItemDataRole.ToolTipRole:
            return COLUMNS[section].tooltip or None
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = self.rows[index.row()], COLUMNS[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return col.display(row)
        if role == SORT_ROLE:
            value = col.sort(row)
            # NaN sorts as "unknown": below every real value, whichever way the column sorts.
            return -math.inf if isinstance(value, float) and not math.isfinite(value) else value
        if role == Qt.ItemDataRole.BackgroundRole:
            if index.column() == CONFIDENCE_COLUMN:
                return QColor(plots.CONFIDENCE_COLORS.get(row.confidence, "#808080"))
            return QColor(ROW_TINTS.get(row.confidence, ROW_TINTS["none"]))
        if role == Qt.ItemDataRole.ForegroundRole:
            if index.column() == CONFIDENCE_COLUMN:
                return QColor("white")
            return QColor("#111111")
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in NUMERIC_COLUMNS:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == len(COLUMNS) - 1:
            return row.notes or None
        return None

    def set_rows(self, rows: list[ResultRow]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()


class ResultsTable(QWidget):
    """The Table tab: a sortable summary of every star's candidates, with CSV export."""

    planetActivated = Signal(str, int)  # star key (StarTarget.name), planet index (-1: none)
    exported = Signal(str)  # CSV path written

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("resultsTable")
        self._analyses: dict[str, StarAnalysis] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 0)
        self.status = QLabel()
        self.status.setObjectName("tableStatus")
        bar.addWidget(self.status, 1)
        self.export_button = QPushButton("Export CSV…")
        self.export_button.setObjectName("exportCsvButton")
        self.export_button.clicked.connect(self._export_interactive)
        bar.addWidget(self.export_button)
        layout.addLayout(bar)

        self.model = ResultsModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.view = QTableView()
        self.view.setObjectName("resultsView")
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        # Start in the order stars were added, until the user clicks a header.
        self.view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.proxy.sort(-1)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.verticalHeader().setVisible(False)
        self.view.setWordWrap(False)
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.view.doubleClicked.connect(self._activated)
        layout.addWidget(self.view, 1)
        self._refresh()

    # --- contents ---------------------------------------------------------

    def add_analysis(self, analysis: StarAnalysis) -> None:
        """Add a star's rows, replacing any it already has."""
        self._analyses[analysis.target.name] = analysis
        self._refresh()

    def set_analyses(self, analyses: list[StarAnalysis]) -> None:
        self._analyses = {a.target.name: a for a in analyses}
        self._refresh()

    def clear(self) -> None:
        self._analyses.clear()
        self._refresh()

    def rows(self) -> list[ResultRow]:
        """Rows in the order shown (current sort)."""
        return [self.model.rows[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
                for r in range(self.proxy.rowCount())]

    def _refresh(self) -> None:
        rows = [row for a in self._analyses.values() for row in rows_for(a)]
        self.model.set_rows(rows)
        found = sum(1 for r in rows if r.planet_index is not None)
        duos = sum(1 for r in rows if r.confidence == DUO_TRANSIT)
        strong = sum(1 for r in rows if r.confidence == "strong")
        n = len(self._analyses)
        self.status.setText("Planet candidates and confidence will appear here." if n == 0 else
                            f"{n} star{'s' if n != 1 else ''}, "
                            f"{plots.candidate_count_text(found, duos)} ({strong} strong)")
        self.export_button.setEnabled(bool(rows))

    def _activated(self, index) -> None:
        row = self.model.rows[self.proxy.mapToSource(index).row()]
        self.planetActivated.emit(row.star_key, -1 if row.planet_index is None else row.planet_index)

    # --- export -----------------------------------------------------------

    def export_csv(self, path: Path | str) -> Path:
        """Write the rows, in the order shown, as CSV with raw numbers (blank = unknown)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_FIELDS)
            for row in self.rows():
                writer.writerow(_csv_record(row))
        self.exported.emit(str(path))
        return path

    def _export_interactive(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export results as CSV", "transitory-results.csv",
                                              "CSV files (*.csv)")
        if not path:
            return
        try:
            self.export_csv(path)
        except OSError as exc:
            log.exception("CSV export to %s failed", path)
            QMessageBox.warning(self, "Export failed", f"Could not write {path}:\n{exc}")
            return
        self.status.setText(f"Exported {self.proxy.rowCount()} rows to {path}")
