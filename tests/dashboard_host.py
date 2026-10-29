"""Drive a demo-mode dashboard end to end: pick stars, submit, wait for results.

Used by tests/test_gui_e2e.py and the 03–06 screenshots in tools/take_shots.py.
Everything runs on ``SyntheticSource``, so nothing touches the network.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from harness import make_main_window

# Stars in the gallery / table shots: a strong planet, two planets, an
# eclipsing binary (rejected) and a quiet star (nothing found).
RESULT_STARS = ("Demo Hot Jupiter", "Demo Two-Planet", "Demo Eclipsing Binary", "Demo Quiet Star")


def wait_until(condition, timeout_s: float = 120.0, step_ms: int = 20) -> None:
    """Run the event loop until ``condition()`` holds; TimeoutError after ``timeout_s``.

    Spins a real QEventLoop rather than ``QTest.qWait``: PySide's qWait keeps
    the GIL while it sleeps, which starves the analysis threads (a 3 s
    analysis took minutes).
    """
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"condition not met within {timeout_s:.0f} s")
        loop = QEventLoop()
        QTimer.singleShot(step_ms, loop.quit)
        loop.exec()


def click_star(window, name: str) -> None:
    """Click the demo star ``name`` on the sky, as the user would."""
    star = next(s for s in window.sky.hosts if s.name == name)
    window.sky.click_at(star.ra, star.dec)


def type_stars(window, text: str, timeout_s: float = 10.0) -> None:
    """Type ``text`` in the selection entry, press Enter and wait for the lookup."""
    panel = window.panel
    QTest.keyClicks(panel.entry, text)
    QTest.keyClick(panel.entry, Qt.Key.Key_Return)
    wait_until(lambda: not panel.resolving and panel.entry.isEnabled(), timeout_s)


def select_and_submit(window, names, timeout_s: float = 10.0) -> None:
    """The first star by clicking it on the sky, the rest typed in; then press Submit."""
    names = list(names)
    click_star(window, names[0])
    if names[1:]:
        type_stars(window, ", ".join(names[1:]), timeout_s)
    QTest.mouseClick(window.panel.submit_button, Qt.MouseButton.LeftButton)


def wait_for_results(window, timeout_s: float = 120.0) -> None:
    wait_until(lambda: not window.runner.busy, timeout_s)
    QApplication.processEvents()


def analysed_window(names=RESULT_STARS, tab: str = "Gallery"):
    """A shown demo window whose ``names`` have been analysed, on ``tab``."""
    window = make_main_window()
    select_and_submit(window, names)
    wait_for_results(window)
    window.tabs.setCurrentIndex([window.tabs.tabText(i) for i in range(window.tabs.count())].index(tab))
    QApplication.processEvents()
    return window


# -- screenshots ---------------------------------------------------------------

def shot_running(app):
    """Three stars submitted on two workers: one done, one running, one queued."""
    window = make_main_window()
    select_and_submit(window, ("Demo Hot Jupiter", "Demo Super-Earth", "Demo Two-Planet"))
    wait_until(lambda: len(window.gallery.sections()) >= 1)
    wait_until(lambda: any("%" in (window.progress.status(n) or "")
                           for n in window.runner.pending_names()), 30.0)
    return window


def shot_gallery(app):
    return analysed_window(tab="Gallery")


def shot_table(app):
    return analysed_window(tab="Table")


def shot_credits(app):
    window = analysed_window(("Demo Hot Jupiter",))
    dialog = window.show_credits()
    dialog.finished.connect(window.close)
    app.processEvents()
    return dialog
