"""Re-record the catalog responses the tests replay (needs network; free public services).

    python tests/data/catalog/record.py

Runs the lookups the catalog tests make against MAST and the NASA Exoplanet
Archive with an empty cache and writes every response to ``recorded.json``
beside this file.  TIC rows are trimmed to the columns the code reads and
the all-hosts query to the validation hosts plus a few others, to keep the
file small.  Tests that check exact values may need updating after a
re-recording, since archive values are revised over time.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from transitory.catalog import archive, client as C, resolve as R, skycatalog as S  # noqa: E402

OUTPUT = Path(__file__).resolve().parent / "recorded.json"

TIC_KEEP = ["ID", "ra", "dec", "Tmag", "KIC", "rad", "objType", "dstArcSec"]
HOSTS_KEEP = {"Kepler-10", "Kepler-7", "HAT-P-7", "Kepler-22", "HD 39091", "LHS 3844", "WASP-18",
              "TOI-270"}

RESOLVED = ["TIC 261136679", "KIC 8006161", "EPIC 201367065", "KOI-72", "K00072.01", "TOI-270.01",
            "Kepler-10 b", "Kepler-2", "pi Men", "pi Men c", "LHS 3844", "WASP-18b"]
UNRESOLVED = ["TIC 99999999999", "nosuchstarxyz", "KOI-999999", "TOI-999999"]


def trim(url, params, body):
    if url == C.MAST_URL:
        reply = json.loads(body)
        if isinstance(reply.get("data"), list):
            reply["data"] = [{k: row.get(k) for k in TIC_KEEP if k in row} for row in reply["data"]]
            reply.pop("fields", None)
        return json.dumps(reply)
    if params.get("query") == " ".join(S.HOST_QUERY.split()):
        rows = json.loads(body)
        kept = [r for r in rows if r["hostname"] in HOSTS_KEEP] + rows[:25]
        return json.dumps(kept, indent=0)
    return body


def main():
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
        print(len(S.host_stars(client=cl, offline_fallback=False).stars), "hosts")
        print(len(S.field_stars(285.6794, 50.2413, 0.1, client=cl)), "field stars")
    rec.save(OUTPUT)
    print(f"wrote {len(rec.recordings)} responses to {OUTPUT}")


if __name__ == "__main__":
    main()
