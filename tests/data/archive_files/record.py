"""Re-record the archive listings ``tests/test_archive_files.py`` replays (needs network; free public services).

    python tests/data/archive_files/record.py

Looks up each case below on both mirrors (archive.stsci.edu and the AWS
``stpubdata`` bucket) with ``archive_files.find_products`` and writes every
reply to ``recorded.json`` beside this file: each URL asked for, with the
reply's text, or null for a 404 (a directory that is not there, such as pi
Men's in sector 2).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from transitory.pipeline import archive_files as A  # noqa: E402

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "recorded.json"

CASES = [
    ("Kepler", 11904151, None),          # Kepler-10: no Q8, Q12, Q16 (CCD module 3)
    ("K2", 201912552, None),             # K2-18: campaign 1
    ("K2", 201437844, None),             # HD 106315: campaign 10's second half
    ("K2", 203008242, None),             # campaign 11's two halves
    ("TESS", 261136679, [1, 2, 27]),     # pi Men: s1 2-min, s2 none, s27 2-min and 20-s
]


def main():
    recorded: dict[str, str | None] = {}

    def recording(url, timeout):
        body = A.requests_transport(url, timeout)
        recorded[url] = None if body is None else body.decode("utf-8")
        return body

    for mission, number, sectors in CASES:
        for mirror in A.MIRRORS:
            products = A.find_products(mission, number, mirror=mirror, sectors=sectors,
                                       transport=recording)
            print(mission, number, mirror, [p["label"] + " " + p["author"] for p in products])
    # TESS-SPOC listings the cases above skip, since SPOC has 2-minute files there.
    for sector in (1, 27):
        recording(A.MIRRORS["stsci"].url("tess-spoc", A.tess_hlsp_dir(sector, 261136679)), 15)
    OUTPUT.write_text(json.dumps(dict(sorted(recorded.items())), indent=1) + "\n")


if __name__ == "__main__":
    main()
