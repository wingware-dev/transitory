"""MastSource and the cache index, with lightkurve replaced by a fake (no network)."""

import threading
import time as _time
from pathlib import Path

import numpy as np
import pytest
from astropy.table import Table

from transitory.config import USER_AGENT, cache_dir
from transitory.models import LightCurve, StarTarget
from transitory.pipeline import cache as cache_mod
from transitory.pipeline import fetch
from transitory.pipeline.cache import CacheIndex, cache_key, load_json, save_json
from transitory.pipeline.fetch import DOIS, MastSource, mission_credits, select_products
from transitory.sources import LightCurveSource, SourceError


class SearchError(Exception):
    """Stands in for lightkurve.search.SearchError (matched by class name)."""


class FakeQuantity:
    def __init__(self, value):
        self.value = np.asarray(value, dtype=float)


class FakeLC:
    def __init__(self, time, flux, flux_err, filename):
        self.time = FakeQuantity(time)
        self.flux = FakeQuantity(flux)
        self.flux_err = FakeQuantity(flux_err)
        self.meta = {"FILENAME": filename}


def row(seq, author="SPOC", exptime=120, target="261136679", distance=0.0, mission="TESS"):
    label = f"{mission} {'Sector' if mission == 'TESS' else 'Quarter'} {seq:02d}"
    obs_id = f"{mission.lower()}-{author.lower()}-{target}-{seq:02d}-{exptime}"
    return {"mission": label, "author": author, "exptime": float(exptime), "target_name": target,
            "distance": float(distance), "obs_id": obs_id, "obs_collection": mission,
            "productFilename": obs_id + "_lc.fits"}


def make_table(rows):
    cols = ["mission", "author", "exptime", "target_name", "distance", "obs_id",
            "obs_collection", "productFilename"]
    return Table(rows=[[r[c] for c in cols] for r in rows], names=cols) if rows else Table()


class FakeSearchResult:
    def __init__(self, lk, table):
        self.lk = lk
        self.table = table

    def __getitem__(self, i):
        return FakeSearchResult(self.lk, self.table[i:i + 1])

    def download(self, quality_bitmask, download_dir, flux_column):
        return self.lk.download(self.table[0], quality_bitmask, download_dir, flux_column)


