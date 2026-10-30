"""Help ▸ Data Credits: the acknowledgements for the data and software used.

The general section lists what the dashboard can use (TESS and Kepler data
from MAST, the NASA Exoplanet Archive, Lightkurve / Astropy / Astroquery,
and the CDS hips2fits sky images and their surveys while the sky view shows
them, ``sky_background.SKY_IMAGES``);
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

from ..catalog.archive import ARCHIVE_CREDIT, ARCHIVE_REFERENCES
from ..catalog.hips import HIPS2FITS_CREDIT, SURVEY_CREDITS
from ..catalog.tic import TIC_CREDIT
from ..models import StarAnalysis
from ..pipeline.fetch import DOI_TITLES, LIGHTKURVE_CREDIT, MAST_CREDITS, doi_line
from . import sky_background

# As each package's citation page asks (checked 2026-10-29).  Astroquery is
# used for the MAST light-curve searches; the catalog queries are plain HTTP.
SOFTWARE_CREDITS = [
    LIGHTKURVE_CREDIT + " Lightkurve Collaboration et al. 2018, Lightkurve: Kepler and TESS time "
    "series analysis in Python, Astrophysics Source Code Library, ascl:1812.013.",
    "This work made use of Astropy (https://www.astropy.org), a community-developed core Python "
    "package and an ecosystem of tools and resources for astronomy (Astropy Collaboration 2013, "
    "A&A 558, A33; 2018, AJ 156, 123; 2022, ApJ 935, 167).",
    "This work made use of astroquery (Ginsburg, Sipőcz, Brasseur et al. 2019, AJ 157, 98).",
]


def sky_image_credits() -> list[tuple[str, list[str]]]:
    """(heading, lines) for the sky view's survey images."""
    return [
        ("Sky images (CDS hips2fits)", [HIPS2FITS_CREDIT]),
        ("Digitized Sky Survey (DSS2)", [SURVEY_CREDITS["dss2"]]),
        ("Two Micron All Sky Survey (2MASS)", [SURVEY_CREDITS["2mass"]]),
        ("Pan-STARRS1", [SURVEY_CREDITS["panstarrs"]]),
    ]


def general_credits() -> list[tuple[str, list[str]]]:
    """(heading, lines) for every data service and package the dashboard can use."""
    return [
        ("TESS data (MAST)", [MAST_CREDITS["TESS"]]),
        ("Kepler data (MAST)", [MAST_CREDITS["Kepler"]]),
        ("Mission data DOIs", [doi_line(doi) for doi in DOI_TITLES]),
        ("TESS Input Catalog (MAST)", [TIC_CREDIT]),
        ("NASA Exoplanet Archive", [ARCHIVE_CREDIT, *ARCHIVE_REFERENCES]),
        *(sky_image_credits() if sky_background.SKY_IMAGES else []),  # not while CDS is down
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
