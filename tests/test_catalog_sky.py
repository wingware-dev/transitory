"""Sky catalog: planet hosts, TIC fields and the bundled offline targets.  No network."""

import json
import logging
from pathlib import Path

import pytest

from transitory.catalog import skycatalog, tic
from transitory.catalog.client import CatalogClient, CatalogError, RecordedTransport, ResponseCache
from transitory.catalog.skycatalog import (field_stars, host_stars, hosts_from_rows, offline_catalog,
                                           offline_known_planets, offline_targets)

RECORDED = Path(__file__).parent / "data" / "catalog" / "recorded.json"


@pytest.fixture
def client(tmp_path):
    return CatalogClient(transport=RecordedTransport(RECORDED), cache=ResponseCache(tmp_path / "catalog"))


@pytest.fixture
def down_client(tmp_path):
    def down(method, url, params):
        raise CatalogError("connection refused")

    return CatalogClient(transport=down, cache=ResponseCache(tmp_path / "down"))


def test_host_stars_groups_planets_by_host(client):
    catalog = host_stars(client=client)
    assert not catalog.offline and catalog.retrieved
    by_name = {s.name: s for s in catalog.stars}
    kepler10 = by_name["Kepler-10"]
    assert kepler10.planets == ["Kepler-10 b", "Kepler-10 c"]
    assert kepler10.tic == 377780790 and kepler10.kind == "host"
    assert kepler10.mission_hint == "Kepler"
    assert kepler10.teff_k == pytest.approx(5708, abs=200) and kepler10.logg == pytest.approx(4.34, abs=0.2)
    target = kepler10.to_target()
    assert (target.teff_k, target.logg, target.mass_msun) == (kepler10.teff_k, kepler10.logg,
                                                              kepler10.mass_msun)
    assert target.mass_msun == pytest.approx(0.91, abs=0.2)
    assert by_name["TOI-270"].n_planets == 3 and by_name["TOI-270"].mission_hint == "TESS"
    assert by_name["HD 39091"].planets == ["pi Men c"]
    assert by_name["WASP-18"].planets == ["WASP-18 b"]  # duplicate rows count once
    assert [s.name for s in catalog.stars] == sorted(by_name)


def test_host_stars_fall_back_to_bundled_targets(down_client, caplog):
    with caplog.at_level(logging.WARNING, logger="transitory.catalog.skycatalog"):
        catalog = host_stars(client=down_client)
    assert catalog.offline
    assert "connection refused" in catalog.note and "unreachable" in caplog.text
    assert {s.name for s in catalog.stars} >= {"Kepler-10", "TOI-270", "KIC 8006161"}


def test_host_stars_without_fallback_raise(down_client):
    with pytest.raises(CatalogError):
        host_stars(client=down_client, offline_fallback=False)


def test_hosts_from_rows_skips_rows_without_position():
    rows = [{"hostname": "A", "pl_name": "A b", "ra": None, "dec": 1.0},
            {"hostname": "B", "pl_name": "B b", "ra": 10.0, "dec": 1.0, "tic_id": "TIC 5",
             "sy_tmag": None, "sy_kepmag": 12.5, "disc_facility": "K2",
             "st_teff": 5000.0, "st_logg": 4.5, "st_mass": 0.8}]
    (star,) = hosts_from_rows(rows)
    assert (star.name, star.tic, star.tmag, star.mission_hint) == ("B", 5, 12.5, "Kepler")
    assert (star.teff_k, star.logg, star.mass_msun) == (5000.0, 4.5, 0.8)


def test_field_stars_brightest_first(client):
    stars = field_stars(285.6794, 50.2413, 0.1, client=client)
    assert len(stars) == 11
    mags = [s.tmag for s in stars]
    assert mags == sorted(mags) and max(mags) <= 13.0
    assert all(s.kind == "field" and s.name == f"TIC {s.tic}" for s in stars)
    kepler10 = next(s for s in stars if s.tic == 377780790)
    assert kepler10.kic == 11904151 and kepler10.teff_k and kepler10.logg and kepler10.mass_msun
    target = kepler10.to_target()
    assert (target.tic, target.kic, target.ra) == (377780790, 11904151, kepler10.ra)


