"""Re-record the catalog responses the tests replay (needs network; free public services).

    python tests/data/catalog/record.py           # everything
    python tests/data/catalog/record.py fields    # just the 1° Kepler-10 fields

Runs the lookups the catalog tests make against MAST and the NASA Exoplanet
Archive with an empty cache and writes every response to ``recorded.json``
beside this file.  TIC rows are trimmed to the columns the code reads and
the all-hosts query to the validation hosts plus a few others, to keep the
file small.  Tests that check exact values may need updating after a
re-recording, since archive values are revised over time.

The sky view asks for a 1° TIC field (Tmag ≤ 13) when it goes to a star:
for Kepler-10 at its host-catalog position (clicked or listed from the sky)
and at its TIC position (typed).  Both are recorded, with values rounded
to keep them small, and where they hold the same stars the second is kept
as ``same_as`` the first; ``fields`` re-records only those, keeping the
rest of ``recorded.json``.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from transitory.catalog import archive, client as C, resolve as R, skycatalog as S  # noqa: E402
from transitory.catalog.tic import MAX_CONE_RADIUS_DEG  # noqa: E402
from transitory.gui.sky_view import FIELD_MAX_TMAG  # noqa: E402

OUTPUT = Path(__file__).resolve().parent / "recorded.json"

TIC_KEEP = ["ID", "ra", "dec", "Tmag", "KIC", "rad", "Teff", "logg", "mass", "objType", "dstArcSec"]
FIELD_DECIMALS = {"ra": 6, "dec": 6, "Tmag": 3, "rad": 3, "Teff": 0, "logg": 3, "mass": 3,
                  "dstArcSec": 2}  # 6 decimals of a degree is ~4 mas
HOSTS_KEEP = {"Kepler-10", "Kepler-7", "HAT-P-7", "Kepler-22", "HD 39091", "LHS 3844", "WASP-18",
              "TOI-270"}

RESOLVED = ["TIC 261136679", "KIC 8006161", "EPIC 201367065", "KOI-72", "K00072.01", "TOI-270.01",
            "Kepler-10 b", "Kepler-2", "pi Men", "pi Men c", "LHS 3844", "WASP-18b",
            "EPIC 201912552", "K2-18"]
UNRESOLVED = ["TIC 99999999999", "nosuchstarxyz", "KOI-999999", "TOI-999999"]


def trim(url, params, body):
    if url == C.MAST_URL:
        reply = json.loads(body)
        if isinstance(reply.get("data"), list):
            reply["data"] = [{k: row.get(k) for k in TIC_KEEP if k in row} for row in reply["data"]]
            reply.pop("fields", None)
            if json.loads(params["request"])["params"].get("radius", 0) >= MAX_CONE_RADIUS_DEG:
                for row in reply["data"]:
                    for k, places in FIELD_DECIMALS.items():
                        if isinstance(row.get(k), float):
                            row[k] = round(row[k], places)
        return json.dumps(reply)
    if params.get("query") == " ".join(S.HOST_QUERY.split()):
        rows = json.loads(body)
        kept = [r for r in rows if r["hostname"] in HOSTS_KEEP] + rows[:25]
        return json.dumps(kept, indent=0)
    return body


def record_fields(cl, hosts):
    """The 1° Kepler-10 fields the sky view asks for, at its host-catalog and TIC positions."""
    host = next(s for s in hosts.stars if s.name == "Kepler-10")
    typed = R.resolve("Kepler-10", client=cl)
    for ra, dec in ((host.ra, host.dec), (typed.ra, typed.dec)):
        stars = S.field_stars(ra, dec, MAX_CONE_RADIUS_DEG, max_tmag=FIELD_MAX_TMAG, client=cl)
        print(len(stars), f"field stars around {ra:.4f} {dec:+.4f}")


def is_field(entry):
    return "Filtered.Tic.Position" in entry["params"].get("request", "")


def share_same_fields(recordings):
    """Store a field holding the same stars as one before it as ``same_as`` that one."""
    seen = {}
    for key, entry in sorted(recordings.items()):
        if not is_field(entry) or "body" not in entry:
            continue
        rows = json.dumps(sorted(json.loads(entry["body"])["data"], key=lambda r: r["ID"]))
        if rows in seen:
            del entry["body"]
            entry["same_as"] = seen[rows]
        else:
            seen[rows] = key


def main(argv):
    if argv == ["fields"]:
        return main_fields()
    if argv:
        sys.exit(__doc__)
    rec = C.RecordingTransport(trim=trim)
    with tempfile.TemporaryDirectory() as cache:
        cl = C.CatalogClient(transport=rec, cache=C.ResponseCache(cache))
        for name in RESOLVED:
            print(name, R.resolve(name, client=cl))
        for name in UNRESOLVED:
            try:
                R.resolve(name, client=cl)
            except R.ResolveError as exc:
                print(name, "->", exc)
        kepler10 = R.resolve("Kepler-10", client=cl)
        archive.known_planets(kepler10, client=cl)
        archive.all_signals(kepler10, client=cl)
        archive.all_signals(R.resolve("KIC 8006161", client=cl), client=cl)
        archive.tois(tic=259377017, client=cl)
        hosts = S.host_stars(client=cl, offline_fallback=False)
        print(len(hosts.stars), "hosts")
        print(len(S.field_stars(285.6794, 50.2413, 0.1, client=cl)), "field stars")
        record_fields(cl, hosts)
    share_same_fields(rec.recordings)
    rec.save(OUTPUT)
    print(f"wrote {len(rec.recordings)} responses to {OUTPUT}")


def main_fields():
    """Re-record just the fields; the hosts and name lookup they start from are replayed."""
    replay = C.RecordedTransport(OUTPUT)

    def transport(method, url, params):
        if is_field({"params": params}):
            return C.requests_transport(method, url, params)
        return replay(method, url, params)

    rec = C.RecordingTransport(inner=transport, trim=trim)
    with tempfile.TemporaryDirectory() as cache:
        cl = C.CatalogClient(transport=rec, cache=C.ResponseCache(cache))
        record_fields(cl, S.host_stars(client=cl, offline_fallback=False))
    fields = {k: v for k, v in rec.recordings.items() if is_field(v)}
    rec.recordings = {**replay.recordings, **fields}
    share_same_fields(rec.recordings)
    rec.save(OUTPUT)
    print(f"wrote {len(fields)} field responses to {OUTPUT} ({len(rec.recordings)} in all)")


if __name__ == "__main__":
    main(sys.argv[1:])
