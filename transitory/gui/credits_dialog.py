"""Help ▸ Data Credits: the acknowledgements for the data and software used.

The general section lists what the dashboard can use (TESS and Kepler data
from MAST, the NASA Exoplanet Archive, Lightkurve / Astropy / Astroquery);
the session section collects the credit lines of each star analysed so far,
mission data DOIs included, so they can be copied into a paper or talk.
"""

from __future__ import annotations

import html

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..catalog.archive import ARCHIVE_CREDIT
from ..models import StarAnalysis
from ..pipeline.fetch import _MAST_CREDIT, DOI_TITLES

SOFTWARE_CREDITS = [
    "Lightkurve Collaboration et al. 2018, Lightkurve: Kepler and TESS time series analysis in "
    "Python, Astrophysics Source Code Library, ascl:1812.013.",
    "Astropy Collaboration et al. 2013, 2018, 2022, The Astropy Project, A&A 558, A33; AJ 156, 123; "
    "ApJ 935, 167.",
    "Ginsburg, Sipőcz, Brasseur et al. 2019, astroquery: An Astronomical Web-querying Package in "
    "Python, AJ 157, 98.",
]


def general_credits() -> list[tuple[str, list[str]]]:
    """(heading, lines) for every data service and package the dashboard can use."""
    dois = [f"{title}: doi:{doi}" for doi, title in DOI_TITLES.items()]
    return [
        ("TESS data (MAST)", [_MAST_CREDIT.format(mission="TESS")]),
        ("Kepler data (MAST)", [_MAST_CREDIT.format(mission="Kepler")]),
        ("Mission data DOIs", dois),
        ("NASA Exoplanet Archive", [ARCHIVE_CREDIT]),
        ("Software", SOFTWARE_CREDITS),
    ]


def session_credits(analyses: list[StarAnalysis]) -> list[str]:
    """The distinct credit lines of ``analyses``, in first-seen order."""
    seen: dict[str, None] = {}
    for analysis in analyses:
        for line in analysis.credits:
            seen.setdefault(line, None)
    return list(seen)


def credits_html(analyses: list[StarAnalysis]) -> str:
    parts = ["<h2>Data credits</h2>"]
    used = session_credits(analyses)
    if used:
        names = ", ".join(html.escape(a.target.name) for a in analyses if a.credits)
        parts.append(f"<h3>For the stars analysed in this session</h3><p><i>{names}</i></p>")
        parts.append("".join(f"<p>{html.escape(line)}</p>" for line in used))
    else:
        parts.append("<p><i>No stars analysed yet in this session; the credits for each "
                     "analysis appear here, and on every plot it produces.</i></p>")
    parts.append("<h3>Data and software this dashboard uses</h3>")
    for heading, lines in general_credits():
        parts.append(f"<p><b>{html.escape(heading)}</b></p>")
        parts.append("".join(f"<p style='margin-left: 12px'>{html.escape(line)}</p>" for line in lines))
    return "".join(parts)


def credits_text(analyses: list[StarAnalysis]) -> str:
    """Plain text of the dialog, for the clipboard."""
    lines = session_credits(analyses)
    for _, general in general_credits():
        lines += [line for line in general if line not in lines]
    return "\n\n".join(lines)


class CreditsDialog(QDialog):
    def __init__(self, analyses: list[StarAnalysis], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("creditsDialog")
        self.setWindowTitle("Data Credits")
        self.resize(720, 640)
        self.analyses = list(analyses)

        self.browser = QTextBrowser()
        self.browser.setObjectName("creditsText")
        self.browser.setOpenExternalLinks(True)
        self.browser.setHtml(credits_html(self.analyses))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.copy_button = QPushButton("Copy as text")
        self.copy_button.setObjectName("copyCreditsButton")
        self.copy_button.clicked.connect(self.copy)
        buttons.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)

        layout = QVBoxLayout(self)
        layout.addWidget(self.browser, 1)
        layout.addWidget(buttons)

    def copy(self) -> None:
        QApplication.clipboard().setText(credits_text(self.analyses))
