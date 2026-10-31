"""Light-curve files by path on archive.stsci.edu and AWS: URLs, listings, ranking, failures.

No network: listings are replayed from ``tests/data/archive_files/recorded.json``
(re-record with ``record.py`` beside it).
"""

import json
from pathlib import Path

import pytest
import requests
from astropy.table import Table

from transitory.config import USER_AGENT
from transitory.pipeline import archive_files as A
from transitory.pipeline.fetch import select_products
from transitory.sources import SourceError

RECORDED = json.loads((Path(__file__).parent / "data" / "archive_files" / "recorded.json")
                      .read_text())

PI_MEN = 261136679
STSCI_S27 = "https://archive.stsci.edu/missions/tess/tid/s0027/0000/0002/6113/6679/"
S3_S27 = ("https://stpubdata.s3.amazonaws.com/?list-type=2"
          "&prefix=tess/public/tid/s0027/0000/0002/6113/6679/tess")


class Replay:
    """A transport answering from the recordings, with ``overrides`` first; records each URL."""

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        body = self.overrides[url] if url in self.overrides else RECORDED[url]
        if isinstance(body, Exception):
            raise body
        return None if body is None else body.encode("utf-8")


def without(url, text):
    """The recorded reply for ``url`` with the lines / S3 entries mentioning ``text`` taken out."""
    body = RECORDED[url]
    if "<Contents>" in body:
        head, *entries = body.split("<Contents>")
        last = entries[-1].split("</Contents>")
        entries[-1] = last[0] + "</Contents>"
        tail = last[1]
        return head + "".join("<Contents>" + e for e in entries if text not in e) + tail
    return "\n".join(line for line in body.splitlines() if text not in line)


# --- paths and URLs ---------------------------------------------------------------------

def test_directory_paths():
    assert A.kepler_dir(11904151) == "0119/011904151"
    assert A.k2_dir("c1", 201912552) == "c1/201900000/12000"
    assert A.k2_dir("c102", 201437844) == "c102/201400000/37000"
    assert A.tess_spoc_dir(1, PI_MEN) == "s0001/0000/0002/6113/6679"
    assert A.tess_hlsp_dir(85, PI_MEN) == "s0085/target/0000/0002/6113/6679"


def test_mirror_urls():
    stsci, s3 = A.MIRRORS["stsci"], A.MIRRORS["s3"]
    assert stsci.url("kepler", "0119/011904151", "kplr011904151-2009131105131_llc.fits") == (
        "https://archive.stsci.edu/missions/kepler/lightcurves/0119/011904151/"
        "kplr011904151-2009131105131_llc.fits")
    assert stsci.url("tess-spoc", A.tess_hlsp_dir(1, PI_MEN)) == (
        "https://archive.stsci.edu/hlsps/tess-spoc/s0001/target/0000/0002/6113/6679/")
    assert s3.url("k2", "c1/201900000/12000", "ktwo201912552-c01_llc.fits") == (
        "https://stpubdata.s3.amazonaws.com/k2/public/lightcurves/c1/201900000/12000/"
        "ktwo201912552-c01_llc.fits")
    assert s3.url("tess-spoc", A.tess_hlsp_dir(1, PI_MEN)) == (
        "https://stpubdata.s3.amazonaws.com/mast/hlsp/tess-spoc/s0001/target/0000/0002/6113/6679/")
    assert stsci.has_tess_spoc and s3.has_tess_spoc


def test_s3_listing_url_and_continuation():
    first = RECORDED[S3_S27].replace(
        "<IsTruncated>false</IsTruncated>",
        "<IsTruncated>true</IsTruncated><NextContinuationToken>a+b/c=</NextContinuationToken>")
    second = RECORDED["https://stpubdata.s3.amazonaws.com/?list-type=2"
                      "&prefix=tess/public/tid/s0001/0000/0002/6113/6679/tess"]
    replay = Replay({S3_S27: first, S3_S27 + "&continuation-token=a%2Bb/c%3D": second})
    names = A.MIRRORS["s3"].list("tess", A.tess_spoc_dir(27, PI_MEN), "tess", transport=replay)
    assert replay.urls == [S3_S27, S3_S27 + "&continuation-token=a%2Bb/c%3D"]
    # The second page's keys are under another directory, so are not this one's files.
    assert "tess2020186164531-s0027-0000000261136679-0189-s_lc.fits" in names
    assert not any("s0001" in n for n in names)


