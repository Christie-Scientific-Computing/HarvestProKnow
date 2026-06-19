# Tests

Unit tests for HarvestProKnow. Nothing outside this directory is touched —
no root-level `pytest.ini`/`pyproject.toml`, no changes to the existing
codebase.

## Running

```bash
pip install -r tests/requirements-test.txt
python -m pytest tests/ -v
```

Run from the repo root. `tests/conftest.py` puts the repo root on `sys.path`
itself, so `harvest`, `api.*`, and `utils.*` import correctly even though
they're plain scripts/namespace packages with no `setup.py`/`pyproject.toml`.

To run a single file or test:

```bash
python -m pytest tests/unit/test_geom_metrics.py -v
python -m pytest tests/unit/test_harvest.py::test_run_writes_results_only_for_patients_with_data -v
```

## Strategy

This codebase talks to three external systems that aren't reachable from a
dev box or CI: the Proknow cloud API, the shared `BigDB` Postgres instance,
and (for the disabled CWP path) a Windows-only SQL Server via trusted-
connection ODBC. Rather than skip testing entirely, these tests mock at the
boundary of each system and verify *our* logic:

- **Postgres** — `FakeCursor`/`FakeConnection` in `conftest.py` stand in for
  a `psycopg` connection, recording executed SQL/params instead of hitting
  a real database.
- **Proknow SDK** — `FakeProKnow`/`FakePatientSummary`/`FakePatientItem`/
  `FakeEntity` in `conftest.py` cover only the slice of the SDK surface the
  code actually calls (`patients.query`, `find_entities`, `.download()`,
  etc.), so `AskProKnow` never makes a network call in tests.
- **DICOM parsing** — `utils/geom_metrics.py`'s helpers operate on already-
  parsed dicts/arrays (not file paths), so they're tested directly with
  hand-built shapely/numpy inputs, no DICOM files needed. The few places
  that do read files (`dicomparser.DicomParser`, `dvhcalc.get_dvh`,
  `pydicom.dcmread`, `gm._read_image_planes`) are mocked at their call
  sites in the orchestration tests (`test_proknow_client.py`) rather than
  backed by synthetic DICOM fixtures.

## What's not covered

- Real Proknow network behavior (auth, pagination, rate limits).
- Real Postgres upsert semantics (constraint behavior, transaction
  isolation) — `FakeCursor`/`FakeConnection` only verify the SQL/values our
  code *sends*.
- The Windows-only CWP/EForms trusted-connection path itself (only the
  pure logic around it is tested, with `pyodbc.connect` mocked).
- Correctness of `dicompylercore`/`pydicom`/`shapely` themselves — those are
  mocked or used directly with simple inputs, not re-tested.
