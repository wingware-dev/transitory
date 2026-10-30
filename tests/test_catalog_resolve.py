"""Name parsing and resolution, against responses recorded from MAST and the Exoplanet Archive."""

from pathlib import Path

import pytest

from transitory.catalog.client import CatalogClient, CatalogError, RecordedTransport, ResponseCache
from transitory.catalog.tic import TIC_CREDIT, TicStar
from transitory.catalog.resolve import (ResolveError, parse_name, resolve, resolve_many,
                                        split_names)
from transitory.models import StarTarget

RECORDED = Path(__file__).parent / "data" / "catalog" / "recorded.json"


@pytest.fixture
def transport():
    return RecordedTransport(RECORDED)


@pytest.fixture
def client(transport, tmp_path):
    return CatalogClient(transport=transport, cache=ResponseCache(tmp_path / "catalog"))


@pytest.mark.parametrize("text, kind, display, number, host", [
    ("TIC 261136679", "tic", "TIC 261136679", 261136679, None),
    ("tic261136679", "tic", "TIC 261136679", 261136679, None),
    ("TIC-0042", "tic", "TIC 42", 42, None),
    ("KIC 8006161", "kic", "KIC 8006161", 8006161, None),
    ("kepid 8006161", "kic", "KIC 8006161", 8006161, None),
    ("EPIC 201367065", "epic", "EPIC 201367065", 201367065, None),
    ("KOI-72", "koi", "KOI-72", 72, None),
    ("koi 72.01", "koi", "KOI-72", 72, None),
    ("K00072.01", "koi", "KOI-72", 72, None),
    ("TOI-270", "toi", "TOI-270", 270, None),
    ("toi 270.02", "toi", "TOI-270", 270, None),
    ("261136679", "tic", "TIC 261136679", 261136679, None),
    ("Kepler-10 b", "planet", "Kepler-10 b", None, "Kepler-10"),
    ("WASP-18b", "planet", "WASP-18b", None, "WASP-18"),
    ("pi Men c", "planet", "pi Men c", None, "pi Men"),
    ("  HD  209458   b ", "planet", "HD 209458 b", None, "HD 209458"),
    ("Kepler-10", "name", "Kepler-10", None, None),
    ("Kepler-10 B", "name", "Kepler-10 B", None, None),  # capital: a stellar companion
    ("Vega", "name", "Vega", None, None),
    ("LHS 3844", "name", "LHS 3844", None, None),
])
def test_parse_name(text, kind, display, number, host):
    parsed = parse_name(text)
    assert (parsed.kind, parsed.text, parsed.number, parsed.host) == (kind, display, number, host)


def test_bare_number_is_a_kic_when_the_hint_is_kepler():
    assert parse_name("8006161", mission_hint="Kepler").kind == "kic"


def test_bare_number_is_an_epic_when_the_hint_is_k2():
    assert parse_name("201367065", mission_hint="K2").kind == "epic"


@pytest.mark.parametrize("text", ["", "   "])
def test_parse_empty_name_fails(text):
    with pytest.raises(ResolveError):
        parse_name(text)


def test_split_names():
    assert split_names("Kepler-10, TIC 261136679;pi Men\n\n ,LHS 3844") == [
        "Kepler-10", "TIC 261136679", "pi Men", "LHS 3844"]
    assert split_names(" , ; ") == []


# (typed, name, tic, kic, mission_hint)
RESOLVED = [
    ("TIC 261136679", "TIC 261136679", 261136679, None, "any"),
    ("KIC 8006161", "KIC 8006161", 123234496, 8006161, "Kepler"),
    ("EPIC 201367065", "EPIC 201367065", 173103335, None, "K2"),
    ("KOI-72", "KOI-72", 377780790, 11904151, "Kepler"),
    ("K00072.01", "KOI-72", 377780790, 11904151, "Kepler"),
    ("TOI-270.01", "TOI-270", 259377017, None, "TESS"),
    ("Kepler-10 b", "Kepler-10", 377780790, 11904151, "any"),
    ("Kepler-2", "Kepler-2", 424865156, 10666592, "any"),  # SIMBAD alias of HAT-P-7
    ("pi Men", "pi Men", 261136679, None, "any"),  # the archive calls it HD 39091
    ("pi Men c", "pi Men", 261136679, None, "any"),
    ("LHS 3844", "LHS 3844", 410153553, None, "any"),
    ("WASP-18b", "WASP-18", 100100827, None, "any"),
    ("EPIC 201912552", "EPIC 201912552", 388804061, None, "K2"),
    ("K2-18", "K2-18", 388804061, None, "any"),
]