def test_field_stars_unreachable_raise(down_client):
    with pytest.raises(CatalogError):
        field_stars(10.0, 20.0, 0.1, client=down_client)


def test_cone_radius_is_capped_and_position_rounded(tmp_path):
    seen = []

    def transport(method, url, params):
        seen.append(json.loads(params["request"]))
        return json.dumps({"status": "COMPLETE", "data": []})

    client = CatalogClient(transport=transport, cache=ResponseCache(tmp_path))
    tic.tic_cone(10.123456789, -20.987654321, 5.0, client=client)
    tic.tic_cone(10.123456789, -20.987654321, 0.2, max_tmag=12, client=client)
    assert seen[0]["service"] == "Mast.Catalogs.Tic.Cone"
    assert seen[0]["params"] == {"ra": 10.1235, "dec": -20.9877, "radius": tic.MAX_CONE_RADIUS_DEG}
    assert seen[1]["service"] == "Mast.Catalogs.Filtered.Tic.Position"
    assert seen[1]["params"]["filters"] == [{"paramName": "Tmag", "values": [{"min": -5, "max": 12.0}]}]


def test_nearest_star_prefers_the_bright_star_over_a_closer_faint_one(tmp_path):
    rows = [{"ID": 1, "ra": 0.0, "dec": 0.0, "Tmag": 15.0, "objType": "STAR", "dstArcSec": 0.3},
            {"ID": 2, "ra": 0.0, "dec": 0.0, "Tmag": 9.0, "objType": "STAR", "dstArcSec": 1.5},
            {"ID": 3, "ra": 0.0, "dec": 0.0, "Tmag": 5.0, "objType": "STAR", "dstArcSec": 8.0},
            {"ID": 4, "ra": 0.0, "dec": 0.0, "Tmag": 1.0, "objType": "EXTENDED", "dstArcSec": 0.1}]
    client = CatalogClient(transport=lambda m, u, p: json.dumps({"status": "COMPLETE", "data": rows}),
                           cache=ResponseCache(tmp_path))
    assert tic.nearest_star(0.0, 0.0, client=client).tic == 2


def test_offline_targets_are_the_validation_set():
    stars = offline_targets()
    names = [s.name for s in stars]
    assert names == ["Kepler-10", "Kepler-7", "Kepler-2", "Kepler-22", "pi Men", "LHS 3844",
                     "WASP-18", "TOI-270", "KIC 8006161"]
    for star in stars:
        assert star.tic and -90 <= star.dec <= 90 and 0 <= star.ra < 360
        assert star.mission_hint in ("Kepler", "TESS")
        assert star.teff_k and star.logg and star.mass_msun
    null = stars[-1]
    assert null.kind == "null" and null.planets == [] and null.kic == 8006161
    assert all(s.kind == "host" and s.planets for s in stars[:-1])


def test_offline_catalog_carries_its_date():
    catalog = offline_catalog()
    assert catalog.offline and catalog.retrieved == skycatalog.load_offline_data()["retrieved"]


def test_offline_known_planets():
    planets = offline_known_planets("toi-270")
    assert [p.name for p in planets] == ["TOI-270 b", "TOI-270 c", "TOI-270 d"]
    assert planets[0].period == pytest.approx(3.35992) and planets[0].depth > 0
    assert "pscomppars, retrieved" in planets[0].ref
    assert offline_known_planets("nowhere") == []


def test_offline_file_matches_the_recorded_archive(client):
    """The bundled positions and planets agree with the live data recorded for the tests."""
    by_name = {s.name: s for s in host_stars(client=client).stars}
    for entry in skycatalog.load_offline_data()["targets"]:
        if entry["role"] == "null" or entry["name"] in ("Kepler-2", "pi Men"):
            continue  # archive host names differ (HAT-P-7, HD 39091)
        host = by_name[entry["name"]]
        assert host.tic == entry["tic"]
        assert host.planets == [p["name"] for p in entry["planets"]]
