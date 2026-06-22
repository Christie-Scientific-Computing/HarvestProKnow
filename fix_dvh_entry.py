"""
One-off debug script: patch a single dvh_data row from a DVH CSV exported
from Proknow (columns: "Dose (Gy)", "<structure name> (%)").

Proknow's CSV export pads the volume column with a trailing run of zeros
out to the plan's global max dose, even for structures that hit 0% volume
earlier. The cumulative_dvh arrays already in the DB (produced by
dvhcalc.get_dvh() in api/proknow_client.py) don't have that padding, so it
gets trimmed here before writing back -- otherwise the array length won't
match what the rest of the pipeline produces.

Usage:
    python fix_dvh_entry.py path/to/dvh.csv --dose-id <dose_id>
    python fix_dvh_entry.py path/to/dvh.csv --mrn <mrn> [--structure-name PTV1_Eval-05]
    python fix_dvh_entry.py path/to/dvh.csv --mrn <mrn> --dry-run
"""
import argparse
import csv
import os
import re
import statistics
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv("/config/.secrets/HarvestProknow/.env")

STRUCTURE_COL_RE = re.compile(r"\s*\(%\)\s*$")


def parse_csv(csv_path: str) -> tuple[list[float], dict[str, list[float]]]:
    """Returns (dose_bins, {structure_name: percent_values})."""
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        if not fieldnames or len(fieldnames) < 2:
            raise ValueError(f"Expected a dose column plus at least one structure column, got {fieldnames}")

        dose_col = fieldnames[0]
        structure_cols = fieldnames[1:]

        doses = []
        columns = {col: [] for col in structure_cols}
        for row in reader:
            doses.append(float(row[dose_col]))
            for col in structure_cols:
                columns[col].append(float(row[col]))

    structures = {}
    for col, values in columns.items():
        name = STRUCTURE_COL_RE.sub("", col).strip()
        structures[name] = values
    return doses, structures


def trim_trailing_zeros(values: list[float]) -> list[float]:
    last_nonzero = -1
    for i, v in enumerate(values):
        if v != 0:
            last_nonzero = i
    if last_nonzero == -1:
        raise ValueError("All values are zero, refusing to trim the whole array")
    return values[: last_nonzero + 1]


def infer_bin_width(doses: list[float]) -> float:
    diffs = [round(b - a, 6) for a, b in zip(doses, doses[1:])]
    return round(statistics.median(diffs), 4)


def resolve_dose_id(cursor, dose_id: str | None, mrn: str | None) -> str:
    if dose_id:
        return dose_id

    cursor.execute("SELECT id, plan_name, plan_date FROM doses WHERE mrn = %s", (mrn,))
    rows = cursor.fetchall()
    if not rows:
        sys.exit(f"No doses found for MRN {mrn!r}")
    if len(rows) > 1:
        print(f"Multiple doses found for MRN {mrn!r}, pass --dose-id to disambiguate:", file=sys.stderr)
        for row in rows:
            print(f"  dose_id={row[0]!r}  plan_name={row[1]!r}  plan_date={row[2]!r}", file=sys.stderr)
        sys.exit(1)
    return rows[0][0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", help="Path to the DVH CSV exported from Proknow")
    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--dose-id", help="dose_id to update directly")
    id_group.add_argument("--mrn", help="Look up the dose_id via the doses table MRN column")
    parser.add_argument("--structure-name", help="Only update this structure (must match a CSV column after stripping ' (%%)')")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to the DB, just show what would change")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args()

    doses, structures = parse_csv(args.csv_path)
    bin_width = infer_bin_width(doses)

    if args.structure_name:
        if args.structure_name not in structures:
            sys.exit(f"--structure-name {args.structure_name!r} not found in CSV columns: {list(structures)}")
        structures = {args.structure_name: structures[args.structure_name]}

    conn = psycopg.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
    )
    try:
        cursor = conn.cursor()
        dose_id = resolve_dose_id(cursor, args.dose_id, args.mrn)

        for structure_name, values in structures.items():
            trimmed = trim_trailing_zeros(values)

            cursor.execute(
                "SELECT cumulative_dvh, bin_width, volume FROM dvh_data WHERE dose_id = %s AND structure_name = %s",
                (dose_id, structure_name),
            )
            existing = cursor.fetchone()
            if existing is None:
                cursor.execute(
                    "SELECT structure_name FROM dvh_data WHERE dose_id = %s", (dose_id,)
                )
                known = [r[0] for r in cursor.fetchall()]
                sys.exit(
                    f"No existing dvh_data row for dose_id={dose_id!r} structure_name={structure_name!r}. "
                    f"Known structures for this dose_id: {known}"
                )
            old_dvh, old_bin_width, volume = existing

            print(f"dose_id={dose_id} structure_name={structure_name!r}")
            print(f"  bin_width:      {old_bin_width} -> {bin_width}")
            print(f"  cumulative_dvh: {len(old_dvh)} bins -> {len(trimmed)} bins (dropped {len(values) - len(trimmed)} trailing zeros)")
            print(f"  volume (unchanged): {volume}")

            if args.dry_run:
                print("  [dry-run] skipping write")
                continue

            if not args.yes:
                resp = input("  Apply this update? [y/N] ").strip().lower()
                if resp != "y":
                    print("  skipped")
                    continue

            cursor.execute(
                "UPDATE dvh_data SET cumulative_dvh = %s, bin_width = %s WHERE dose_id = %s AND structure_name = %s",
                (trimmed, bin_width, dose_id, structure_name),
            )
            conn.commit()
            print("  updated")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
