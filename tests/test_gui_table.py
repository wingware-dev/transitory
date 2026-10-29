"""Results table driven headless with synthetic StarAnalysis objects."""

import csv
import math

import pytest
from PySide6.QtCore import Qt

from test_plots import make_analysis
from transitory.gui.results_table import (
    COLUMNS,
    CONFIDENCE_COLUMN,
    CSV_FIELDS,
    R_SUN_IN_R_EARTH,
    ResultsTable,
    rows_for,
)
from transitory.models import StarAnalysis, StarTarget

HEADERS = [c.header for c in COLUMNS]


def col(name):
    return HEADERS.index(name)


def analyses():
    two = make_analysis(name="Two Planet Star")
    two.target.radius_rsun = 0.9
    two.planets[1].vetting.confidence = "weak"
    nofit = make_analysis(name="Unfitted Star", with_fit=False)
    nofit.planets = nofit.planets[:1]
    empty = StarAnalysis(target=StarTarget(name="Quiet Star"), credits=[])
    failed = StarAnalysis(target=StarTarget(name="Missing Star"), errors=["fetch: no data"])
    return [two, nofit, empty, failed]


@pytest.fixture
def table(qtbot):
    widget = ResultsTable()
    qtbot.addWidget(widget)
    widget.resize(1200, 400)
    widget.show()
    qtbot.waitExposed(widget)
    widget.set_analyses(analyses())
    return widget


def cell(table, row, column, role=Qt.ItemDataRole.DisplayRole):
    return table.proxy.index(row, column).data(role)


def test_rows_columns_and_values(table):
    assert [table.proxy.headerData(i, Qt.Orientation.Horizontal) for i in range(len(COLUMNS))] == [
        "Star", "#", "P (d)", "Rp/Rs", "Rp (R⊕)", "T14 (h)", "Depth (ppm)", "SNR", "SDE",
        "Confidence", "Notes"]
    rows = table.rows()
    assert [(r.star_key, r.planet) for r in rows] == [
        ("Two Planet Star", "b"), ("Two Planet Star", "c"), ("Unfitted Star", "b"),
        ("Quiet Star", "—"), ("Missing Star", "—")]
    assert cell(table, 0, col("Star")) == "Two Planet Star (TIC 123456789)"
    assert cell(table, 0, col("P (d)")) == "3.21000 ± 0.00010"
    assert cell(table, 0, col("Rp/Rs")) == "0.1000 ± 0.0020"
    assert cell(table, 0, col("T14 (h)")) == "2.90"
    assert cell(table, 0, col("Depth (ppm)")) == "10000"
    assert cell(table, 0, col("SNR")) == "40.0" and cell(table, 0, col("SDE")) == "15.0"
    assert "8 transits" in cell(table, 0, col("Notes"))
    assert table.status.text() == "4 stars, 3 candidates (2 strong)"


def test_earth_radius_only_when_stellar_radius_known(table):
    rows = table.rows()
    assert rows[0].rp_earth == pytest.approx(0.1 * 0.9 * R_SUN_IN_R_EARTH)
    assert rows[0].rp_earth_err == pytest.approx(0.002 * 0.9 * R_SUN_IN_R_EARTH)
    assert cell(table, 0, col("Rp (R⊕)")) == "9.82 ± 0.20"
    assert math.isnan(rows[2].rp_earth)
    assert cell(table, 2, col("Rp (R⊕)")) == "—"


def test_unfitted_candidate_uses_bls_values(table):
    row = table.rows()[2]
    assert row.rp_rs == pytest.approx(0.1)  # sqrt(depth)
    assert row.t14_hours == pytest.approx(2.9)
    assert "no fit" in row.notes


def test_transit_count_noted_once():
    analysis = make_analysis()
    planet = analysis.planets[0]
    planet.vetting.reasons = ["SNR 497.6, SDE 24.3, 8 transits, odd/even consistent"]
    assert rows_for(analysis)[0].notes == "SNR 497.6, SDE 24.3, 8 transits, odd/even consistent"
    planet.candidate.n_transits = 1
    planet.vetting.reasons = ["only 1 transit observed"]
    assert rows_for(analysis)[0].notes == "only 1 transit observed"
    planet.vetting = None  # no reasons to carry it: the table adds the count
    assert rows_for(analysis)[0].notes == "1 transit; not vetted"


def test_stars_without_planets_explain_why():
    quiet, failed = analyses()[2:]
    assert rows_for(quiet)[0].notes == "no transit signal found"
    assert rows_for(failed)[0].notes == "fetch: no data"


