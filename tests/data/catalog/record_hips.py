"""Re-record the HiPS cutout the hips tests use (needs network; free public CDS service).

    python tests/data/catalog/record_hips.py              # real DSS2 cutout from hips2fits
    python tests/data/catalog/record_hips.py --stand-in   # synthetic JPEG, no network
    python tests/data/catalog/record_hips.py --skyview    # SkyView DSS2 bands (free NASA service)

Writes ``hips_cutout.jpg`` beside this file: a small DSS2 colour cutout
around Kepler-10 on the WCS :data:`FIXTURE_VIEW` describes.  ``--stand-in``
draws a gradient JPEG of the same size instead (red rising to the top,
green rising to the left), for when CDS can't be reached.

``--skyview`` writes, for the same view, SkyView's grey JPEGs of the DSS2
bands hips.SURVEYS["dss2"] merges (``skyview_dss2_ir.jpg`` and so on) and
the DSS2 Red image as FITS (``skyview_dss2_red.fits``), whose header and
pixels the tests hold the JPEG's orientation against.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from transitory.catalog import hips  # noqa: E402

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "hips_cutout.jpg"

# (ra_range, dec_range, width_px, height_px) around Kepler-10
FIXTURE_VIEW = ((285.43, 285.93), (50.115, 50.365), 64, 32)


def stand_in() -> bytes:
    import io

    from PIL import Image

    width, height = FIXTURE_VIEW[2], FIXTURE_VIEW[3]
    image = Image.new("RGB", (width, height))
    for y in range(height):
        for x in range(width):
            image.putpixel((x, y), (255 * (height - 1 - y) // (height - 1),
                                    255 * (width - 1 - x) // (width - 1), 64))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def skyview_file(name: str, suffix: str) -> Path:
    """Where SkyView survey ``name``'s recorded image goes ("DSS2 Red" -> skyview_dss2_red.jpg)."""
    return HERE / f"skyview_{name.lower().replace(' ', '_')}{suffix}"


def record_skyview() -> None:
    wcs = hips.view_wcs(*FIXTURE_VIEW)
    for name in hips.SURVEYS["dss2"].skyview:
        params = hips.skyview_params(name, wcs)
        data = hips.requests_bytes_transport(hips.SKYVIEW_URL, params)
        skyview_file(name, ".jpg").write_bytes(data)
        print(f"wrote {skyview_file(name, '.jpg')} ({len(data)} bytes)")
    params = {**hips.skyview_params("DSS2 Red", wcs), "Return": "FITS"}
    data = hips.requests_bytes_transport(hips.SKYVIEW_URL, params)
    skyview_file("DSS2 Red", ".fits").write_bytes(data)
    print(f"wrote {skyview_file('DSS2 Red', '.fits')} ({len(data)} bytes)")


def main() -> None:
    if "--skyview" in sys.argv[1:]:
        record_skyview()
        return
    if "--stand-in" in sys.argv[1:]:
        data = stand_in()
    else:
        wcs = hips.view_wcs(*FIXTURE_VIEW)
        params = hips.request_params(hips.SURVEYS["dss2"], wcs)
        data = hips.requests_bytes_transport(hips.HIPS2FITS_URL, params)
    OUTPUT.write_bytes(data)
    print(f"wrote {OUTPUT} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
