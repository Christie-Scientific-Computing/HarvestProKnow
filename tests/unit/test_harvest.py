"""
Unit tests for harvest.py.

ProknowHarvester.__init__ is side-effect-free (just stores connection
params), so instances are constructed directly with dummy values rather
than via __new__. DB-touching methods are exercised against the FakeCursor/
FakeConnection from conftest.py instead of a real Postgres connection.
"""
import psycopg
import pytest

import harvest
from harvest import ProknowHarvester


def make_harvester(conn=None):
    harvester = ProknowHarvester(
        db_host=None, db_port=None, db_name=None, db_user=None, db_password=None,
    )
    harvester.conn = conn
    return harvester


# --------------------------------------------------------------------------
# read_patient_ids
# --------------------------------------------------------------------------

def test_read_patient_ids_reads_column(tmp_path):
    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("patient_id,other\n1,a\n2,b\n3,c\n")
    harvester = make_harvester()
    assert harvester.read_patient_ids(str(csv_path)) == ["1", "2", "3"]


def test_read_patient_ids_skips_empty_values(tmp_path):
    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("patient_id\n1\n\n3\n")
    harvester = make_harvester()
    assert harvester.read_patient_ids(str(csv_path)) == ["1", "3"]


def test_read_patient_ids_missing_file_raises(tmp_path):
    harvester = make_harvester()
    with pytest.raises(FileNotFoundError):
        harvester.read_patient_ids(str(tmp_path / "does-not-exist.csv"))


# --------------------------------------------------------------------------
# check_hash_in_db
# --------------------------------------------------------------------------

def test_check_hash_in_db_true(fake_cursor, fake_connection):
    fake_cursor._fetchone_result = (True,)
    harvester = make_harvester(conn=fake_connection)
    assert harvester.check_hash_in_db("abc123") is True
    sql, params = fake_cursor.executed[0]
    assert params == ("abc123",)
    assert "patients" in sql


def test_check_hash_in_db_false(fake_cursor, fake_connection):
    fake_cursor._fetchone_result = (False,)
    harvester = make_harvester(conn=fake_connection)
    assert harvester.check_hash_in_db("abc123") is False


# --------------------------------------------------------------------------
# _write_table
# --------------------------------------------------------------------------

def test_write_table_single_column_id(fake_cursor):
    harvester = make_harvester()
    data = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]

    harvester._write_table(fake_cursor, "patients", data)

    assert len(fake_cursor.executemany_calls) == 1
    query, values = fake_cursor.executemany_calls[0]
    assert query == (
        "INSERT INTO patients (id, name) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name"
    )
    assert values == [(1, "a"), (2, "b")]


def test_write_table_composite_id_col(fake_cursor):
    harvester = make_harvester()
    data = [{"dose_id": 1, "structure_name": "PTV", "volume": 5}]

    harvester._write_table(
        fake_cursor, "dvh_data", data, id_col=("dose_id", "structure_name"),
    )

    query, values = fake_cursor.executemany_calls[0]
    assert query == (
        "INSERT INTO dvh_data (dose_id, structure_name, volume) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (dose_id, structure_name) DO UPDATE SET volume = EXCLUDED.volume"
    )
    assert values == [(1, "PTV", 5)]


def test_write_table_empty_data_is_noop(fake_cursor):
    harvester = make_harvester()
    harvester._write_table(fake_cursor, "patients", [])
    assert fake_cursor.executemany_calls == []


# --------------------------------------------------------------------------
# write_results_to_db
# --------------------------------------------------------------------------

def _full_results():
    return {
        "patient_data": [{"id": 1}],
        "treatment_data": [{"id": 2, "MRN": "m"}],
        "dvh_data": [{"dose_id": 1, "structure_name": "PTV", "volume": 5}],
        "geom_metrics": [{"dose_id": 1, "structure_set_id": 1, "target": "PTV", "oar": "Heart", "dice": 0.5}],
    }


def test_write_results_to_db_writes_each_nonempty_bucket_and_commits(fake_cursor, fake_connection):
    harvester = make_harvester(conn=fake_connection)
    harvester.write_results_to_db(_full_results())

    assert len(fake_cursor.executemany_calls) == 4
    assert fake_connection.committed is True
    assert fake_cursor.closed is True


def test_write_results_to_db_skips_empty_buckets(fake_cursor, fake_connection):
    harvester = make_harvester(conn=fake_connection)
    results = _full_results()
    results["dvh_data"] = []
    results["geom_metrics"] = []

    harvester.write_results_to_db(results)

    assert len(fake_cursor.executemany_calls) == 2