def test_confidence_colour_coding(table):
    strong = cell(table, 0, CONFIDENCE_COLUMN, Qt.ItemDataRole.BackgroundRole)
    weak = cell(table, 1, CONFIDENCE_COLUMN, Qt.ItemDataRole.BackgroundRole)
    none = cell(table, 3, CONFIDENCE_COLUMN, Qt.ItemDataRole.BackgroundRole)
    assert strong.name() == "#1a9641" and weak.name() == "#e6860b" and none.name() == "#808080"
    assert cell(table, 0, 0, Qt.ItemDataRole.BackgroundRole).name() == "#e3f4e6"


def test_duo_transit_row_labelled_and_not_counted_as_candidate(table):
    star = make_analysis(name="Duo Star")
    star.planets[1].vetting.confidence = "duo-transit"
    table.set_analyses(analyses() + [star])
    duo = next(r for r in table.rows() if r.star_key == "Duo Star" and r.planet == "c")
    row = table.rows().index(duo)
    assert cell(table, row, CONFIDENCE_COLUMN) == "duo-transit"
    assert cell(table, row, CONFIDENCE_COLUMN, Qt.ItemDataRole.BackgroundRole).name() == "#8073ac"
    assert table.status.text() == "5 stars, 4 candidates, 1 duo-transit (3 strong)"
    # Ranked between weak and rejected.
    table.view.sortByColumn(CONFIDENCE_COLUMN, Qt.SortOrder.DescendingOrder)
    assert [r.confidence for r in table.rows() if r.planet_index is not None][-2:] == ["weak", "duo-transit"]


def test_sorts_numerically_with_unknowns_lowest(table, qtbot):
    header = table.view.horizontalHeader()
    qtbot.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                     pos=_section_centre(header, col("P (d)")))
    assert header.sortIndicatorSection() == col("P (d)")
    periods = [r.period for r in table.rows()]
    known = [p for p in periods if not math.isnan(p)]
    assert len(known) == 3
    if header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder:
        assert all(math.isnan(p) for p in periods[:2]) and known == sorted(known)
    else:
        assert all(math.isnan(p) for p in periods[-2:]) and known == sorted(known, reverse=True)
    table.view.sortByColumn(col("P (d)"), Qt.SortOrder.DescendingOrder)
    periods = [r.period for r in table.rows()]
    assert periods[:3] == [9.87, 3.21, 3.21] and all(math.isnan(p) for p in periods[3:])
    table.view.sortByColumn(CONFIDENCE_COLUMN, Qt.SortOrder.DescendingOrder)
    assert [r.confidence for r in table.rows()][:3] == ["strong", "strong", "weak"]


def _section_centre(header, logical):
    from PySide6.QtCore import QPoint

    x = header.sectionViewportPosition(logical) + header.sectionSize(logical) // 2
    return QPoint(x, header.height() // 2)


def test_replacing_a_star_keeps_one_set_of_rows(table):
    again = make_analysis(name="Two Planet Star")
    again.planets = again.planets[:1]
    table.add_analysis(again)
    assert [r.star_key for r in table.rows()].count("Two Planet Star") == 1
    table.clear()
    assert table.rows() == [] and not table.export_button.isEnabled()


def test_double_click_reports_star_and_planet(table, qtbot):
    index = table.proxy.index(1, 0)
    rect = table.view.visualRect(index)
    with qtbot.waitSignal(table.planetActivated) as blocker:
        # QTest's double-click sends no press first, which item views need to arm doubleClicked
        qtbot.mouseClick(table.view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        qtbot.mouseDClick(table.view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert blocker.args == ["Two Planet Star", 1]


def test_csv_export_in_shown_order(table, tmp_path):
    table.view.sortByColumn(col("P (d)"), Qt.SortOrder.DescendingOrder)
    path = table.export_csv(tmp_path / "out" / "results.csv")
    with path.open(newline="", encoding="utf-8") as fh:
        records = list(csv.reader(fh))
    assert records[0] == CSV_FIELDS
    first = dict(zip(CSV_FIELDS, records[1]))
    assert first["star"] == "Two Planet Star (TIC 123456789)" and first["planet"] == "c"
    assert float(first["period_d"]) == 9.87 and float(first["period_err_d"]) == 1e-4
    assert first["confidence"] == "weak"
    blank = dict(zip(CSV_FIELDS, records[-1]))
    assert blank["planet"] == "" and blank["period_d"] == "" and blank["rp_earth"] == ""
    assert len(records) == 1 + 5
