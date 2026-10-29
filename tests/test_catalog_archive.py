"""Exoplanet Archive queries and unit conversion, against recorded responses."""

import json
import math
from pathlib import Path

import pytest

from transitory.catalog import archive
from transitory.catalog.archive import ArchivePlanet, ArchiveResult, reference_text, tic_number
from transitory.catalog.client import CatalogClient, RecordedTransport, ResponseCache
from transitory.models import StarTarget

RECORDED = Path(__file__).parent / "data" / "catalog" / "recorded.json"

KEPLER10 = StarTarget(name="Kepler-10", tic=377780790, kic=11904151)
NULL_STAR = StarTarget(name="KIC 8006161", tic=123234496, kic=8006161)


@pytest.fixture
def client(tmp_path):
    return CatalogClient(transport=RecordedTransport(RECORDED), cache=ResponseCache(tmp_path / "catalog"))


def test_known_planets_are_the_transiting_confirmed_ones(client):
    result = archive.known_planets(KEPLER10, client=client)
    assert [p.name for p in result.planets] == ["Kepler-10 b", "Kepler-10 c"]  # d does not transit
    b = result.planets[0]
    assert b.table == "pscomppars" and b.host == "Kepler-10" and b.tic == 377780790
    assert b.period == pytest.approx(0.8374907)
    assert b.duration_h == pytest.approx(1.811)
    assert b.depth == pytest.approx(191.9e-6)  # pl_trandep is in percent
    assert b.rp_rs == pytest.approx(0.01268)
    assert b.ref.startswith("Bonomo et al. 2025 (https://ui.adsabs.harvard.edu/")
    assert result.retrieved == b.retrieved != ""
    assert result.queries and "pscomppars" in result.queries[0]


def test_known_planet_conversion(client):
    (b, c) = archive.known_planets(KEPLER10, client=client).known()
    assert b.name == "Kepler-10 b" and b.period == pytest.approx(0.8374907)
    assert b.depth == pytest.approx(191.9e-6) and b.rp_rs == pytest.approx(0.01268)
    assert "NASA Exoplanet Archive pscomppars, retrieved " in b.ref
    assert b.ref.startswith("Bonomo et al. 2025")


def test_all_signals_combines_confirmed_toi_and_koi(client):
    result = archive.all_signals(KEPLER10, client=client)
    tables = {p.table for p in result.planets}
    assert tables == {"pscomppars", "toi", "cumulative"}
    koi = next(p for p in result.planets if p.name == "K00072.01")
    assert koi.depth == pytest.approx(190.4e-6)  # koi_depth is in ppm
    assert koi.t0_bjd == pytest.approx(131.574858 + 2454833.0)  # BKJD -> BJD
    assert koi.kic == 11904151 and koi.disposition == "CONFIRMED"
    assert len(result.queries) == 3


def test_null_star_has_no_signals(client):
    assert archive.all_signals(NULL_STAR, client=client).planets == []


def test_tois_convert_ppm_and_derive_rp_rs(client):
    result = archive.tois(tic=259377017, client=client)
    assert [p.name for p in result.planets] == ["TOI-270.03", "TOI-270.01", "TOI-270.02"]  # by period
    first = result.planets[0]
    assert first.depth == pytest.approx(976.694e-6, rel=1e-5)  # TOI depth is in ppm
    assert first.duration_h == pytest.approx(1.3266673)
    # No ratio in the TOI table: Rp/Rs from Rp = 1.2235 R_earth, Rs = 0.37436 R_sun.
    assert first.rp_rs == pytest.approx(1.2234666 * 6371.0 / 695700.0 / 0.374358)
    assert first.disposition == "CP" and not first.is_false_positive


def test_query_text_escapes_names():
    seen = []

    def transport(method, url, params):
        seen.append(params["query"])
        return "[]"

    client = CatalogClient(transport=transport, cache=ResponseCache(None))
    archive.confirmed_planets(host="O'Neil's Star", transiting_only=True, client=client)
    assert "lower(hostname) = 'o''neil''s star'" in seen[0]
    assert seen[0].endswith("and tran_flag = 1")


def test_queries_need_an_identifier():
    with pytest.raises(ValueError):
        archive.confirmed_planets()
    with pytest.raises(ValueError):
        archive.kois()
    with pytest.raises(ValueError):
        archive.tois()


def test_koi_and_toi_number_queries():
    seen = []

    def transport(method, url, params):
        seen.append(params["query"])
        return "[]"

    client = CatalogClient(transport=transport, cache=ResponseCache(None))
    archive.kois(koi_number=72, client=client)
    archive.tois(toi_number=270, client=client)
    assert seen[0].endswith("where kepoi_name like 'K00072.%'")
    assert seen[1].endswith("where toipfx = '270'")


def test_false_positives_are_left_out_of_known():
    result = ArchiveResult(planets=[
        ArchivePlanet(name="K1.01", host="KIC 1", table="cumulative", period=1.0, disposition="CONFIRMED"),
        ArchivePlanet(name="K1.02", host="KIC 1", table="cumulative", period=2.0,
                      disposition="FALSE POSITIVE"),
        ArchivePlanet(name="TOI-9.01", host="TIC 9", table="toi", period=3.0, disposition="FP"),
    ])
    assert [k.name for k in result.known()] == ["K1.01"]
    assert len(result.known(include_false_positives=True)) == 3


def test_missing_period_becomes_nan():
    planet = ArchivePlanet(name="X b", host="X", table="pscomppars", period=None)
    assert math.isnan(planet.to_known().period)


def test_extend_keeps_the_oldest_retrieval_date():
    a = ArchiveResult(retrieved="2026-10-29", queries=["q1"])
    a.extend(ArchiveResult(retrieved="2026-10-01", queries=["q2"]))
    a.extend(ArchiveResult())
    assert a.retrieved == "2026-10-01" and a.queries == ["q1", "q2"]


def test_reference_text():
    link = ("<a refstr=X href=https://ui.adsabs.harvard.edu/abs/2020A&A...636A..98C/abstract "
            "target=ref>Cort&eacute;s-Zuleta et al. 2020</a>")
    assert reference_text(link) == ("Cortés-Zuleta et al. 2020 "
                                    "(https://ui.adsabs.harvard.edu/abs/2020A&A...636A..98C/abstract)")
    assert reference_text(None) == ""
    assert reference_text("Calculated Value") == "Calculated Value"


@pytest.mark.parametrize("value, expected", [("TIC 377780790", 377780790), (42, 42), ("", None),
                                             (None, None)])
def test_tic_number(value, expected):
    assert tic_number(value) == expected


def test_recorded_rows_have_the_columns_the_code_reads():
    """The recordings came from the live archive; keep them in step with the queries."""
    recordings = json.loads(RECORDED.read_text(encoding="utf-8"))
    columns = {c.strip() for c in archive.PSCOMPPARS_COLUMNS.split(",")}
    checked = 0
    for entry in recordings.values():
        if entry["params"].get("query", "").startswith(f"select {archive.PSCOMPPARS_COLUMNS} "):
            for row in json.loads(entry["body"]):
                assert row.keys() == columns
                checked += 1
    assert checked > 0