# --- parsers ----------------------------------------------------------------------------

def test_parse_apache_index():
    names = A.parse_apache_index(RECORDED[STSCI_S27])
    assert "tess2020186164531-s0027-0000000261136679-0189-s_lc.fits" in names
    assert "tess2020186164531-s0027-0000000261136679-0189-a_fast-lc.fits" in names
    assert not any("/" in n for n in names)  # not the parent directory


def test_parse_s3_listing():
    keys, token = A.parse_s3_listing(RECORDED[S3_S27])
    assert token is None
    assert "tess/public/tid/s0027/0000/0002/6113/6679/" \
           "tess2020186164531-s0027-0000000261136679-0189-a_fast-lc.fits" in keys
    keys, token = A.parse_s3_listing(RECORDED[
        "https://stpubdata.s3.amazonaws.com/?list-type=2"
        "&prefix=tess/public/tid/s0002/0000/0002/6113/6679/tess"])
    assert keys == [] and token is None


# --- products on both mirrors -----------------------------------------------------------

def same_products(mission, number, sectors=None):
    """The products found on each mirror, checked to be the same files."""
    found = {m: A.find_products(mission, number, mirror=m, sectors=sectors, transport=Replay())
             for m in A.MIRRORS}
    strip = [{k: v for k, v in p.items() if k not in ("url", "source")} for p in found["stsci"]]
    assert strip == [{k: v for k, v in p.items() if k not in ("url", "source")}
                     for p in found["s3"]]
    for mirror, products in found.items():
        assert all(p["source"] == mirror for p in products)
        assert all(p["url"].startswith(A.MIRRORS[mirror].base_url) for p in products)
        assert all(p["url"].endswith("/" + Path(p["file"]).name) for p in products)
    return found["stsci"]


def test_kepler_quarters_from_time_stamps():
    products = same_products("Kepler", 11904151)
    # Kepler-10 fell on the failed CCD module 3 in Q8, Q12 and Q16.
    assert [p["seq"] for p in products] == [0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 13, 14, 15, 17]
    q4 = products[4]
    assert q4 == {
        "file": "archive/Kepler/kplr011904151-2010009091648_llc.fits",
        "url": "https://archive.stsci.edu/missions/kepler/lightcurves/0119/011904151/"
               "kplr011904151-2010009091648_llc.fits",
        "source": "stsci", "author": "Kepler", "exptime": 1800.0, "seq": 4, "half": "",
        "label": "Kepler Quarter 04", "obs_id": "kplr011904151-2010009091648_llc",
        "product": "kplr011904151-2010009091648_llc", "target_name": "kplr011904151",
    }


def test_kepler_unknown_time_stamp_is_skipped(caplog):
    url = "https://archive.stsci.edu/missions/kepler/lightcurves/0119/011904151/"
    body = RECORDED[url].replace("2013131215648_llc", "2013999999999_llc")
    products = A.find_products("Kepler", 11904151, transport=Replay({url: body}))
    assert 17 not in [p["seq"] for p in products]
    assert "2013999999999" in caplog.text


def test_k2_campaign_probes_every_directory():
    replay = Replay()
    products = A.find_products("K2", 201912552, transport=replay)
    assert [(p["seq"], p["label"], p["file"]) for p in products] == [
        (1, "K2 Campaign 01", "archive/K2/ktwo201912552-c01_llc.fits")]
    # c0-c8, c102, c111, c112, c12-c19: campaign 9 has none, 10 and 11 are in halves.
    assert len(replay.urls) == len(A.K2_CAMPAIGN_DIRS) == 20
    assert same_products("K2", 201912552) == products


def test_k2_campaign_halves():
    # Campaign 10's first half was never released; c102 is its second.
    c10 = same_products("K2", 201437844)
    assert [(p["seq"], p["half"], p["label"]) for p in c10] == [(10, "b", "K2 Campaign 10b")]
    assert c10[0]["file"] == "archive/K2/ktwo201437844-c102_llc.fits"
    c11 = same_products("K2", 203008242)
    assert [(p["seq"], p["half"], p["product"]) for p in c11] == [
        (11, "a", "ktwo203008242-c111_llc"), (11, "b", "ktwo203008242-c112_llc")]


