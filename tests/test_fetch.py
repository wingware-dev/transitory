"""MastSource and the cache index, with lightkurve replaced by a fake (no network)."""

import http.server
import threading
import time as _time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
import requests
from astropy.table import Table

from transitory.config import USER_AGENT, cache_dir
from transitory.models import LightCurve, StarTarget
from transitory.pipeline import archive_files
from transitory.pipeline import cache as cache_mod
from transitory.pipeline import fetch, run
from transitory.pipeline.archive_files import MIRRORS, SourceUnavailable
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
        self.search_errors = []  # raised one per search, before search_error
        self.download_errors = []  # raised one per download
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
            if self.search_errors:
                raise self.search_errors.pop(0)
            if self.search_error is not None:
                raise self.search_error
            return FakeSearchResult(self, make_table(self.rows.get(kw["mission"], [])))
        finally:
            with self._lock:
                self.active -= 1

    def download(self, r, quality_bitmask, download_dir, flux_column):
        self.downloads.append((r["obs_id"], quality_bitmask, download_dir, flux_column))
        if self.download_errors:
            raise self.download_errors.pop(0)
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
    """lightkurve replaced, and the MAST search the only source (``mirrors`` adds the others)."""
    lk = FakeLightkurve()
    monkeypatch.setattr(fetch, "_lightkurve", lambda: lk)
    monkeypatch.setattr(fetch, "MAST_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setenv(fetch.LC_SOURCES_ENV, "search")
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
    assert lc.doi_sectors == {DOIS["SPOC"]: [14, 16], DOIS["TESS-SPOC"]: [15]}
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


def k2_row(campaign, exptime=1800, author="K2", target="ktwo211945201"):
    """A K2 search row; ``campaign`` is "05" or a half such as "10a", as lightkurve labels them."""
    obs_id = f"ktwo-{target}-c{campaign}-{exptime}"
    return {"mission": f"K2 Campaign {campaign}", "author": author, "exptime": float(exptime),
            "target_name": target, "distance": 0.0, "obs_id": obs_id, "obs_collection": "K2",
            "productFilename": obs_id + "_llc.fits"}


def test_select_products_k2_long_cadence_by_campaign_half():
    rows = [k2_row("05"), k2_row("05", exptime=60), k2_row("10a"), k2_row("10b"),
            k2_row("05", author="K2SFF"), k2_row("18", target="ktwo999")]
    rows[-1]["distance"] = 20.0  # a neighbour
    picked = [rows[i]["mission"] for i in select_products(make_table(rows), "K2")]
    assert picked == ["K2 Campaign 05", "K2 Campaign 10a", "K2 Campaign 10b"]


def test_k2_host_fetches_k2_campaigns(fake_lk):
    rows = [k2_row("05"), k2_row("10a"), k2_row("10b")]
    fake_lk.rows["K2"] = rows
    for n, r in enumerate(rows):
        fake_lk.add_data(r["productFilename"], np.arange(n * 100.0, n * 100.0 + 70, 0.02), 3e4)
    lc = MastSource().fetch(StarTarget("K2-334", tic=5, mission_hint="K2"))
    assert fake_lk.searches == [("K2-334", {"mission": "K2", "author": "K2", "exptime": "long"})]
    assert len(fake_lk.downloads) == 3  # both halves of campaign 10
    assert lc.mission == "K2"
    assert lc.sectors_or_quarters == [5, 10]
    assert lc.dois == [DOIS["K2"]]
    assert lc.cadence_s == 1800.0
    credits = mission_credits(lc)
    assert credits[0] == fetch.MAST_CREDITS["Kepler"]
    assert credits[1] == "K2 Light Curves (all): https://doi.org/10.17909/T9WS3R (campaigns 5, 10)"


def test_k2_search_is_not_blocked_by_an_old_kepler_no_data_entry(fake_lk):
    CacheIndex().put(cache_key("lightcurve", "Kepler", "K2-334"),
                     {"query": "K2-334", "mission": "Kepler", "products": []})
    fake_lk.rows["K2"] = [k2_row("05")]
    fake_lk.add_data(k2_row("05")["productFilename"], np.arange(0, 70, 0.02), 3e4)
    assert MastSource().fetch(StarTarget("K2-334", mission_hint="K2")).mission == "K2"


def test_any_mission_without_kic_tries_tess_then_k2(fake_lk):
    with pytest.raises(SourceError, match=r"searched TESS \(TIC 7\), K2 \(K2-18\)"):
        MastSource().fetch(StarTarget("K2-18", tic=7))
    assert [s[1]["mission"] for s in fake_lk.searches] == ["TESS", "K2"]


def test_k2_search_uses_the_epic_when_known(fake_lk):
    with pytest.raises(SourceError, match=r"searched TESS \(TIC 7\), K2 \(EPIC 201912552\)"):
        MastSource().fetch(StarTarget("K2-18", tic=7, epic=201912552))


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


# --- timeouts and retries ------------------------------------------------------------

def _read_timeout():
    return requests.ReadTimeout("HTTPSConnectionPool(host='mast.stsci.edu', port=443): "
                                "Read timed out. (read timeout=120.0)")


def test_search_timeout_is_tried_once_more(fake_lk):
    tess_setup(fake_lk)
    fake_lk.search_errors = [_read_timeout()]
    calls = []
    lc = MastSource().fetch(StarTarget("x", tic=261136679, mission_hint="TESS"),
                            progress_cb=lambda m, f: calls.append(m))
    assert len(fake_lk.searches) == 2
    assert lc.sectors_or_quarters == [14, 15, 16]
    assert any("timed out or could not connect; trying again" in m for m in calls)


def test_search_timing_out_twice_fails_the_star_without_caching(fake_lk):
    fake_lk.search_errors = [_read_timeout(), _read_timeout()]
    with pytest.raises(SourceError, match=r"timed out or could not connect \(tried twice\)"):
        MastSource().fetch(StarTarget("x", tic=5, mission_hint="TESS"))
    assert len(fake_lk.searches) == 2
    assert CacheIndex().keys() == []


def test_timed_out_star_is_a_per_star_error_in_the_analysis(fake_lk):
    fake_lk.search_errors = [_read_timeout(), _read_timeout()]
    result = run.analyze_star(StarTarget("Kepler-22", kic=10593626, mission_hint="Kepler"), MastSource())
    assert result.raw is None and not result.ok
    assert len(result.errors) == 1
    assert result.errors[0].startswith(
        "fetch: MAST is down or cannot be reached from this network: "
        "MAST search for 'KIC 10593626' timed out or could not connect (tried twice). "
        "This is not a lack of data for the star: try again later.")


def test_after_mast_is_found_down_other_stars_fail_at_once(fake_lk):
    fake_lk.search_errors = [_read_timeout(), _read_timeout()]
    source = MastSource()
    with pytest.raises(SourceError, match="MAST is down"):
        source.fetch(StarTarget("A", tic=5, mission_hint="TESS"))
    with pytest.raises(SourceError, match=r"^MAST is down or cannot be reached from this network: MAST "
                                          r"was not asked for 'TIC 6', as an earlier search in this run "
                                          r"found it unusable\. This is not a lack of data"):
        source.fetch(StarTarget("B", tic=6, mission_hint="TESS"))
    assert len(fake_lk.searches) == 2  # A's two tries; B never searched
    assert CacheIndex().keys() == []


def test_cached_stars_still_load_while_mast_is_down(fake_lk):
    tess_setup(fake_lk)
    source = MastSource()
    target = StarTarget("Pi Men", tic=261136679, mission_hint="TESS")
    source.fetch(target)
    fake_lk.search_errors = [_read_timeout(), _read_timeout()]
    with pytest.raises(SourceError):
        source.fetch(StarTarget("A", tic=5, mission_hint="TESS"))
    assert source.fetch(target).sectors_or_quarters == [14, 15, 16]


def test_a_new_run_or_the_hold_running_out_asks_mast_again(fake_lk, monkeypatch):
    source = MastSource()
    fake_lk.search_errors = [_read_timeout(), _read_timeout()]
    with pytest.raises(SourceError):
        source.fetch(StarTarget("A", tic=5, mission_hint="TESS"))
    source.new_run()
    with pytest.raises(SourceError, match="No light curves"):
        source.fetch(StarTarget("B", tic=6, mission_hint="TESS"))
    assert len(fake_lk.searches) == 3

    fake_lk.search_errors = [_read_timeout(), _read_timeout()]
    with pytest.raises(SourceError):
        source.fetch(StarTarget("C", tic=7, mission_hint="TESS"))
    monkeypatch.setattr(fetch, "MAST_OUTAGE_HOLD_SECONDS", 0.0)
    with pytest.raises(SourceError, match="No light curves"):
        source.fetch(StarTarget("D", tic=8, mission_hint="TESS"))
    assert len(fake_lk.searches) == 6


@pytest.mark.parametrize("error", [ValueError("bad table"), SearchError("Could not resolve x")])
def test_errors_about_one_star_do_not_stop_the_others(fake_lk, error):
    fake_lk.search_errors = [error]
    source = MastSource()
    with pytest.raises(SourceError):
        source.fetch(StarTarget("A", tic=5, mission_hint="TESS"))
    with pytest.raises(SourceError, match="No light curves"):
        source.fetch(StarTarget("B", tic=6, mission_hint="TESS"))
    assert len(fake_lk.searches) == 2


def test_rate_limit_also_holds_off_other_stars(fake_lk):
    fake_lk.search_errors = [_http_error(429)]
    source = MastSource()
    with pytest.raises(SourceError):
        source.fetch(StarTarget("A", tic=5, mission_hint="TESS"))
    with pytest.raises(SourceError, match=r"^MAST is limiting requests .* was not asked for 'TIC 6'"):
        source.fetch(StarTarget("B", tic=6, mission_hint="TESS"))
    assert len(fake_lk.searches) == 1


def test_server_errors_are_tried_once_more(fake_lk):
    tess_setup(fake_lk)
    reply = requests.Response()
    reply.status_code = 503
    fake_lk.search_errors = [requests.HTTPError("503 Server Error", response=reply)]
    MastSource().fetch(StarTarget("x", tic=261136679, mission_hint="TESS"))
    assert len(fake_lk.searches) == 2


def _http_error(status):
    reply = requests.Response()
    reply.status_code = status
    return requests.HTTPError(f"{status} Error", response=reply)


def test_server_errors_twice_say_mast_is_having_problems(fake_lk):
    fake_lk.search_errors = [_http_error(503), _http_error(503)]
    with pytest.raises(SourceError, match=r"^MAST is having problems \(HTTP 503\): .*"
                                          r"not a lack of data"):
        MastSource().fetch(StarTarget("x", tic=5, mission_hint="TESS"))
    assert CacheIndex().keys() == []


def test_rate_limit_is_not_retried_and_says_so(fake_lk):
    fake_lk.search_errors = [_http_error(429)]
    with pytest.raises(SourceError, match=r"^MAST is limiting requests from this machine "
                                          r"\(HTTP 429\): .*not a lack of data"):
        MastSource().fetch(StarTarget("x", tic=5, mission_hint="TESS"))
    assert len(fake_lk.searches) == 1
    assert CacheIndex().keys() == []


def test_mast_side_search_error_says_so(fake_lk):
    from astroquery.exceptions import RemoteServiceError

    fake_lk.search_error = RemoteServiceError(
        "Request failed.  If this problem persists, please notify archive[at]stsci.edu.")
    with pytest.raises(SourceError, match=r"^MAST reported an error on its side: .*not a lack of data"):
        MastSource().fetch(StarTarget("K2-334", mission_hint="K2"))
    assert len(fake_lk.searches) == 1
    assert CacheIndex().keys() == []


def test_other_search_errors_are_not_retried(fake_lk):
    fake_lk.search_error = ValueError("bad table")
    with pytest.raises(SourceError, match="failed: bad table"):
        MastSource().fetch(StarTarget("x", tic=5, mission_hint="TESS"))
    assert len(fake_lk.searches) == 1


def test_download_timeout_is_tried_once_more(fake_lk):
    tess_setup(fake_lk)
    fake_lk.download_errors = [_read_timeout()]
    lc = MastSource().fetch(StarTarget("x", tic=261136679, mission_hint="TESS"))
    assert len(fake_lk.downloads) == 4  # three sectors, the first one twice
    assert lc.sectors_or_quarters == [14, 15, 16]


def test_retry_waits_the_backoff(fake_lk, monkeypatch):
    sleeps = []
    monkeypatch.setattr(fetch, "MAST_RETRY_BACKOFF_SECONDS", 7.0)
    monkeypatch.setattr(fetch.time, "sleep", sleeps.append)
    fake_lk.search_errors = [_read_timeout(), _read_timeout()]
    with pytest.raises(SourceError):
        MastSource().fetch(StarTarget("x", tic=5, mission_hint="TESS"))
    assert sleeps == [7.0]


@pytest.fixture
def fresh_timeouts(monkeypatch):
    """Lets _ensure_timeouts run again, undoing what it changes afterwards."""
    from astroquery.mast import Observations
    from astroquery.mast import utils as mast_utils
    session = Observations._session
    monkeypatch.setattr(fetch, "_timeouts_set", False)
    monkeypatch.setattr(session, "adapters", type(session.adapters)(session.adapters))
    monkeypatch.setattr(mast_utils, "requests", requests)
    monkeypatch.setattr(Observations._portal_api_connection, "TIMEOUT",
                        Observations._portal_api_connection.TIMEOUT)
    return Observations


def test_mast_requests_get_a_timeout(fake_lk, fresh_timeouts):
    from astroquery.mast import utils as mast_utils
    session = fresh_timeouts._session
    tess_setup(fake_lk)
    MastSource().fetch(StarTarget("x", tic=261136679, mission_hint="TESS"))
    for url in ("https://mast.stsci.edu/api/v0/invoke", "http://mastresolver.stsci.edu/x"):
        assert session.get_adapter(url).timeout == fetch.MAST_TIMEOUT_SECONDS
    assert mast_utils.requests.timeout == fetch.MAST_TIMEOUT_SECONDS
    assert mast_utils.requests.Session is requests.Session
    assert fresh_timeouts._portal_api_connection.TIMEOUT == fetch.MAST_PORTAL_TIMEOUT_SECONDS


class _ExecutingAdapter(requests.adapters.BaseAdapter):
    """A MAST portal that answers every request "still executing"."""

    def __init__(self):
        super().__init__()
        self.sent = 0

    def send(self, request, **kwargs):
        self.sent += 1
        reply = requests.Response()
        reply.status_code = 200
        reply._content = b'{"status": "EXECUTING"}'
        reply.request = request
        reply.url = request.url
        return reply

    def close(self):
        pass


def test_search_stuck_executing_fails_the_star_after_the_portal_limit(
        fake_lk, fresh_timeouts, monkeypatch):
    from astroquery.mast import discovery_portal
    portal = fresh_timeouts._portal_api_connection
    session = requests.Session()
    adapter = _ExecutingAdapter()
    session.mount("https://", adapter)
    monkeypatch.setattr(portal, "_session", session)
    clock = [0.0]

    def fake_time():  # each poll takes 20 s of MAST's time
        clock[0] += 20.0
        return clock[0]

    monkeypatch.setattr(discovery_portal, "time", type("FakeTime", (), {"time": staticmethod(fake_time)}))

    def search_lightcurve(target, **kw):  # lightkurve's search, as far as the portal
        fake_lk.searches.append((target, kw))
        portal._request("POST", portal.MAST_REQUEST_URL, data="request=x")

    monkeypatch.setattr(fake_lk, "search_lightcurve", search_lightcurve)
    target = StarTarget("Kepler-22", kic=10593626, mission_hint="Kepler")
    result = run.analyze_star(target, MastSource())
    assert result.raw is None and not result.ok
    assert result.errors == ["fetch: MAST is overloaded or down: MAST search for 'KIC 10593626' "
                             "timed out. This is not a lack of data for the star: try again later. "
                             "(Timeout limit of 180.0 exceeded.)"]
    assert len(fake_lk.searches) == 1  # not tried again
    assert adapter.sent == 9
    assert CacheIndex().keys() == []


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        _time.sleep(2.0)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def slow_server():
    """A local server that takes 2 s to answer, standing in for a slow MAST."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def test_timeout_adapter_ends_a_request_sent_without_timeout(slow_server):
    session = requests.Session()
    session.mount("http://", fetch._TimeoutAdapter(0.3))
    start = _time.monotonic()
    with pytest.raises(requests.Timeout):
        session.get(slow_server)
    assert _time.monotonic() - start < 1.5


def test_timeout_requests_ends_a_get_sent_without_timeout(slow_server):
    start = _time.monotonic()
    with pytest.raises(requests.Timeout):
        fetch._TimeoutRequests(0.3).get(slow_server)
    assert _time.monotonic() - start < 1.5


def test_credits():
    lc = LightCurve(np.zeros(1), np.ones(1), np.ones(1), "TESS", dois=[DOIS["SPOC"]])
    lines = MastSource().credits(lc)
    assert "TESS mission" in lines[0] and "NAS5-26555" in lines[0]
    assert "TESS Light Curves - All Sectors: https://doi.org/10.17909/t9-nmc8-f686" in lines
    assert any("Lightkurve" in line for line in lines)
    kepler = mission_credits(LightCurve(np.zeros(1), np.ones(1), np.ones(1), "Kepler",
                                        dois=[DOIS["Kepler"]]))
    assert "Kepler mission" in kepler[0]
    tess = mission_credits(LightCurve(np.zeros(1), np.ones(1), np.ones(1), "TESS", dois=[DOIS["SPOC"]],
                                      sectors_or_quarters=[27, 1, 2, 3, 13, 28]))
    assert tess[1].endswith("https://doi.org/10.17909/t9-nmc8-f686 (sectors 1-3, 13, 27-28)")
    both = mission_credits(LightCurve(np.zeros(1), np.ones(1), np.ones(1), "TESS",
                                      dois=[DOIS["SPOC"], DOIS["TESS-SPOC"]], sectors_or_quarters=[2, 6, 9],
                                      doi_sectors={DOIS["SPOC"]: [2, 9], DOIS["TESS-SPOC"]: [6]}))
    assert both[1].endswith("t9-nmc8-f686 (sectors 2, 9)") and both[2].endswith("t9-wpz1-8s54 (sectors 6)")
    assert mission_credits(LightCurve(np.zeros(1), np.ones(1), np.ones(1), "Synthetic")) == []


def test_credits_say_where_the_files_were_retrieved_from():
    def lc(mission, source, retrieved="2026-10-30"):
        return LightCurve(np.zeros(1), np.ones(1), np.ones(1), mission, dois=[DOIS[{"TESS": "SPOC"}.get(
            mission, mission)]], source=source, retrieved=retrieved)

    static = mission_credits(lc("TESS", "STScI archive files"))
    assert static[2:] == ["Light curves retrieved on 2026-10-30 from STScI archive files "
                          "(https://archive.stsci.edu/missions/).", fetch.LIGHTKURVE_CREDIT]
    s3 = mission_credits(lc("Kepler", "AWS Open Data (stpubdata)"))
    assert s3[2:4] == ["Light curves retrieved on 2026-10-30 from AWS Open Data (s3://stpubdata).",
                       "Kepler Mission Data was accessed on 2026-10-30 from "
                       "https://registry.opendata.aws/mast-kepler."]
    both = fetch.retrieval_credits(lc("K2", "STScI archive files + AWS Open Data (stpubdata)", ""))
    assert both == ["Light curves retrieved from STScI archive files (https://archive.stsci.edu/missions/)"
                    " and AWS Open Data (s3://stpubdata).",
                    "K2 Mission Data was accessed on DATE from https://registry.opendata.aws/mast-k2."]
    assert fetch.retrieval_credits(lc("TESS", "MAST search")) == [
        "Light curves retrieved on 2026-10-30 from the MAST search (https://mast.stsci.edu)."]
    assert fetch.retrieval_credits(lc("TESS", "")) == []


def test_aws_credits_cite_the_tess_datasets_used():
    def lc(*authors):
        return LightCurve(np.zeros(1), np.ones(1), np.ones(1), "TESS", dois=[DOIS[a] for a in authors],
                          source="AWS Open Data (stpubdata)", retrieved="2026-10-30")

    tess = ("Transiting Exoplanet Survey Satellite (TESS) was accessed on 2026-10-30 from "
            "https://registry.opendata.aws/mast-tess.")
    ffi = ["TESS-SPOC was accessed on 2026-10-30 from https://registry.opendata.aws/mast-tess-spoc.",
           fetch.TESS_SPOC_REFERENCE]
    assert fetch.retrieval_credits(lc("SPOC"))[1:] == [tess]
    assert fetch.retrieval_credits(lc("TESS-SPOC"))[1:] == ffi
    assert fetch.retrieval_credits(lc("SPOC", "TESS-SPOC"))[1:] == [tess, *ffi]
    assert "Caldwell" in fetch.TESS_SPOC_REFERENCE


def test_target_without_any_identifier():
    with pytest.raises(SourceError, match="No identifier"):
        MastSource().fetch(StarTarget("  ", mission_hint="TESS"))


# --- the source chain: STScI archive files, the AWS mirror, then the search ---------

class _Reply:
    def __init__(self, status, body=b""):
        self.status_code, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        yield self.body


class FakeMirrors:
    """archive.stsci.edu and stpubdata: listings and downloads of ``files``, by URL.

    A file's content is its name, which ``FakeLightkurve.read`` serves data for.
    Mirrors named in ``down`` time out; ``download_failures`` fail that many downloads.
    """

    def __init__(self):
        self.files: dict[str, str] = {}
        self.down: set[str] = set()
        self.download_failures = 0
        self.listings: list[str] = []
        self.downloads: list[str] = []
        self.sector_lookups = 0
        self.sectors = [1, 4]

    @staticmethod
    def mirror_of(url):
        return "s3" if url.startswith(archive_files.S3_URL) else "stsci"

    def add(self, mirror, collection, path, name):
        self.files[MIRRORS[mirror].url(collection, path, name)] = name

    def transport(self, url, timeout):
        assert timeout == archive_files.LISTING_TIMEOUT_SECONDS
        mirror = self.mirror_of(url)
        self.listings.append(url)
        if mirror in self.down:
            raise SourceUnavailable(f"{url} could not be fetched: Read timed out")
        if mirror == "stsci":
            names = [u[len(url):] for u in self.files if u.startswith(url) and "/" not in u[len(url):]]
            return "".join(f'<a href="{n}">{n}</a>' for n in names).encode() if names else None
        prefix = parse_qs(urlsplit(url).query)["prefix"][0]
        keys = [u[len(archive_files.S3_URL) + 1:] for u in self.files if self.mirror_of(u) == "s3"]
        contents = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys if k.startswith(prefix))
        return (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>false</IsTruncated>{contents}</ListBucketResult>").encode()

    def get(self, url, headers, timeout, stream):
        self.downloads.append(url)
        if self.mirror_of(url) in self.down:
            raise requests.ConnectionError(f"{url}: connection refused")
        if self.download_failures:
            self.download_failures -= 1
            raise requests.ReadTimeout(f"{url}: Read timed out")
        name = self.files.get(url)
        return _Reply(404) if name is None else _Reply(200, name.encode())

    def sectors_for(self, ra, dec):
        self.sector_lookups += 1
        return self.sectors


KIC = 11904151
TIC = 261136679


def add_kepler(mirrors, lk, quarters=(1, 2), on=("stsci", "s3")):
    stamps = {q: stamp for stamp, q in archive_files.KEPLER_QUARTER_STAMPS.items()}
    for q in quarters:
        name = f"kplr{KIC:09d}-{stamps[q]}_llc.fits"
        for mirror in on:
            mirrors.add(mirror, "kepler", archive_files.kepler_dir(KIC), name)
        lk.add_data(name, np.arange(q * 90.0, q * 90.0 + 80, 0.02), 5e4)


def add_tess(mirrors, lk, sectors=(1, 4), on=("stsci", "s3")):
    for sector in sectors:
        name = f"tess2018206045859-s{sector:04d}-{TIC:016d}-0120-s_lc.fits"
        for mirror in on:
            mirrors.add(mirror, "tess", archive_files.tess_spoc_dir(sector, TIC), name)
        lk.add_data(name, np.arange(sector * 30.0, sector * 30.0 + 27, 0.1), 1e3)


KEPLER_10 = StarTarget("Kepler-10", kic=KIC, mission_hint="Kepler")
PI_MEN = StarTarget("pi Men", tic=TIC, ra=84.29, dec=-80.47, mission_hint="TESS")


@pytest.fixture
def mirrors(fake_lk, monkeypatch):
    """Every source in the default order, the mirrors faked by a FakeMirrors."""
    monkeypatch.delenv(fetch.LC_SOURCES_ENV)
    return FakeMirrors()


def chain_source(mirrors, **kw):
    return MastSource(transport=mirrors.transport, get=mirrors.get,
                      sectors_for=mirrors.sectors_for, **kw)


def test_lc_sources_default_and_override(monkeypatch, caplog):
    monkeypatch.delenv(fetch.LC_SOURCES_ENV, raising=False)
    assert fetch.lc_sources() == ["static", "s3", "search"]
    monkeypatch.setenv(fetch.LC_SOURCES_ENV, " S3, search,s3 ")
    assert fetch.lc_sources() == ["s3", "search"]
    monkeypatch.setenv(fetch.LC_SOURCES_ENV, "ftp")
    assert fetch.lc_sources() == ["static", "s3", "search"]
    assert "unknown source(s) ftp" in caplog.text


def test_static_files_are_used_and_cached(mirrors, fake_lk):
    add_kepler(mirrors, fake_lk)
    calls = []
    lc = chain_source(mirrors).fetch(KEPLER_10, progress_cb=lambda m, f: calls.append(m))
    assert lc.source == "STScI archive files" and lc.retrieved == cache_mod.now_iso()[:10]
    assert lc.mission == "Kepler" and lc.sectors_or_quarters == [1, 2]
    assert lc.dois == [DOIS["Kepler"]]
    assert all(u.startswith(archive_files.STSCI_URL) for u in mirrors.listings + mirrors.downloads)
    assert len(mirrors.downloads) == 2 and fake_lk.searches == []
    assert "Downloading Kepler Quarter 02 (2/2) from STScI archive files" in calls
    assert calls[-1] == "Kepler light curve ready (from STScI archive files)"

    entry = CacheIndex().get(cache_key("lightcurve", "Kepler", f"KIC {KIC}"))
    assert [p["source"] for p in entry["products"]] == ["stsci", "stsci"]
    assert all(p["file"].startswith("archive/Kepler/kplr") for p in entry["products"])
    assert all(p["url"] in mirrors.files for p in entry["products"])
    assert (cache_dir() / entry["products"][0]["file"]).exists()

    mirrors.down = {"stsci", "s3"}
    fake_lk.search_error = ConnectionError("MAST is down")
    n_listings = len(mirrors.listings)
    again = chain_source(mirrors).fetch(KEPLER_10)
    assert again.source == "STScI archive files" and again.retrieved == lc.retrieved
    np.testing.assert_array_equal(again.flux, lc.flux)
    assert len(mirrors.listings) == n_listings and fake_lk.searches == []


def test_static_unreachable_falls_back_to_s3(mirrors, fake_lk):
    add_kepler(mirrors, fake_lk)
    mirrors.down = {"stsci"}
    calls = []
    lc = chain_source(mirrors).fetch(KEPLER_10, progress_cb=lambda m, f: calls.append(m))
    assert lc.source == "AWS Open Data (stpubdata)"
    assert lc.sectors_or_quarters == [1, 2]
    assert all(u.startswith(archive_files.S3_URL) for u in mirrors.downloads)
    assert "https://registry.opendata.aws/mast-kepler." in mission_credits(lc)[-2]
    assert "STScI archive files unavailable; trying AWS Open Data (stpubdata)" in calls
    assert fake_lk.searches == []


def test_both_mirrors_unreachable_fall_back_to_the_search(mirrors, fake_lk):
    mirrors.down = {"stsci", "s3"}
    rows = [row(q, "Kepler", 1800, target="kplr011904151", mission="Kepler") for q in (1, 2)]
    fake_lk.rows["Kepler"] = rows
    for r in rows:
        fake_lk.add_data(r["productFilename"], np.arange(0, 80, 0.02), 5e4)
    calls = []
    lc = chain_source(mirrors).fetch(KEPLER_10, progress_cb=lambda m, f: calls.append(m))
    assert lc.source == "MAST search"
    assert [s[0] for s in fake_lk.searches] == [f"KIC {KIC}"]
    assert ("STScI archive files and AWS Open Data (stpubdata) unavailable; trying MAST search"
            in calls)
    entry = CacheIndex().get(cache_key("lightcurve", "Kepler", f"KIC {KIC}"))
    assert {p["source"] for p in entry["products"]} == {"search"}


def test_search_as_a_fallback_gets_one_short_try_and_the_error_lists_every_source(
        mirrors, fake_lk, monkeypatch):
    mirrors.down = {"stsci", "s3"}
    fake_lk.search_errors = [_read_timeout(), _read_timeout()]
    budgets = []

    @fetch.contextlib.contextmanager
    def budget(seconds):
        budgets.append(seconds)
        yield

    monkeypatch.setattr(fetch, "_search_budget", budget)
    with pytest.raises(SourceError) as info:
        chain_source(mirrors).fetch(KEPLER_10)
    message = str(info.value)
    assert message.startswith(f"No source could be used for Kepler light curves of 'KIC {KIC}': "
                              "STScI archive files: could not be reached (")
    assert "; AWS Open Data (stpubdata): could not be reached (" in message
    assert message.endswith("; MAST search: MAST is down or cannot be reached from this network. "
                            "This is not a lack of data for the star: try again later.")
    assert len(fake_lk.searches) == 1 and budgets == [fetch.MAST_FALLBACK_SEARCH_SECONDS]
    assert CacheIndex().keys() == []


def test_search_budget_shortens_and_restores_the_mast_limits(monkeypatch):
    adapter, portal = fetch._TimeoutAdapter(120.0), type("Portal", (), {"TIMEOUT": 180.0})()
    monkeypatch.setattr(fetch, "_limits", {"adapter": adapter, "portal": portal})
    with fetch._search_budget(60.0):
        assert (adapter.timeout, portal.TIMEOUT) == (60.0, 60.0)
    assert (adapter.timeout, portal.TIMEOUT) == (120.0, 180.0)
    with fetch._search_budget(None):
        assert adapter.timeout == 120.0


def test_a_missing_directory_is_no_data_and_does_not_fall_through(mirrors, fake_lk):
    source = chain_source(mirrors)
    with pytest.raises(SourceError, match="No light curves at MAST"):
        source.fetch(KEPLER_10)
    assert all(u.startswith(archive_files.STSCI_URL) for u in mirrors.listings)
    assert fake_lk.searches == []
    entry = CacheIndex().get(cache_key("lightcurve", "Kepler", f"KIC {KIC}"))
    assert entry["products"] == []
    n_listings = len(mirrors.listings)
    with pytest.raises(SourceError, match="No light curves at MAST"):
        source.fetch(KEPLER_10)
    assert len(mirrors.listings) == n_listings  # the negative cache answered


def test_an_unreachable_mirror_is_held_off_per_source(mirrors, fake_lk, monkeypatch):
    add_kepler(mirrors, fake_lk, on=("s3",))
    mirrors.down = {"stsci"}
    source = chain_source(mirrors)
    assert source.fetch(KEPLER_10).source == "AWS Open Data (stpubdata)"
    stsci = [u for u in mirrors.listings if u.startswith(archive_files.STSCI_URL)]
    assert len(stsci) == 1

    other = StarTarget("Kepler-22", kic=10593626, mission_hint="Kepler")
    with pytest.raises(SourceError, match="No light curves"):  # S3 answers for it: no data
        source.fetch(other)
    assert [u for u in mirrors.listings if u.startswith(archive_files.STSCI_URL)] == stsci
    assert fake_lk.searches == []

    source.new_run()
    mirrors.down = set()
    with pytest.raises(SourceError, match="No light curves"):
        source.fetch(StarTarget("KIC 3", kic=3, mission_hint="Kepler"))
    assert len([u for u in mirrors.listings if u.startswith(archive_files.STSCI_URL)]) == 2


def test_a_held_search_is_skipped_while_the_mirrors_still_work(mirrors, fake_lk):
    source = chain_source(mirrors)
    fake_lk.search_errors = [_read_timeout(), _read_timeout()]
    with pytest.raises(SourceError, match="MAST is down"):
        source.fetch(StarTarget("Some star", mission_hint="Kepler"))  # name only: search
    add_kepler(mirrors, fake_lk)
    assert source.fetch(KEPLER_10).source == "STScI archive files"
    with pytest.raises(SourceError, match="MAST was not asked for 'Other star'"):
        source.fetch(StarTarget("Other star", mission_hint="Kepler"))
    assert len(fake_lk.searches) == 2


def test_cached_stars_load_with_every_source_down(mirrors, fake_lk):
    add_tess(mirrors, fake_lk)
    source = chain_source(mirrors)
    first = source.fetch(PI_MEN)
    assert first.sectors_or_quarters == [1, 4] and first.source == "STScI archive files"
    mirrors.down = {"stsci", "s3"}
    fake_lk.search_error = ConnectionError("MAST is down")
    with pytest.raises(SourceError, match="not a lack of data"):
        source.fetch(StarTarget("Kepler-22", kic=10593626, mission_hint="Kepler"))
    n = (len(mirrors.listings), len(mirrors.downloads), mirrors.sector_lookups)
    assert source.fetch(PI_MEN).sectors_or_quarters == [1, 4]
    assert (len(mirrors.listings), len(mirrors.downloads), mirrors.sector_lookups) == n


def test_tess_files_found_in_the_covering_sectors(mirrors, fake_lk):
    add_tess(mirrors, fake_lk)
    lc = chain_source(mirrors).fetch(PI_MEN)
    assert lc.mission == "TESS" and lc.author == "SPOC" and lc.cadence_s == 120.0
    assert lc.source_ids == [f"tess2018206045859-s{s:04d}-{TIC:016d}-0120-s_lc" for s in (1, 4)]
    assert mirrors.sector_lookups == 1


def test_tess_with_no_covering_sector_is_no_data_without_listing(mirrors, fake_lk):
    mirrors.sectors = []
    with pytest.raises(SourceError, match="No light curves"):
        chain_source(mirrors).fetch(PI_MEN)
    assert mirrors.listings == [] and fake_lk.searches == []


def test_s3_without_tess_files_is_no_data(mirrors, fake_lk):
    mirrors.down = {"stsci"}
    tess_setup(fake_lk)
    with pytest.raises(SourceError, match="No light curves"):
        chain_source(mirrors).fetch(PI_MEN)
    assert fake_lk.searches == []  # S3 holds TESS-SPOC too, so its answer is believed


def test_tess_spoc_files_from_s3(mirrors, fake_lk):
    add_tess(mirrors, fake_lk, sectors=(1,))
    name = f"hlsp_tess-spoc_tess_phot_{TIC:016d}-s0004_tess_v1_lc.fits"
    mirrors.add("s3", "tess-spoc", archive_files.tess_hlsp_dir(4, TIC), name)
    fake_lk.add_data(name, np.arange(120.0, 147, 0.1), 1e3)
    mirrors.down = {"stsci"}
    lc = chain_source(mirrors).fetch(PI_MEN)
    assert lc.source == "AWS Open Data (stpubdata)" and fake_lk.searches == []
    assert lc.author == "SPOC+TESS-SPOC" and lc.sectors_or_quarters == [1, 4]
    assert lc.doi_sectors == {DOIS["SPOC"]: [1], DOIS["TESS-SPOC"]: [4]}
    assert fetch.TESS_SPOC_REFERENCE in mission_credits(lc)


def test_tess_sectors_that_cannot_be_looked_up_skip_the_mirrors(mirrors, fake_lk):
    def lookup(ra, dec):
        raise fetch.CatalogError("footprint file and TESScut both unreachable")

    tess_setup(fake_lk)
    source = MastSource(transport=mirrors.transport, get=mirrors.get, sectors_for=lookup)
    assert source.fetch(PI_MEN).source == "MAST search"
    assert mirrors.listings == []


def test_a_star_with_no_catalog_number_goes_straight_to_the_search(mirrors, fake_lk):
    tess_setup(fake_lk)
    fake_lk.search_errors = [_read_timeout()]  # the search is primary: tried once more
    calls = []
    lc = chain_source(mirrors).fetch(StarTarget("Pi Mensae", mission_hint="TESS"),
                                     progress_cb=lambda m, f: calls.append(m))
    assert lc.source == "MAST search" and len(fake_lk.searches) == 2
    assert mirrors.listings == [] and not any("unavailable" in m for m in calls)


def test_mirror_download_is_tried_once_more(mirrors, fake_lk):
    add_kepler(mirrors, fake_lk)
    mirrors.download_failures = 1
    lc = chain_source(mirrors).fetch(KEPLER_10)
    assert lc.source == "STScI archive files" and len(mirrors.downloads) == 3


def test_mirror_download_failing_twice_falls_back_and_reuses_what_was_fetched(mirrors, fake_lk):
    add_kepler(mirrors, fake_lk)
    stsci_q2 = [u for u in mirrors.files if u.startswith(archive_files.STSCI_URL)][1]
    original = mirrors.get

    def get(url, **kw):
        if url == stsci_q2:
            raise requests.ConnectionError("reset")
        return original(url, **kw)

    source = MastSource(transport=mirrors.transport, get=get, sectors_for=mirrors.sectors_for)
    lc = source.fetch(KEPLER_10)
    assert lc.source == "AWS Open Data (stpubdata)"
    # Quarter 1 came from STScI before quarter 2 failed; S3 gave only quarter 2.
    assert [u.startswith(archive_files.S3_URL) for u in mirrors.downloads] == [False, True]
    assert lc.sectors_or_quarters == [1, 2]


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
