# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A data-harvesting script for a radiotherapy "Continuous Improvement" platform. It reads a CSV of patient IDs, queries [Proknow](https://proknow.com) (a radiotherapy oncology information system) for patient metadata, treatment/dose data, DVHs (dose-volume histograms), and geometrical structure metrics, then upserts everything into a shared PostgreSQL database (`BigDB`). It's meant to be run on a recurring schedule (e.g. cron) to keep the DB current, and is used alongside a separate system called EDNA — see `static/Donal-Diagram.svg` for the data-flow diagram referenced in [README.md](README.md).

There is a second, currently-disabled data source: `AskCWP` queries a SQL Server "EForms"/booking-form database at The Christie. It only works on Windows (relies on `Trusted_Connection`/SSPI auth) and is wired up in [harvest.py](harvest.py) but commented out.

## Running

```bash
pip install -r requirements.txt
python harvest.py
```

Requires:
- A `.env` file at `/config/.secrets/HarvestProknow/.env` (hardcoded path, loaded via `load_dotenv` in [harvest.py](harvest.py)) providing `PROKNOW_WORKSPACE`, `PROKNOW_CREDS` (path to Proknow credentials JSON), `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS`, and (if CWP is re-enabled) `CHRISTIE_CREDS`.
- [config.toml](config.toml) in the repo root for non-secret runtime config: `path_to_csv` (CSV of patient IDs, column `patient_id`), `log-to-file`, `log-dir`, `log-level`.
- A running PostgreSQL instance with the expected tables already created (`patients`, `doses`, `dvh_data`, `geom_metrics` — see Database section below). Table creation/migrations are not part of this repo.

A pytest-based unit test suite exists under [tests/](tests/) (run with `python -m pytest tests/`); see [tests/README.md](tests/README.md) for the mocking strategy. There is no linter config or CI pipeline file in this repo currently.

### Docker

[Dockerfile](Dockerfile) builds a `python:3.14` image, installs `msodbcsql17` + FreeTDS (required for the SQL Server/CWP path via `pyodbc`/`pymssql`), and installs `requirements.txt`. It does not set an entrypoint/CMD or copy the repo in — treat it as a base image to build on rather than a ready-to-run container.

## Architecture

**[harvest.py](harvest.py)** — entry point and orchestration (`ProknowHarvester`).
- `ProknowHarvester` is a context manager wrapping a single `psycopg` connection (`__enter__`/`__exit__` open/close it).
- `run(csv_path)` loops over patient IDs read from CSV, calling `fetch_proknow_data` then `write_results_to_db` per patient (no batching).
- `fetch_proknow_data` constructs an `AskProKnow(patient_id)`, skips the patient if not found in Proknow, and gathers four result buckets: `patient_data`, `treatment_data`, `dvh_data`, `geom_metrics` (the latter two both come from one `AskProKnow.get_dose_metrics()` call). New data types should be added as a new key in this dict plus a matching `AskProKnow` getter.
- A hash-skip mechanism (`check_hash_in_db`) is meant to avoid re-fetching unchanged patients (SHA-256 over the raw Proknow patient summary JSON), but the actual `return` on hash-match is currently commented out — every patient is refetched and upserted regardless.
- `write_results_to_db` / `_write_table` perform generic `INSERT ... ON CONFLICT (id_col) DO UPDATE` upserts, driven entirely by dict keys in the result payloads — so the table schema is implicitly defined by whatever keys `AskProKnow.get_*` methods return. `id_col` can be a single column or a tuple of columns (composite conflict target).

**[api/proknow_client.py](api/proknow_client.py)** — `AskProKnow`, the Proknow data-access layer. One instance per patient.
- On construction, looks up the patient by ID (`find_patient`), downloads the full `PatientItem`, computes `patient_hash` over `patient.data`, then calls `_filter_doses()` — which downloads each dose's RTDOSE and keeps only those with `DoseSummationType == "PLAN"` (drops per-beam doses) — into `self.accepted_dose_ids`. `get_treatment_data()` and `get_dose_metrics()` both skip doses not in this list.
- `get_patient_data()` — flat patient demographic/summary record for the `patients` table. Asserts the returned MRN matches the requested `patient_id`.
- `get_treatment_data()` — for each accepted dose, walks up to its parent `plan`, downloads the RTPLAN DICOM, and extracts planning fields (`get_data_from_plan`) for the `doses` table. `plan_id`/`structure_set_id`/`image_set_id` on the dose link the family of objects together.
- `get_dose_metrics()` — for each accepted dose, downloads the dose's RTDOSE, RTSTRUCT, and (via `_download_image_slices()`) just 2 slices of its image set into one shared tempdir, then calls `calculate_dvhs()` and `calculate_geometrical_metrics()` against those shared downloads, returning `(dvh_data, geom_metrics)`. This single pass replaces what used to be two independent `get_dvh_data()`/`get_geometrical_metrics()` passes that each re-downloaded the structure set, and the whole image series just to read slice spacing/orientation off its DICOM headers.
  - `calculate_dvhs()` parses the RTSTRUCT/RTDOSE once via `pydicom.dcmread` and passes the parsed `Dataset`s into `dicompylercore.dvhcalc.get_dvh()` for every structure — `get_dvh()` re-reads/re-parses its structure/dose arguments from disk on every call if given file paths instead of `Dataset`s. Returns relative-volume cumulative DVH curves plus structure volume in cm³ for the `dvh_data` table; zero-volume structures are dropped.
  - `calculate_geometrical_metrics()` takes an already-downloaded RTSTRUCT path and image-slice directory and delegates to [utils/geom_metrics.py](utils/geom_metrics.py) for the geometry math; flattens the resulting pairwise target/OAR metrics for the `geom_metrics` table.
  - `_download_image_slices()` streams individual images from an image set by position-sorted index, bypassing `ImageSetItem.download()`'s whole-series download; it reaches into SDK-internal attributes (`_requestor`/`_workspace_id`/`_id`) since the public SDK has no partial-download API.