def test_tess_spoc_two_minute_and_missing_sector():
    products = same_products("TESS", PI_MEN, sectors=[27, 1, 2])
    assert [(p["seq"], p["author"], p["exptime"], p["label"]) for p in products] == [
        (1, "SPOC", 120.0, "TESS Sector 01"), (27, "SPOC", 120.0, "TESS Sector 27")]
    assert products[0]["obs_id"] == "tess2018206045859-s0001-0000000261136679-0120-s_lc"
    assert products[0]["target_name"] == "261136679"


def test_tess_needs_sectors():
    with pytest.raises(ValueError):
        A.find_products("TESS", PI_MEN)


# --- ranking ----------------------------------------------------------------------------

STSCI_HLSP_S27 = "https://archive.stsci.edu/hlsps/tess-spoc/s0027/target/0000/0002/6113/6679/"
S3_SPOC_S27 = ("https://stpubdata.s3.amazonaws.com/?list-type=2"
               "&prefix=mast/hlsp/tess-spoc/s0027/target/0000/0002/6113/6679/hlsp_")


def test_tess_ranking_without_two_minute_files():
    no_2min = {STSCI_S27: without(STSCI_S27, "s_lc.fits"), S3_S27: without(S3_S27, "s_lc.fits")}
    stsci = A.find_products("TESS", PI_MEN, sectors=[27], transport=Replay(no_2min))
    assert [(p["author"], p["exptime"], p["file"]) for p in stsci] == [
        ("TESS-SPOC", 600.0,
         "archive/TESS/hlsp_tess-spoc_tess_phot_0000000261136679-s0027_tess_v1_lc.fits")]
    assert stsci[0]["url"] == ("https://archive.stsci.edu/hlsps/tess-spoc/s0027/target/0000/0002/"
                               "6113/6679/hlsp_tess-spoc_tess_phot_0000000261136679-s0027_tess_v1_lc.fits")
    s3 = A.find_products("TESS", PI_MEN, sectors=[27], mirror="s3", transport=Replay(no_2min))
    assert [(p["author"], p["exptime"], p["file"]) for p in s3] == [
        (p["author"], p["exptime"], p["file"]) for p in stsci]
    assert s3[0]["url"] == ("https://stpubdata.s3.amazonaws.com/mast/hlsp/tess-spoc/s0027/target/"
                            "0000/0002/6113/6679/hlsp_tess-spoc_tess_phot_0000000261136679-s0027_tess_v1_lc.fits")
    # With no TESS-SPOC file either, the 20-second ones are next best.
    no_hlsp = dict(no_2min, **{STSCI_HLSP_S27: None, S3_SPOC_S27: without(S3_SPOC_S27, "hlsp_")})
    for mirror in A.MIRRORS:
        fast = A.find_products("TESS", PI_MEN, sectors=[27], mirror=mirror, transport=Replay(no_hlsp))
        assert [(p["author"], p["exptime"]) for p in fast] == [("SPOC", 20.0)]
        assert fast[0]["product"].endswith("-a_fast-lc")


def test_tess_spoc_cadence_by_sector():
    assert [A.tess_spoc_cadence_s(s) for s in (1, 26, 27, 55, 56, 85)] == \
        [1800.0, 1800.0, 600.0, 600.0, 200.0, 200.0]


def candidate(seq, author, exptime, half="", mission="TESS"):
    unit = {"TESS": "Sector", "Kepler": "Quarter", "K2": "Campaign"}[mission]
    return {"seq": seq, "half": half, "author": author, "exptime": float(exptime),
            "label": f"{mission} {unit} {seq:02d}{half}", "target_name": "x",
            "obs_id": f"{seq}{half}-{author}-{exptime}"}


@pytest.mark.parametrize("mission, records", [
    ("TESS", [candidate(5, "SPOC", 20), candidate(5, "TESS-SPOC", 600), candidate(5, "SPOC", 120),
              candidate(6, "SPOC", 20), candidate(6, "TESS-SPOC", 200), candidate(7, "SPOC", 20),
              candidate(8, "QLP", 600)]),
    ("K2", [candidate(10, "K2", 1800, "b", "K2"), candidate(11, "K2", 1800, "a", "K2"),
            candidate(11, "K2", 1800, "b", "K2"), candidate(12, "K2", 60, "", "K2")]),
    ("Kepler", [candidate(3, "Kepler", 1800, mission="Kepler"),
                candidate(3, "Kepler", 60, mission="Kepler")]),
])
def test_ranking_matches_select_products(mission, records):
    cols = ["mission", "author", "exptime", "target_name", "obs_id"]
    table = Table(rows=[[r["label"], r["author"], r["exptime"], r["target_name"], r["obs_id"]]
                        for r in records], names=cols)
    expected = [records[i]["obs_id"] for i in select_products(table, mission)]
    assert [r["obs_id"] for r in A.best_per_period(records)] == expected


