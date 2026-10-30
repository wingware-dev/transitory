"""Re-record the TESS sector fixtures the tess_sectors tests use (needs network; free public services).

    python tests/data/tess_sectors/record.py

Writes beside this file:

- ``footprints.json``: MAST's TESS FFI footprint file, trimmed to the CCD
  polygons with a corner within :data:`KEEP_DEG` of the test positions
  (pi Men and RA 0, Dec -30), in the original column layout.
- ``tesscut_pi_men.json``: TESScut's sector reply for pi Men.

The tests compare the two, so after a re-recording that brings new
sectors, update the expected lists in ``tests/test_tess_sectors.py``.
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from transitory.catalog.client import requests_transport  # noqa: E402
from transitory.pipeline import tess_sectors as T  # noqa: E402

HERE = Path(__file__).resolve().parent
PI_MEN = (84.291188, -80.469119)
TEST_POSITIONS = [PI_MEN, (0.0, -30.0)]
KEEP_DEG = 25.0


def trimmed_footprints(text: str) -> dict:
    data = json.loads(text)
    centres = np.array([T._unit_vectors(np.array(ra), np.array(dec)) for ra, dec in TEST_POSITIONS])
    keep = []
    for i, region in enumerate(data["s_region"]):
        pairs = np.array(region.split()[1:], dtype=float).reshape(-1, 2)
        corners = T._unit_vectors(pairs[:, 0], pairs[:, 1])
        if np.max(corners @ centres.T) >= np.cos(np.radians(KEEP_DEG)):
            keep.append(i)
    return {column: [values[i] for i in keep] for column, values in data.items()}


def main() -> None:
    text = requests_transport("GET", T.FOOTPRINT_URL, {})
    (HERE / "footprints.json").write_text(json.dumps(trimmed_footprints(text), indent=0) + "\n",
                                          encoding="utf-8")
    ra, dec = PI_MEN
    reply = requests_transport("GET", T.TESSCUT_SECTOR_URL,
                               {"ra": f"{ra:.6f}", "dec": f"{dec:.6f}", "radius": "0"})
    (HERE / "tesscut_pi_men.json").write_text(json.dumps(json.loads(reply), indent=0) + "\n",
                                              encoding="utf-8")


if __name__ == "__main__":
    main()
