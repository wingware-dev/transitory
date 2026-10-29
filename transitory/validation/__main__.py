"""``python -m transitory.validation``: run the known-planet validation and write the report."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ..sources import SyntheticSource
from . import report
from .targets import select, validation_targets
from .validate import archive_provider, run_validation, synthetic_provider, synthetic_targets


def _max_products(text: str) -> int | None:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 (all) or more")
    return value or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m transitory.validation",
                                     description="Run the pipeline on known planets and null stars, "
                                                 "and report what it finds against published values.")
    parser.add_argument("--demo", action="store_true", help="validate on the synthetic demo stars (no downloads)")
    parser.add_argument("--targets", metavar="NAMES", default="",
                        help='only these stars or planets, e.g. "Kepler-10, pi Men c"')
    parser.add_argument("--max-sectors", metavar="N", type=_max_products, default=None,
                        help="TESS sectors / Kepler quarters per star; 0 (the default) for all")
    parser.add_argument("--out", metavar="DIR", type=Path, default=None,
                        help="report directory (default: reports/validation)")
    parser.add_argument("--publish", action="store_true",
                        help=f"also copy the report to {report.DOCS_DIR.relative_to(report.PROJECT_ROOT)}")
    parser.add_argument("--refresh", action="store_true", help="re-query the archive instead of using its cache")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    if args.demo:
        source = SyntheticSource()
        targets = synthetic_targets(source)
        provider = synthetic_provider(source)
    else:
        from ..pipeline.fetch import MastSource
        source = MastSource(max_products=args.max_sectors)
        targets = validation_targets()
        provider = archive_provider(refresh=args.refresh)
    if args.targets:
        try:
            targets = select(targets, args.targets.split(","))
        except KeyError as exc:
            print(exc.args[0], file=sys.stderr)
            return 2

    last = [-1]

    def progress(message: str, fraction: float) -> None:
        pct = int(fraction * 100)
        if pct != last[0]:
            last[0] = pct
            print(f"{pct:3d}%  {message}", file=sys.stderr, flush=True)

    run = run_validation(targets, source, provider, progress_cb=progress, max_products=args.max_sectors)
    path = report.write_report(run, args.out)
    print(f"wrote {path}")
    if args.publish:
        print(f"published {report.publish(path.parent)}")
    tally = run.tally()
    print(f"{tally['found']} found, {tally['aliases']} at aliases, {tally['missed']} missed of "
          f"{tally['published']} published; {tally['spurious_null'] + tally['spurious_host']} spurious, "
          f"{tally['duo_null'] + tally['duo_host']} duo-transit, {tally['rejected']} rejected by vetting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