- All DICOM downloads go through `tempfile.TemporaryDirectory()` — nothing touching Proknow DICOM data is persisted to disk outside the harvest run.

**[utils/geom_metrics.py](utils/geom_metrics.py)** — pure, stateless geometry helpers (no Proknow/DB dependency). Computes structure geometry directly from RTSTRUCT contour polygons (via Shapely) rather than rasterizing to a voxel grid, to avoid voxel quantization:
- Structures are classified by name into `TARGET` (name contains `"TV"` — covers GTV/CTV/PTV/ITV), `OAR`, or `EXCLUDED` (BODY/EXTERNAL, or `RTROIInterpretedType == EXTERNAL`) via `_classify`.
- The referenced image series is read to get authoritative slice thickness and is asserted to be axial (`_assert_axial_orientation`) — the whole per-slice-polygon approach assumes constant-z slices; oblique series raise.
- Metrics computed pairwise for every (target, OAR) pair: Dice coefficient, relative overlap, signed 3D centroid displacement vector + magnitude, minimum surface-to-surface distance, and HD95 (95th-percentile symmetric Hausdorff) surface distance — the latter two via `cKDTree` nearest-neighbour queries on contour boundary point clouds.
- Rounding precision for output values is centralized in module-level constants (`_VOLUME_DP`, `_DICE_DP`, `_DISTANCE_DP`) — change these rather than rounding ad hoc elsewhere.
- This module (and the geometry-calculation parts of `proknow_client.py`) was originally generated by Claude Opus 4.8, per the docstrings — read the docstrings in both files closely before modifying, they document non-obvious geometric assumptions (even-odd fill rule for nested contours, z-precision rounding so Dice/intersection logic matches planes across structures, etc).

**[api/cwp_client.py](api/cwp_client.py)** — `AskCWP`, currently disabled in `harvest.py`. Connects to a SQL Server booking-form DB (FreeTDS/pyodbc) using OS-trusted auth, so it only works when run on Windows. `get_booking_data()` calls a stored procedure (`usp_GetLatestRadiotherapyBooking`) and filters/renames the columns via `filter_data`. Only ever returns the latest booking form per patient — known limitation for re-treated patients (see TODO in README).

**[utils/setup.py](utils/setup.py)** — `init_config()` parses [config.toml](config.toml) (via `tomllib`) and coerces values to expected types; `init_logger()` configures root logging (file or stdout) based on that config, and silences `httpx` logging to WARNING (Proknow SDK is httpx-based and otherwise noisy).

## Database

There's no migrations/schema file in this repo — schema is implicit in the dict keys produced by the `AskProKnow.get_*` methods and the `id_col` conflict targets passed to `_write_table`. Known tables and their composite/primary upsert keys:
- `patients` — keyed by implicit default `"id"` (Proknow patient ID); has a `sha256` column used for the (currently inert) skip-if-unchanged check.
- `doses` — keyed by implicit default `"id"` (dose ID).
- `dvh_data` — keyed by `(dose_id, structure_name)`.
- `geom_metrics` — keyed by `(dose_id, structure_set_id, target, oar)`.

If you add a new `get_*` data source in `proknow_client.py`, add a corresponding key to the `results` dict in `fetch_proknow_data`, a `self._write_table(...)` call in `write_results_to_db`, and pick an `id_col` that uniquely identifies a row for that table.

## Known limitations (also tracked in README TODO)

- Patient discovery relies on a static CSV of IDs rather than querying Proknow for new patients in the workspace.
- The CWP/EForms booking-form query only works on Windows due to trusted-connection auth.
- CWP only returns the latest booking form, which may be wrong for re-treated patients.
- The hash-based skip-if-unchanged optimization in `check_hash_in_db` is currently a no-op (the early `return` is commented out in `fetch_proknow_data`).

## Instructions

1. When you make code significant changes to the code (that would break the current tests in `./tests/`), make sure to update or add new tests. Tests are run after every git push, so I don't want to deal with broken tests. 