# --- absent versus unreachable ----------------------------------------------------------

def test_missing_directory_is_no_data():
    for mirror in A.MIRRORS:
        assert A.find_products("TESS", PI_MEN, sectors=[2], mirror=mirror, transport=Replay()) == []


def test_unreachable_mirror_raises():
    timeout = A.SourceUnavailable("timed out")
    with pytest.raises(A.SourceUnavailable):
        A.find_products("TESS", PI_MEN, sectors=[1, 27], transport=Replay({STSCI_S27: timeout}))
    with pytest.raises(A.SourceUnavailable):
        A.find_products("TESS", PI_MEN, sectors=[27], mirror="s3",
                        transport=Replay({S3_S27: timeout}))


def test_s3_bucket_404_is_unreachable_not_no_data():
    with pytest.raises(A.SourceUnavailable):
        A.find_products("TESS", PI_MEN, sectors=[27], mirror="s3", transport=Replay({S3_S27: None}))


def test_garbled_listing_is_unreachable():
    with pytest.raises(A.SourceUnavailable):
        A.find_products("TESS", PI_MEN, sectors=[27], mirror="s3",
                        transport=Replay({S3_S27: "<html>busy</html"}))


class FakeReply:
    def __init__(self, status=200, chunks=(b"",), fail_after=None):
        self.status_code = status
        self.content = b"".join(chunks)
        self.chunks = list(chunks)
        self.fail_after = fail_after

    def iter_content(self, size):
        for n, chunk in enumerate(self.chunks):
            if n == self.fail_after:
                raise requests.ConnectionError("connection reset")
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGet:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize("result, expected", [
    (FakeReply(200, [b"listing"]), b"listing"),
    (FakeReply(404), None),
    (FakeReply(410), None),
])
def test_requests_transport_replies(monkeypatch, result, expected):
    get = FakeGet(result)
    monkeypatch.setattr(A.requests, "get", get)
    assert A.requests_transport("https://x/", 15.0) == expected
    assert get.calls[0][1]["headers"]["User-Agent"] == USER_AGENT
    assert get.calls[0][1]["timeout"] == 15.0


@pytest.mark.parametrize("result", [
    requests.Timeout("read timed out"), requests.ConnectionError("refused"),
    FakeReply(503), FakeReply(429), FakeReply(403),
])
def test_requests_transport_unreachable(monkeypatch, result):
    monkeypatch.setattr(A.requests, "get", FakeGet(result))
    with pytest.raises(A.SourceUnavailable):
        A.requests_transport("https://x/", 15.0)


# --- downloading ------------------------------------------------------------------------

def test_download_streams_to_dest(tmp_path):
    get = FakeGet(FakeReply(200, [b"SIMPLE", b"  =  T"]))
    dest = tmp_path / "archive" / "TESS" / "a.fits"
    assert A.download("https://x/a.fits", dest, get=get) == dest
    assert dest.read_bytes() == b"SIMPLE  =  T"
    url, kwargs = get.calls[0]
    assert url == "https://x/a.fits" and kwargs["stream"] is True
    assert kwargs["headers"]["User-Agent"] == USER_AGENT
    assert kwargs["timeout"] == (A.DOWNLOAD_CONNECT_TIMEOUT_SECONDS, A.DOWNLOAD_READ_TIMEOUT_SECONDS)
    assert list(dest.parent.iterdir()) == [dest]


def test_download_missing_file_is_not_unavailable(tmp_path):
    with pytest.raises(SourceError) as info:
        A.download("https://x/a.fits", tmp_path / "a.fits", get=FakeGet(FakeReply(404)))
    assert not isinstance(info.value, A.SourceUnavailable)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("result", [
    requests.Timeout("connect timed out"), FakeReply(503),
    FakeReply(200, [b"SIMPLE", b"more"], fail_after=1),
])
def test_download_failure_leaves_nothing(tmp_path, result):
    dest = tmp_path / "a.fits"
    with pytest.raises(A.SourceUnavailable):
        A.download("https://x/a.fits", dest, get=FakeGet(result))
    assert list(tmp_path.iterdir()) == []
