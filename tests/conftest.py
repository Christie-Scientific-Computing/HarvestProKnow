"""
Shared test fixtures and fakes for the HarvestProKnow test suite.

Everything here mocks at the boundary of an external system (Postgres,
the ProKnow SDK) so unit tests can exercise our own logic without a live
database or live Proknow credentials.
"""
import sys
from pathlib import Path

import pytest

# Repo root is the parent of this tests/ dir. Modules under test (harvest,
# api.*, utils.*) live there as plain scripts/namespace packages with no
# pyproject.toml/setup.py, so it must be on sys.path for `import harvest`
# etc. to work regardless of how pytest is invoked.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------
# Fake psycopg connection / cursor
# --------------------------------------------------------------------------

class FakeCursor:
    """Records executed SQL/params instead of touching a real database."""

    def __init__(self, fetchone_result=None, raise_on_execute=None):
        self.executed = []      # list of (sql, params) from execute()
        self.executemany_calls = []  # list of (sql, values) from executemany()
        self.closed = False
        self._fetchone_result = fetchone_result
        self._raise_on_execute = raise_on_execute

    def execute(self, sql, params=None):
        if self._raise_on_execute:
            raise self._raise_on_execute
        self.executed.append((sql, params))
        return self

    def executemany(self, sql, values):
        if self._raise_on_execute:
            raise self._raise_on_execute
        self.executemany_calls.append((sql, list(values)))

    def fetchone(self):
        return self._fetchone_result

    def close(self):
        self.closed = True


class FakeConnection:
    """Records commit/rollback calls; hands out a single shared FakeCursor."""

    def __init__(self, cursor=None):
        self.cursor_obj = cursor or FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_cursor():
    return FakeCursor()


@pytest.fixture
def fake_connection(fake_cursor):
    return FakeConnection(cursor=fake_cursor)


# --------------------------------------------------------------------------
# Fake ProKnow SDK surface
#
# Only covers the slice of the SDK that api/proknow_client.py actually
# calls: pk.patients.query(...), summary.get(), patient.find_entities(...),
# entity.get()/.download(tmpdir), and the .data/.mrn/.id/etc attributes.
# --------------------------------------------------------------------------

class FakeEntity:
    """Stand-in for a ProKnow entity summary/item (image set, structure set, plan, dose)."""

    def __init__(self, data, download_path=None, children=None):
        self.data = data
        self._download_path = download_path
        # entity.get() commonly returns itself fully "hydrated" in these tests
        self._children = children or []

    def get(self):
        return self

    def download(self, tmpdir):
        # Real SDK downloads into tmpdir and returns the file path.
        return self._download_path


class FakePatientItem:
    """Stand-in for ProKnow's PatientItem (the hydrated patient)."""

    def __init__(self, id, mrn, name, birth_date, sex, data, entities=None):
        self.id = id
        self.mrn = mrn
        self.name = name
        self.birth_date = birth_date
        self.sex = sex
        self.data = data
        self._entities = entities or []

    def find_entities(self, predicate):
        return [e for e in self._entities if predicate(e)]


class FakePatientSummary:
    """Stand-in for ProKnow's PatientSummary (returned by patients.query)."""

    def __init__(self, patient_item, summary_data):
        self._patient_item = patient_item
        self.data = summary_data

    def get(self):
        return self._patient_item


class FakePatientsAPI:
    def __init__(self, query_result):
        self._query_result = query_result

    def query(self, workspace, search=None):
        return self._query_result


class FakeProKnow:
    """Stand-in for the top-level `ProKnow` client object."""

    def __init__(self, url, credentials, query_result=None):
        self.patients = FakePatientsAPI(query_result or [])


def make_patient_summary(
    patient_id="12345",
    name="Test Patient",
    birth_date="1980-01-01",
    sex="M",
    entities=None,
    extra_patient_data=None,
    extra_summary_data=None,
):
    """Build a FakePatientSummary -> FakePatientItem pair with sane defaults
    matching the fields AskProKnow.get_patient_data()/_calc_patient_hash() read.
    """
    patient_data = {
        "collections": [],
        **(extra_patient_data or {}),
    }
    patient_item = FakePatientItem(
        id="patient-item-id",
        mrn=patient_id,
        name=name,
        birth_date=birth_date,
        sex=sex,
        data=patient_data,
        entities=entities,
    )
    summary_data = {
        "created_at": "2024-01-01T00:00:00Z",
        "clinical_date": "2024-01-01",
        "study_count": 1,
        "ct_count": 1,
        "mr_count": 0,
        "structure_set_count": 1,
        "plan_count": 1,
        "dose_count": 1,
        "unknown_count": 0,
        **(extra_summary_data or {}),
    }
    return FakePatientSummary(patient_item, summary_data)
