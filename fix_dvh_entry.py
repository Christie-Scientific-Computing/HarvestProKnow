"""
One-off debug script: patch a single dvh_data row from a DVH export.

Supports two input formats, auto-detected from the file (override with
--format if detection ever picks the wrong one):

  "csv" -- the wide Proknow-style CSV, columns "Dose (Gy)", "<structure
      name> (%)". Proknow's export pads the volume column with a trailing
      run of zeros out to the plan's global max dose, even for structures
      that hit 0% volume earlier, so that gets trimmed before writing back
      -- otherwise the array length won't match what the rest of the
      pipeline (dvhcalc.get_dvh() in api/proknow_client.py) produces.

  "tps" -- a single-structure DVH dump from another planning system, e.g.:
        #RoiName:PTV1_Eval-05
        #Roi volume fraction outside grid: 0 %
        #Unit: cGy
        0.000	100.000
        28.460	100.000
        ...
      Dose is converted to Gy (from the declared #Unit) and the curve --
      sampled on an irregular dose grid -- is linearly resampled onto the
      same fixed bin_width grid the "csv" format and the DB use, so
      everything downstream (trim/diff/plot/write) is format-agnostic.

Defaults to a dry run -- it only prints the diff (and optionally plots it).
Pass --write to actually update the row.

Usage:
    python fix_dvh_entry.py path/to/dvh.csv --dose-id <dose_id>                  # dry-run, just show the diff
    python fix_dvh_entry.py path/to/tps_dvh.txt --mrn <mrn> --plot               # dry-run + visual comparison, other TPS format
    python fix_dvh_entry.py path/to/dvh.csv --mrn <mrn> --write                  # actually apply the update
    python fix_dvh_entry.py path/to/dvh.csv --mrn <mrn> --structure-name PTV1_Eval-05 --volume 123.45 --write
    python fix_dvh_entry.py path/to/dvh.csv --mrn <mrn> --plot --compare path/to/tps_dvh.txt
        # overlays 3 curves: dvh.csv (the row that would be written), tps_dvh.txt
        # (comparison only), and the DB's current cumulative_dvh
"""
import argparse
import csv
import os
import re
import statistics
import sys

import numpy as np
import psycopg
from dotenv import load_dotenv

load_dotenv("/config/.secrets/HarvestProknow/.env")

STRUCTURE_COL_RE = re.compile(r"\s*\(%\)\s*$")
TPS_BIN_WIDTH = 0.01  # Gy; matches the dvh_data convention used elsewhere in this repo.
TPS_DOSE_UNIT_SCALE = {"gy": 1.0, "cgy": 0.01}


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


def parse_tps_dvh(path: str) -> tuple[list[float], dict[str, list[float]]]:
    """Parse a single-structure DVH dump from another planning system.

    Format: "#Key: value" comment lines (RoiName, Unit, ...) followed by
    whitespace-separated (dose, volume %) rows on an irregular dose grid.
    The curve is linearly resampled onto a fixed TPS_BIN_WIDTH grid (the
    same convention parse_csv()/the dvh_data table use) so the rest of the
    script doesn't need to know the difference.
    """
    roi_name = None
    unit_scale = None
    raw_dose = []
    raw_volume = []

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if m := re.match(r"#RoiName:\s*(.+)", line, re.IGNORECASE):
                    roi_name = m.group(1).strip()
                elif m := re.match(r"#Unit:\s*(.+)", line, re.IGNORECASE):
                    unit = m.group(1).strip().lower()
                    if unit not in TPS_DOSE_UNIT_SCALE:
                        raise ValueError(f"{path}: unsupported dose unit {unit!r}")
                    unit_scale = TPS_DOSE_UNIT_SCALE[unit]
                elif m := re.match(r"#Roi volume fraction outside grid:\s*([\d.]+)\s*%", line, re.IGNORECASE):
                    fraction = float(m.group(1))
                    if fraction:
                        print(f"  warning: TPS reports {fraction}% of the ROI volume is outside its dose grid")
                continue
            dose_str, volume_str = line.split()[:2]
            raw_dose.append(float(dose_str))
            raw_volume.append(float(volume_str))

    if roi_name is None:
        raise ValueError(f"{path}: missing '#RoiName:' header line")
    if unit_scale is None:
        raise ValueError(f"{path}: missing '#Unit:' header line")
    if not raw_dose:
        raise ValueError(f"{path}: no DVH data rows found")

    doses_gy = [d * unit_scale for d in raw_dose]

    # Collapse duplicate dose values (vertical drops in the curve), keeping
    # the value *after* the drop -- i.e. the last entry at a given dose --
    # so resampling picks up the post-drop volume at/after that dose.
    dedup_dose, dedup_volume = [], []
    for d, v in zip(doses_gy, raw_volume):
        if dedup_dose and dedup_dose[-1] == d:
            dedup_volume[-1] = v
        else:
            dedup_dose.append(d)
            dedup_volume.append(v)

    n_bins = int(dedup_dose[-1] / TPS_BIN_WIDTH) + 1
    grid = [i * TPS_BIN_WIDTH for i in range(n_bins)]
    resampled = np.interp(grid, dedup_dose, dedup_volume).tolist()

    return grid, {roi_name: resampled}