class FakeLightkurve:
    """Records searches, downloads and reads; serves per-file synthetic data."""

    def __init__(self, rows_by_mission=None):
        self.rows = rows_by_mission or {}
        self.searches = []
        self.downloads = []
        self.reads = []
        self.data = {}  # productFilename -> (time, pdcsap, sap)
        self.search_error = None
        self.search_delay = 0.0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def add_data(self, product, time, level, pdcsap_nan=False):
        rng = np.random.default_rng(len(self.data))
        flux = level * (1 + rng.normal(0, 1e-4, len(time)))
        pdc = np.full_like(flux, np.nan) if pdcsap_nan else flux
        self.data[product] = (np.asarray(time, float), pdc, flux * 1.01)

    def search_lightcurve(self, target, **kw):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.search_delay:
                _time.sleep(self.search_delay)
            self.searches.append((target, kw))
            if self.search_error is not None:
                raise self.search_error
            return FakeSearchResult(self, make_table(self.rows.get(kw["mission"], [])))
        finally:
            with self._lock:
                self.active -= 1

    def download(self, r, quality_bitmask, download_dir, flux_column):
        self.downloads.append((r["obs_id"], quality_bitmask, download_dir, flux_column))
        path = Path(download_dir, "mastDownload", r["obs_collection"], r["obs_id"], r["productFilename"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r["productFilename"])
        return self.read(str(path), flux_column=flux_column, quality_bitmask=quality_bitmask)

    def read(self, path, flux_column, quality_bitmask):
        self.reads.append((Path(path).name, flux_column, quality_bitmask))
        time, pdc, sap = self.data[Path(path).read_text()]
        flux = pdc if flux_column == "pdcsap_flux" else sap
        return FakeLC(time, flux, np.full_like(flux, 1e-4 * np.nanmedian(sap)), path)


@pytest.fixture
def fake_lk(monkeypatch):
    lk = FakeLightkurve()
    monkeypatch.setattr(fetch, "_lightkurve", lambda: lk)
    return lk


def tess_setup(lk):
    rows = [
        row(14, "SPOC", 120), row(14, "SPOC", 20), row(14, "TESS-SPOC", 600),
        row(15, "TESS-SPOC", 600),
        row(16, "SPOC", 20),
        row(14, "SPOC", 120, target="999", distance=30.0),  # a neighbour
    ]
    lk.rows["TESS"] = rows
    for i, r in enumerate(rows):
        seq = int(r["mission"].split()[-1])
        lk.add_data(r["productFilename"], np.arange(seq * 30.0, seq * 30.0 + 27, 0.1), 1000.0 * (i + 1))
    return rows


# --- product selection ---------------------------------------------------------------

def test_select_products_tess_prefers_spoc_2min_then_tess_spoc_then_20s():
    rows = [row(14, "SPOC", 20), row(14, "TESS-SPOC", 600), row(14, "SPOC", 120),
            row(15, "TESS-SPOC", 200), row(15, "SPOC", 20),
            row(16, "SPOC", 20), row(17, "QLP", 600),
            row(14, "SPOC", 120, target="999", distance=30.0)]
    picked = [rows[i] for i in select_products(make_table(rows), "TESS")]
    assert [(r["mission"], r["author"], r["exptime"]) for r in picked] == [
        ("TESS Sector 14", "SPOC", 120.0),
        ("TESS Sector 15", "TESS-SPOC", 200.0),
        ("TESS Sector 16", "SPOC", 20.0),
    ]


def test_select_products_kepler_long_cadence_only():
    rows = [row(q, "Kepler", 1800, target="kplr011904151", mission="Kepler") for q in (1, 2, 5)]
    rows.append(row(3, "Kepler", 60, target="kplr011904151", mission="Kepler"))
    picked = [rows[i]["mission"] for i in select_products(make_table(rows), "Kepler")]
    assert picked == ["Kepler Quarter 01", "Kepler Quarter 02", "Kepler Quarter 05"]


def test_select_products_empty():
    assert select_products(make_table([]), "TESS") == []


# --- fetching ----------------------------------------------------------------------------

def test_fetch_tess_stitches_normalised_sectors(fake_lk):
    tess_setup(fake_lk)
    source = MastSource()
    lc = source.fetch(StarTarget("Pi Men", tic=261136679, mission_hint="TESS"))

    assert fake_lk.searches == [("TIC 261136679", {"mission": "TESS", "author": ("SPOC", "TESS-SPOC")})]
    assert [d[0] for d in fake_lk.downloads] == [
        "tess-spoc-261136679-14-120", "tess-tess-spoc-261136679-15-600", "tess-spoc-261136679-16-20"]
    for _, bitmask, download_dir, column in fake_lk.downloads:
        assert (bitmask, column) == ("default", "pdcsap_flux")
        assert Path(download_dir) == cache_dir()

    assert lc.mission == "TESS"
    assert lc.sectors_or_quarters == [14, 15, 16]
    assert lc.author == "SPOC+TESS-SPOC"
    assert lc.dois == [DOIS["SPOC"], DOIS["TESS-SPOC"]]
    assert lc.source_ids == [d[0] + "_lc" for d in fake_lk.downloads]  # product file names
    assert lc.cadence_s == 120.0
    assert np.all(np.diff(lc.time) >= 0)
    for seq in (14, 15, 16):
        part = (lc.time >= seq * 30) & (lc.time < seq * 30 + 27)
        assert np.median(lc.flux[part]) == pytest.approx(1.0, abs=1e-6)
        assert np.median(lc.flux_err[part]) == pytest.approx(1.01e-4, rel=1e-3)
    assert isinstance(source, LightCurveSource)


def test_second_fetch_uses_cache_without_network(fake_lk):
    tess_setup(fake_lk)
    target = StarTarget("Pi Men", tic=261136679, mission_hint="TESS")
    first = MastSource().fetch(target)
    n_search, n_down = len(fake_lk.searches), len(fake_lk.downloads)

    second = MastSource().fetch(target)
    assert (len(fake_lk.searches), len(fake_lk.downloads)) == (n_search, n_down)
    np.testing.assert_array_equal(first.flux, second.flux)
    assert second.sectors_or_quarters == first.sectors_or_quarters
    assert second.dois == first.dois


def test_missing_cached_file_fetched_again(fake_lk):
    tess_setup(fake_lk)
    target = StarTarget("Pi Men", tic=261136679, mission_hint="TESS")
    MastSource().fetch(target)
    entry = CacheIndex().get(cache_key("lightcurve", "TESS", "TIC 261136679"))
    (cache_dir() / entry["products"][0]["file"]).unlink()
    MastSource().fetch(target)
    assert len(fake_lk.searches) == 2


def test_cache_index_paths_relative_to_cache_dir(fake_lk):
    tess_setup(fake_lk)
    MastSource().fetch(StarTarget("Pi Men", tic=261136679, mission_hint="TESS"))
    entry = CacheIndex().get(cache_key("lightcurve", "TESS", "TIC 261136679"))
    files = [p["file"] for p in entry["products"]]
    assert all(f.startswith("mastDownload/TESS/") for f in files)
    assert entry["query"] == "TIC 261136679" and entry["created"]


def test_kepler_by_kic(fake_lk):
    rows = [row(q, "Kepler", 1800, target="kplr011904151", mission="Kepler") for q in (1, 2)]
    for r in rows:  # as at MAST, all quarters share one obs_id
        r["obs_id"] = "kplr011904151_lc_Q111111111111111111"
    fake_lk.rows["Kepler"] = rows
    for r in rows:
        q = int(r["mission"].split()[-1])
        fake_lk.add_data(r["productFilename"], np.arange(q * 90.0, q * 90.0 + 80, 0.02), 5e4)
    lc = MastSource().fetch(StarTarget("Kepler-10", kic=11904151, mission_hint="any"))
    assert fake_lk.searches == [("KIC 11904151", {"mission": "Kepler", "author": "Kepler",
                                                  "exptime": "long"})]
    assert lc.mission == "Kepler"
    assert lc.sectors_or_quarters == [1, 2]
    assert lc.dois == [DOIS["Kepler"]]
    assert lc.cadence_s == 1800.0
    assert lc.source_ids == [r["productFilename"].removesuffix(".fits") for r in rows]


def test_any_mission_falls_back_to_tess(fake_lk):
    tess_setup(fake_lk)
    lc = MastSource().fetch(StarTarget("Some star", tic=261136679, kic=123))
    assert [s[1]["mission"] for s in fake_lk.searches] == ["Kepler", "TESS"]
    assert fake_lk.searches[0][0] == "KIC 123"
    assert lc.mission == "TESS"


def test_name_used_without_catalog_number(fake_lk):
    tess_setup(fake_lk)
    MastSource().fetch(StarTarget("Pi Mensae", mission_hint="TESS"))
    assert fake_lk.searches[0][0] == "Pi Mensae"


def test_pdcsap_missing_falls_back_to_sap(fake_lk):
    fake_lk.rows["TESS"] = [row(3)]
    fake_lk.add_data(row(3)["productFilename"], np.arange(0, 27, 0.1), 2000.0, pdcsap_nan=True)
    lc = MastSource().fetch(StarTarget("x", tic=261136679, mission_hint="TESS"))
    assert ("tess-spoc-261136679-03-120_lc.fits", "sap_flux", "default") in fake_lk.reads
    assert lc.author == "SPOC (SAP)"
    assert np.median(lc.flux) == pytest.approx(1.0, abs=1e-6)


def test_no_data_raises_and_is_cached_for_a_while(fake_lk, monkeypatch):
    target = StarTarget("Nowhere", tic=1, mission_hint="TESS")
    with pytest.raises(SourceError, match="No light curves at MAST"):
        MastSource().fetch(target)
    with pytest.raises(SourceError):
        MastSource().fetch(target)
    assert len(fake_lk.searches) == 1

    monkeypatch.setattr(fetch, "NEGATIVE_CACHE_DAYS", 0.0)
    with pytest.raises(SourceError):
        MastSource().fetch(target)
    assert len(fake_lk.searches) == 2


def test_unresolvable_name_is_no_data(fake_lk):
    fake_lk.search_error = SearchError("Could not resolve Notastar to a sky position.")
    with pytest.raises(SourceError, match="No light curves"):
        MastSource().fetch(StarTarget("Notastar", mission_hint="TESS"))


def test_search_failure_raises_source_error_and_caches_nothing(fake_lk):
    fake_lk.search_error = ConnectionError("MAST is down")
    target = StarTarget("x", tic=5, mission_hint="TESS")
    with pytest.raises(SourceError, match="MAST is down"):
        MastSource().fetch(target)
    assert CacheIndex().keys() == []


def test_max_products(fake_lk):
    tess_setup(fake_lk)
    lc = MastSource(max_products=2).fetch(StarTarget("x", tic=261136679, mission_hint="TESS"))
    assert lc.sectors_or_quarters == [14, 15]


def test_requests_are_serial_across_threads(fake_lk):
    fake_lk.search_delay = 0.05
    errors = []

    def work(i):
        try:
            MastSource().fetch(StarTarget(f"star {i}", tic=100 + i, mission_hint="TESS"))
        except SourceError:
            pass  # no data, as expected
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(fake_lk.searches) == 4
    assert fake_lk.max_active == 1


def test_progress_reported(fake_lk):
    tess_setup(fake_lk)
    calls = []
    MastSource().fetch(StarTarget("x", tic=261136679, mission_hint="TESS"),
                       progress_cb=lambda m, f: calls.append((m, f)))
    assert any("Downloading TESS Sector 15" in m for m, _ in calls)
    assert all(0.0 <= f <= 1.0 for _, f in calls)
    assert calls[-1][1] == 1.0


def test_user_agent_set_on_mast_session(fake_lk, monkeypatch):
    from astroquery.mast import Observations
    monkeypatch.setattr(fetch, "_user_agent_set", False)
    original = Observations._session.headers.get("User-Agent", "")
    monkeypatch.setitem(Observations._session.headers, "User-Agent", original)
    tess_setup(fake_lk)
    MastSource().fetch(StarTarget("x", tic=261136679, mission_hint="TESS"))
    agent = Observations._session.headers["User-Agent"]
    assert agent.startswith(USER_AGENT)
    assert agent.count(USER_AGENT) == 1


def test_credits():
    lc = LightCurve(np.zeros(1), np.ones(1), np.ones(1), "TESS", dois=[DOIS["SPOC"]])
    lines = MastSource().credits(lc)
    assert "TESS mission" in lines[0] and "NAS 5-26555" in lines[0]
    assert any("doi:10.17909/t9-nmc8-f686" in line for line in lines)
    assert any("Lightkurve" in line for line in lines)
    kepler = mission_credits(LightCurve(np.zeros(1), np.ones(1), np.ones(1), "Kepler",
                                        dois=[DOIS["Kepler"]]))
    assert "Kepler mission" in kepler[0]
    assert mission_credits(LightCurve(np.zeros(1), np.ones(1), np.ones(1), "Synthetic")) == []


def test_target_without_any_identifier():
    with pytest.raises(SourceError, match="No identifier"):
        MastSource().fetch(StarTarget("  ", mission_hint="TESS"))


# --- cache index -------------------------------------------------------------------------

def test_cache_key_normalised():
    assert cache_key("lightcurve", "TESS", "  TIC   261136679 ") == "lightcurve:tess:tic 261136679"
    assert cache_key("a", "Pi Men") == cache_key("A", "pi  men")


def test_cache_index_put_get_remove(tmp_path):
    index = CacheIndex(tmp_path)
    assert index.get("k") is None
    stored = index.put("k", {"products": [1, 2]})
    assert stored["created"]
    assert CacheIndex(tmp_path).get("k")["products"] == [1, 2]
    assert index.keys() == ["k"]
    index.remove("k")
    assert index.get("k") is None


def test_cache_index_age(tmp_path, monkeypatch):
    index = CacheIndex(tmp_path)
    index.put("k", {})
    assert index.get("k", max_age_days=1) is not None
    monkeypatch.setattr(cache_mod, "now_iso", lambda: "2000-01-01T00:00:00+00:00")
    index.put("old", {})
    assert index.get("old", max_age_days=1) is None
    assert index.get("old") is not None


def test_cache_index_corrupt_file_ignored(tmp_path, caplog):
    (tmp_path / "index.json").write_text("{not json")
    index = CacheIndex(tmp_path)
    assert index.get("k") is None
    index.put("k", {"x": 1})
    assert index.get("k")["x"] == 1
    assert "unreadable" in caplog.text


def test_cache_index_relative_paths(tmp_path):
    index = CacheIndex(tmp_path)
    inside = tmp_path / "mastDownload" / "f.fits"
    assert index.relative(inside) == "mastDownload/f.fits"
    assert index.absolute("mastDownload/f.fits") == tmp_path / "mastDownload" / "f.fits"
    outside = Path("/elsewhere/f.fits")
    assert index.absolute(index.relative(outside)) == outside.resolve()


def test_save_load_json(tmp_path):
    save_json("catalog", "Resolve:Pi Men", {"tic": 261136679}, root=tmp_path)
    data, created = load_json("catalog", "Resolve:Pi Men", root=tmp_path)
    assert data == {"tic": 261136679} and created
    assert load_json("catalog", "other", root=tmp_path) is None
    assert load_json("catalog", "Resolve:Pi Men", max_age_days=0, root=tmp_path) is None