def test_write_results_to_db_rolls_back_and_reraises_on_db_error(fake_connection):
    from conftest import FakeCursor

    cursor = FakeCursor(raise_on_execute=psycopg.Error("write failed"))
    fake_connection.cursor_obj = cursor
    harvester = make_harvester(conn=fake_connection)

    with pytest.raises(psycopg.Error):
        harvester.write_results_to_db(_full_results())

    assert fake_connection.rolled_back is True
    assert cursor.closed is True


def test_write_results_to_db_requires_connection():
    harvester = make_harvester(conn=None)
    with pytest.raises(RuntimeError):
        harvester.write_results_to_db(_full_results())


# --------------------------------------------------------------------------
# fetch_proknow_data
# --------------------------------------------------------------------------

class FakeAskProKnow:
    def __init__(
        self, patient_id, *, patient_summary="found", patient_hash="hash1",
        patient_data=None, treatment_data=None, dvh_data=None, geom_metrics=None,
        raise_method=None,
    ):
        self.patient_id = patient_id
        self.patient_summary = patient_summary
        self.patient_hash = patient_hash
        self._patient_data = patient_data if patient_data is not None else {"id": patient_id}
        self._treatment_data = treatment_data or []
        self._dvh_data = dvh_data or []
        self._geom_metrics = geom_metrics or []
        self._raise_method = raise_method

    def _maybe_raise(self, name):
        if self._raise_method == name:
            raise RuntimeError("boom")

    def get_patient_data(self):
        self._maybe_raise("get_patient_data")
        return self._patient_data

    def get_treatment_data(self):
        self._maybe_raise("get_treatment_data")
        return self._treatment_data

    def get_dvh_data(self):
        self._maybe_raise("get_dvh_data")
        return self._dvh_data

    def get_geometrical_metrics(self):
        self._maybe_raise("get_geometrical_metrics")
        return self._geom_metrics


def test_fetch_proknow_data_returns_none_when_patient_not_found(monkeypatch, fake_cursor, fake_connection):
    monkeypatch.setattr(
        harvest, "AskProKnow", lambda patient_id: FakeAskProKnow(patient_id, patient_summary=None),
    )
    harvester = make_harvester(conn=fake_connection)

    assert harvester.fetch_proknow_data("missing-patient") is None
    # check_hash_in_db should never be reached for a not-found patient.
    assert fake_cursor.executed == []


def test_fetch_proknow_data_returns_none_when_a_getter_raises(monkeypatch, fake_cursor, fake_connection):
    fake_cursor._fetchone_result = (False,)
    monkeypatch.setattr(
        harvest, "AskProKnow",
        lambda patient_id: FakeAskProKnow(patient_id, raise_method="get_dvh_data"),
    )
    harvester = make_harvester(conn=fake_connection)

    assert harvester.fetch_proknow_data("p1") is None


def test_fetch_proknow_data_happy_path(monkeypatch, fake_cursor, fake_connection):
    fake_cursor._fetchone_result = (False,)
    monkeypatch.setattr(
        harvest, "AskProKnow",
        lambda patient_id: FakeAskProKnow(
            patient_id,
            patient_data={"id": patient_id, "MRN": patient_id},
            treatment_data=[{"id": "d1"}],
            dvh_data=[{"dose_id": "d1", "structure_name": "PTV"}],
            geom_metrics=[{"dose_id": "d1", "structure_set_id": "ss1", "target": "PTV", "oar": "Heart"}],
        ),
    )
    harvester = make_harvester(conn=fake_connection)

    results = harvester.fetch_proknow_data("p1")

    assert results == {
        "patient_data": [{"id": "p1", "MRN": "p1"}],
        "treatment_data": [{"id": "d1"}],
        "dvh_data": [{"dose_id": "d1", "structure_name": "PTV"}],
        "geom_metrics": [{"dose_id": "d1", "structure_set_id": "ss1", "target": "PTV", "oar": "Heart"}],
    }


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def test_run_writes_results_only_for_patients_with_data(monkeypatch):
    harvester = make_harvester()
    monkeypatch.setattr(harvester, "read_patient_ids", lambda path: ["p1", "p2"])

    fetched = {"p1": {"patient_data": [{"id": 1}]}, "p2": None}
    written = []

    monkeypatch.setattr(harvester, "fetch_proknow_data", lambda pid: fetched[pid])
    monkeypatch.setattr(harvester, "write_results_to_db", lambda results: written.append(results))

    harvester.run("ignored.csv")

    assert written == [{"patient_data": [{"id": 1}]}]