def detect_format(path: str) -> str:
    """Sniff "csv" vs "tps" from the first non-blank line of the file."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                return "tps" if line.startswith("#") else "csv"
    raise ValueError(f"{path}: file is empty")


def load_dvh_file(path: str, fmt: str = "auto") -> tuple[list[float], dict[str, list[float]]]:
    """Auto-detect (or use the given) format and parse a DVH export."""
    resolved = fmt if fmt != "auto" else detect_format(path)
    return parse_tps_dvh(path) if resolved == "tps" else parse_csv(path)


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


def plot_comparison(
    dose_primary: list[float], primary_values: list[float], primary_label: str,
    old_bin_width: float, old_dvh: list[float],
    dose_id: str, structure_name: str, plot_dir: str | None,
    dose_compare: list[float] | None = None, compare_values: list[float] | None = None,
    compare_label: str | None = None,
) -> None:
    """Plot the imported (trimmed) DVH(s) against the DB's current DVH."""
    import matplotlib
    if plot_dir:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dose_old = [i * old_bin_width for i in range(len(old_dvh))]

    fig, ax = plt.subplots()
    ax.plot(dose_old, old_dvh, label="DB (current)", linestyle="--")
    ax.plot(dose_primary, primary_values, label=primary_label)
    if dose_compare is not None:
        ax.plot(dose_compare, compare_values, label=compare_label, linestyle=":")
    ax.set_xlabel("Dose (Gy)")
    ax.set_ylabel("Relative volume (%)")
    ax.set_title(f"{structure_name} (dose_id={dose_id})")
    ax.legend()

    if plot_dir:
        os.makedirs(plot_dir, exist_ok=True)
        path = os.path.join(plot_dir, f"{dose_id}_{structure_name}.png")
        fig.savefig(path)
        print(f"  saved plot to {path}")
    else:
        plt.show()
    plt.close(fig)


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
    parser.add_argument("csv_path", help="Path to the DVH export (Proknow-style wide CSV, or another TPS's single-structure DVH dump)")
    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--dose-id", help="dose_id to update directly")
    id_group.add_argument("--mrn", help="Look up the dose_id via the doses table MRN column")
    parser.add_argument("--format", choices=["auto", "csv", "tps"], default="auto", help="Input format; auto-detected by default (see module docstring)")
    parser.add_argument("--structure-name", help="Only update this structure (must match a CSV column, or the file's #RoiName, after stripping ' (%%)')")
    parser.add_argument("--write", action="store_true", help="Actually apply the update. Without this, the script only shows the diff (and plot, if requested)")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt (only relevant with --write)")
    parser.add_argument("--plot", action="store_true", help="Plot the imported DVH(s) against the DB's current DVH")
    parser.add_argument("--plot-dir", help="Save plots as PNGs here instead of opening an interactive window (implies --plot)")
    parser.add_argument("--compare", metavar="PATH", help="A second DVH export (other format/TPS) to additionally overlay in --plot, purely for visual comparison -- it is never written to the DB")
    parser.add_argument("--compare-format", choices=["auto", "csv", "tps"], default="auto", help="Format of --compare; auto-detected by default")
    parser.add_argument("--volume", type=float, help="Manually override the volume (cm^3) field for the row(s) being updated")
    args = parser.parse_args()
    if args.plot_dir:
        args.plot = True

    doses, structures = load_dvh_file(args.csv_path, args.format)
    bin_width = infer_bin_width(doses)

    if args.structure_name:
        if args.structure_name not in structures:
            sys.exit(f"--structure-name {args.structure_name!r} not found in CSV columns: {list(structures)}")
        structures = {args.structure_name: structures[args.structure_name]}

    if args.volume is not None and len(structures) > 1:
        sys.exit("--volume applies to a single structure; pass --structure-name to disambiguate")

    compare_doses = compare_structures = None
    if args.compare:
        if not args.plot:
            print("note: --compare only affects --plot/--plot-dir output")
        else:
            compare_doses, compare_structures = load_dvh_file(args.compare, args.compare_format)

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
            old_dvh, old_bin_width, old_volume = existing
            new_volume = args.volume if args.volume is not None else old_volume

            print(f"dose_id={dose_id} structure_name={structure_name!r}")
            print(f"  bin_width:      {old_bin_width} -> {bin_width}")
            print(f"  cumulative_dvh: {len(old_dvh)} bins -> {len(trimmed)} bins (dropped {len(values) - len(trimmed)} trailing zeros)")
            if args.volume is not None:
                print(f"  volume:         {old_volume} -> {new_volume}")
            else:
                print(f"  volume (unchanged): {old_volume}")

            if args.plot:
                compare_dose_axis = compare_trimmed = None
                if compare_structures is not None:
                    compare_values = compare_structures.get(structure_name)
                    if compare_values is None:
                        print(f"  note: --compare file has no structure named {structure_name!r}; known: {list(compare_structures)}")
                    else:
                        compare_trimmed = trim_trailing_zeros(compare_values)
                        compare_dose_axis = compare_doses[: len(compare_trimmed)]

                plot_comparison(
                    doses[: len(trimmed)], trimmed, os.path.basename(args.csv_path),
                    old_bin_width, old_dvh, dose_id, structure_name, args.plot_dir,
                    dose_compare=compare_dose_axis, compare_values=compare_trimmed,
                    compare_label=os.path.basename(args.compare) if args.compare else None,
                )

            if not args.write:
                print("  [dry-run] pass --write to apply this update")
                continue

            if not args.yes:
                resp = input("  Apply this update? [y/N] ").strip().lower()
                if resp != "y":
                    print("  skipped")
                    continue

            if args.volume is not None:
                cursor.execute(
                    "UPDATE dvh_data SET cumulative_dvh = %s, bin_width = %s, volume = %s "
                    "WHERE dose_id = %s AND structure_name = %s",
                    (trimmed, bin_width, new_volume, dose_id, structure_name),
                )
            else:
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