@pytest.mark.parametrize("typed, name, tic, kic, hint", RESOLVED)
def test_resolve_recorded(client, typed, name, tic, kic, hint):
    target = resolve(typed, client=client)
    assert (target.name, target.tic, target.kic, target.mission_hint) == (name, tic, kic, hint)
    assert target.ra is not None and target.dec is not None
    assert target.tmag is not None and target.radius_rsun is not None


def test_resolved_values_come_from_the_tic(client):
    target = resolve("pi Men", client=client)
    assert target.ra == pytest.approx(84.29119, abs=1e-4)
    assert target.dec == pytest.approx(-80.46912, abs=1e-4)
    assert target.tmag == pytest.approx(5.105, abs=1e-3)
    assert target.radius_rsun == pytest.approx(1.149, abs=1e-3)
    assert target.label == "pi Men (TIC 261136679)"
    assert target.credits == [TIC_CREDIT]


@pytest.mark.parametrize("typed, epic", [
    ("EPIC 201912552", 201912552),
    ("EPIC 201367065", 201367065),
    ("K2-18", 201912552),  # from the archive's K2 planets and candidates table
    ("pi Men", None),  # a host K2 did not observe
    ("Kepler-10", None),  # a Kepler star, found by its KIC
])
def test_resolve_fills_the_epic(client, typed, epic):
    assert resolve(typed, client=client).epic == epic


def test_label_names_the_epic_when_there_is_no_tic_or_kic():
    assert StarTarget(name="K2-18", epic=201912552).label == "K2-18 (EPIC 201912552)"
    assert StarTarget(name="EPIC 201912552", epic=201912552).label == "EPIC 201912552"
    assert StarTarget(name="K2-18", tic=388804061, epic=201912552).label == "K2-18 (TIC 388804061)"


def test_tic_credit_only_with_stellar_parameters():
    assert TicStar(tic=1, ra=0.0, dec=0.0, tmag=9.0).credits == []
    assert TicStar(tic=1, ra=0.0, dec=0.0, teff_k=5000.0).credits == [TIC_CREDIT]


@pytest.mark.parametrize("typed, message", [
    ("TIC 99999999999", "not in the TESS Input Catalog"),
    ("nosuchstarxyz", "does not know it"),
    ("KOI-999999", "not in the cumulative KOI table"),
    ("TOI-999999", "not in the TOI table"),
])
def test_unknown_names_raise_resolve_error(client, typed, message):
    with pytest.raises(ResolveError, match=message):
        resolve(typed, client=client)


def test_second_resolve_needs_no_network(client, transport):
    first = resolve("Kepler-10 b", client=client)
    calls = len(transport.calls)
    assert calls > 0
    assert resolve("Kepler-10 b", client=client) == first
    assert len(transport.calls) == calls


def test_mission_hint_overrides_the_derived_one(client):
    assert resolve("KOI-72", mission_hint="TESS", client=client).mission_hint == "TESS"


def test_resolve_many_keeps_going_past_bad_names(client):
    targets, errors = resolve_many("Kepler-10 b, nosuchstarxyz; TOI-270.01", client=client)
    assert [t.name for t in targets] == ["Kepler-10", "TOI-270"]
    assert len(errors) == 1 and errors[0].startswith("nosuchstarxyz:")


def test_unreachable_service_is_a_catalog_error_not_a_miss(tmp_path):
    def down(method, url, params):
        raise CatalogError("connection refused")

    client = CatalogClient(transport=down, cache=ResponseCache(tmp_path))
    with pytest.raises(CatalogError):
        resolve("TIC 261136679", client=client)
    with pytest.raises(CatalogError):
        resolve_many("Kepler-10, pi Men", client=client)
