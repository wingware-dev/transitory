"""``python -m transitory.validation``: run the known-planet validation and write the report."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ..sources import SyntheticSource
from . import report
from .targets import select, validation_targets
from .validate import archive_provider, fetch_credits, run_validation, synthetic_provider, synthetic_targets


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
    parser.add_argument("--rebuild-credits", action="store_true",
                        help="only rewrite the report's data credits from its results.json (and the "
                             "light-curve cache, for a report that did not record them); no search")
    return parser.parse_args(argv)


def rebuild_credits(args: argparse.Namespace) -> int:
    directory = args.out if args.out is not None else report.default_directory()
    try:
        saved = json.loads((directory / report.JSON_NAME).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"no report to rebuild: {exc}", file=sys.stderr)
        return 2
    source, targets, provider = _inputs(args, saved.get("max_products"))
    by_name = {vt.target.name: vt for vt in targets}

    def backfill(name: str) -> dict:
        print(f"reading the light curve of {name}", file=sys.stderr, flush=True)
        return report.credit_entry(*fetch_credits(by_name[name], source, provider))

    path = report.rebuild_credits(directory, backfill)
    print(f"rewrote the data credits in {path}")
    if args.publish:
        print(f"published {report.publish(path.parent)}")
    return 0


def _inputs(args: argparse.Namespace, max_products: int | None):
    """The light-curve source, targets and published-value provider the arguments ask for."""
    if args.demo:
        source = SyntheticSource()
        return source, synthetic_targets(source), synthetic_provider(source)
    from ..pipeline.fetch import MastSource
    return MastSource(max_products=max_products), validation_targets(), archive_provider(refresh=args.refresh)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    if args.rebuild_credits:
        return rebuild_credits(args)
    source, targets, provider = _inputs(args, args.max_sectors)
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
