"""Selection panel: type star names, keep a list of chosen stars, submit them.

The entry box takes one name or a comma-separated list (planet names, TIC /
KIC / EPIC numbers, KOI / TOI, SIMBAD names); names resolve in a worker
thread through ``catalog.resolve.resolve_many``, and the ones that fail are
reported without dropping the rest.  Stars clicked in the sky view come in
through ``toggle_star``, which adds them or, when listed, removes them.  Submit emits the list with the chosen mission
preference applied.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..catalog.client import CatalogClient, CatalogError
from ..catalog.resolve import resolve_many
from ..catalog.skycatalog import SkyStar
from ..models import MissionHint, StarTarget
from .catalog_jobs import JobRunner

log = logging.getLogger(__name__)

# Resolver: text -> (targets, error messages), as resolve_many returns.
Resolver = Callable[[str], tuple[list[StarTarget], list[str]]]

MISSION_CHOICES: list[tuple[str, MissionHint | None]] = [
    ("Auto (each star's default)", None),
    ("TESS", "TESS"),
    ("Kepler", "Kepler"),
]

ENTRY_PLACEHOLDER = "Kepler-10, TIC 261136679, KOI-72, TOI-270, pi Men c…"


def target_key(target: StarTarget) -> str:
    """Identity for duplicate checks: the TIC when known, else the name."""
    return f"tic:{target.tic}" if target.tic is not None else f"name:{target.name.lower()}"


def describe_target(target: StarTarget) -> str:
    text = target.label
    extras = []
    if target.tmag is not None:
        extras.append(f"Tmag {target.tmag:.1f}")
    if target.mission_hint != "any":
        extras.append(target.mission_hint)
    return f"{text} — {', '.join(extras)}" if extras else text


class SelectionPanel(QWidget):
    targetsChanged = Signal(list)  # list[StarTarget]
    targetActivated = Signal(object)  # StarTarget, double-clicked in the list
    submitted = Signal(list)  # list[StarTarget], mission preference applied

    def __init__(self, client: CatalogClient | None = None, resolver: Resolver | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.resolver: Resolver = resolver or (lambda text: resolve_many(text, client=client))
        self._targets: list[StarTarget] = []
        self.runner = JobRunner(self)

        self.entry = QLineEdit()
        self.entry.setObjectName("starEntry")
        self.entry.setPlaceholderText(ENTRY_PLACEHOLDER)
        self.entry.setClearButtonEnabled(True)
        self.entry.setToolTip("Planet or star names, TIC / KIC / EPIC numbers, KOI or TOI; "
                              "separate several with commas")
        self.entry.returnPressed.connect(self.add_from_entry)
        self.add_button = QPushButton("Add")
        self.add_button.setObjectName("addButton")
        self.add_button.clicked.connect(self.add_from_entry)
        entry_row = QHBoxLayout()
        entry_row.addWidget(self.entry, 1)
        entry_row.addWidget(self.add_button)

        self.message = QLabel()
        self.message.setObjectName("selectionMessage")
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.list = QListWidget()
        self.list.setObjectName("selectedStars")
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.itemDoubleClicked.connect(
            lambda item: self.targetActivated.emit(item.data(Qt.ItemDataRole.UserRole)))
        QShortcut(QKeySequence.StandardKey.Delete, self.list, self.remove_selected,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence(Qt.Key.Key_Backspace), self.list, self.remove_selected,
                  context=Qt.ShortcutContext.WidgetShortcut)

        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("removeButton")
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("clearButton")
        self.clear_button.clicked.connect(self.clear)
        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self.remove_button)
        list_buttons.addWidget(self.clear_button)
        list_buttons.addStretch(1)

        self.mission = QComboBox()
        self.mission.setObjectName("missionChoice")
        for text, value in MISSION_CHOICES:
            self.mission.addItem(text, value)
        form = QFormLayout()
        form.addRow("Mission:", self.mission)

        self.submit_button = QPushButton("Submit")
        self.submit_button.setObjectName("submitButton")
        self.submit_button.setDefault(True)
        self.submit_button.clicked.connect(self.submit)

        self.count_label = QLabel()
        self.count_label.setObjectName("selectionCount")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Add stars by name or catalog number:"))
        layout.addLayout(entry_row)
        layout.addWidget(self.message)
        layout.addWidget(self.count_label)
        layout.addWidget(self.list, 1)
        layout.addLayout(list_buttons)
        layout.addLayout(form)
        layout.addWidget(self.submit_button)
        self._update_buttons()

    # -- the list ---------------------------------------------------------------------

    def targets(self) -> list[StarTarget]:
        return list(self._targets)

    def add_target(self, target: StarTarget) -> bool:
        """Append ``target`` unless the same star is listed; returns whether it was added."""
        key = target_key(target)
        if any(target_key(t) == key for t in self._targets):
            self._show_message(f"{target.label} is already in the list.")
            return False
        self._targets.append(target)
        item = QListWidgetItem(describe_target(target))
        item.setData(Qt.ItemDataRole.UserRole, target)
        self.list.addItem(item)
        self._changed()
        return True

    def add_star(self, star: SkyStar) -> bool:
        """Add a star clicked in the sky view."""
        added = self.add_target(star.to_target())
        if added:
            self._show_message(f"Added {star.name}.")
        return added

    def remove_target(self, target: StarTarget) -> bool:
        """Remove the listed star that is the same as ``target``; returns whether one was."""
        key = target_key(target)
        row = next((i for i, t in enumerate(self._targets) if target_key(t) == key), None)
        if row is None:
            return False
        self.list.takeItem(row)
        del self._targets[row]
        self._changed()
        return True

    def toggle_star(self, star: SkyStar) -> bool:
        """Add a star clicked in the sky view, or remove it if listed; returns whether it is listed now."""
        if self.remove_target(star.to_target()):
            self._show_message(f"Removed {star.name}.")
            return False
        return self.add_star(star)

    def remove_selected(self) -> None:
        rows = sorted((self.list.row(item) for item in self.list.selectedItems()), reverse=True)
        for row in rows:
            self.list.takeItem(row)
            del self._targets[row]
        if rows:
            self._changed()

    def clear(self) -> None:
        self.list.clear()
        self._targets.clear()
        self._changed()

    def _changed(self) -> None:
        self._update_buttons()
        self.targetsChanged.emit(self.targets())

    def _update_buttons(self) -> None:
        n = len(self._targets)
        self.count_label.setText(f"{n} star{'s' if n != 1 else ''} selected")
        self.remove_button.setEnabled(bool(self.list.selectedItems()))
        self.clear_button.setEnabled(n > 0)
        self.submit_button.setEnabled(n > 0)

    # -- typing ------------------------------------------------------------------------

    @property
    def resolving(self) -> bool:
        return self.runner.busy

    def add_from_entry(self) -> None:
        """Resolve what is typed in the entry box (in the background) and add it."""
        text = self.entry.text().strip()
        if not text or self.resolving:
            return
        self.entry.setEnabled(False)
        self.add_button.setEnabled(False)
        self._show_message(f"Looking up {text}…")
        self.runner.submit(lambda: self.resolver(text), self._resolved, self._resolve_failed)

    def _resolved(self, result: tuple[list[StarTarget], list[str]]) -> None:
        targets, errors = result
        added, duplicates = [], []
        for target in targets:
            (added if self.add_target(target) else duplicates).append(target.name)
        lines = []
        if added:
            lines.append(f"Added {', '.join(added)}.")
        if duplicates:
            lines.append(f"Already listed: {', '.join(duplicates)}.")
        lines += [f"Not found: {e}" for e in errors]
        self._show_message("\n".join(lines), error=bool(errors))
        self._entry_done(clear=not errors)

    def _resolve_failed(self, exc: Exception) -> None:
        if isinstance(exc, CatalogError):
            self._show_message(f"Catalog service unavailable: {exc}", error=True)
        else:
            log.error("resolving star names failed", exc_info=exc)
            self._show_message(f"Lookup failed: {exc}", error=True)
        self._entry_done(clear=False)

    def _entry_done(self, clear: bool) -> None:
        if clear:
            self.entry.clear()
        self.entry.setEnabled(True)
        self.add_button.setEnabled(True)
        self.entry.setFocus()

    def _show_message(self, text: str, error: bool = False) -> None:
        self.message.setText(text)
        self.message.setProperty("error", error)
        self.message.setStyleSheet("color: #c0392b;" if error else "")

    # -- submit ------------------------------------------------------------------------

    @property
    def mission_preference(self) -> MissionHint | None:
        return self.mission.currentData()

    def submit(self) -> list[StarTarget]:
        """Emit ``submitted`` with copies of the targets, mission preference applied."""
        preference = self.mission_preference
        targets = [replace(t, mission_hint=preference) if preference else replace(t)
                   for t in self._targets]
        if targets:
            self.submitted.emit(targets)
        return targets

    def closeEvent(self, event) -> None:
        self.runner.wait()
        super().closeEvent(event)
