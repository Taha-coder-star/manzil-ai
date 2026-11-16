"""Load areas into the shared location gazetteer (audit WP5, MA-008, TD-13).

    python -m app.load_gazetteer --dir <folder>            # dry run (default): plan only
    python -m app.load_gazetteer --dir <folder> --apply    # write missing rows

The folder holds locations.csv, location_aliases.csv and location_neighbors.csv in the
demo/ format (docs/INVENTORY_SCHEMA.md). They are validated as app.demo_dataset does and
written by app.seed's gazetteer phase: missing rows only, an existing row that differs is
a conflict and the phase writes nothing (exit 2). The gazetteer is shared by every
agency, so this needs the schema-owner login (SEED_OWNER_DATABASE_URL) and no agency.
A running worker reads the gazetteer at start: restart it after an apply.
"""

import argparse
import os
import sys
from pathlib import Path

from app import demo_dataset as dd
from app import seed

FILES = ("locations.csv", "location_aliases.csv", "location_neighbors.csv")


def load(directory):
    """The validated gazetteer rows of a folder; raises seed.SeedError."""
    directory = Path(directory)
    try:
        data = {name: dd._read_csv(directory / name, dd.COLUMNS[name]) for name in FILES}
        raw = {name: (directory / name).read_text("utf-8") for name in FILES}
    except (OSError, ValueError, dd.DatasetError) as exc:
        raise seed.SeedError(f"gazetteer files could not be read ({type(exc).__name__})") from None
    errors = []
    locations = dd.check_locations(data["locations.csv"], errors, demo=False)
    dd.check_aliases(data["location_aliases.csv"], locations, errors)
    dd.check_neighbors(data["location_neighbors.csv"], locations, errors)
    dd.check_raw_text(raw, errors)
    if errors:
        raise seed.SeedError(f"gazetteer files are invalid ({len(errors)} errors): "
                             + "; ".join(errors[:5]))
    return data


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True)
    parser.add_argument("--apply", action="store_true", help="write (default: plan only)")
    args = parser.parse_args(argv)
    try:
        data = load(args.dir)
        with seed._connect(environ.get(seed.OWNER_URL_ENV, "").strip(),
                           seed.OWNER_URL_ENV) as conn:
            report = seed.gazetteer_phase(conn, data, write=args.apply)
    except seed.SeedError as exc:
        print(f"Gazetteer load stopped: {exc}")
        return 1
    parts = [f"{t} +{report.inserts.get(t, 0)} ={report.existing.get(t, 0)}"
             for t in seed.TABLES if t in report.inserts or t in report.existing]
    print(f"gazetteer [{report.mode}] {report.outcome}: " + ", ".join(parts))
    for conflict in report.conflicts:
        print(f"  CONFLICT {conflict}")
    print("(+N = to insert / inserted, =N = already present and left unchanged)")
    return 2 if report.conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
