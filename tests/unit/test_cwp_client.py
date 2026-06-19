"""
Unit tests for api/cwp_client.py.

AskCWP.__init__ reads credentials from a file path (env var CHRISTIE_CREDS)
and immediately opens a pyodbc connection, so most tests bypass __init__ via
__new__ and set only the attributes the method under test actually needs.
"""
import json

import pytest

from api.cwp_client import AskCWP


def make_cwp(**attrs):
    """Construct an AskCWP instance without running __init__."""
    cwp = AskCWP.__new__(AskCWP)
    for key, value in attrs.items():
        setattr(cwp, key, value)
    return cwp


RAW_ROW = {
    "ChristieNo": "12345",
    "PatientSex": "F",
    "PatientAge": 54,
    "DecisionToTreatDate": "2024-01-01",
    "Diagnosis": "Breast cancer",
    "TreatmentIntent": "Curative",
    "TreatmentSite": "Breast",
    "PrimaryTreatmentSite": "Breast",
    "MetastaticSite": None,
    "RegionalNodalSite": None,
    "SideOfNodalTreatmentSite": None,
    "TreatmentBy": "EBRT",
    "TreatmentCategory": "Primary",
    "Modality": "Photon",
    "Fractions": 15,
    "Fractionation": "Standard",
    "Chemotherapy": "No",
    "HasThePatientHadPreviousRadiotherapy": "No",
    "DoesThePatientHaveAPacemaker": "No",
}


def test_filter_data_maps_expected_fields():
    cwp = make_cwp()
    result = cwp.filter_data(RAW_ROW)
    assert result == {
        "MRN": "12345",
        "sex": "F",
        "age": 54,
        "decision_to_treat_date": "2024-01-01",
        "diagnosis": "Breast cancer",
        "treatment_intent": "Curative",
        "treatment_site": "Breast",
        "primary_site": "Breast",
        "metastatic_site": None,
        "regional_node_site": None,
        "side_of_nodal_treatment": None,
        "treatment_by": "EBRT",
        "treatment_category": "Primary",
        "treatment_modality": "Photon",
        "fractions": 15,
        "fractionation": "Standard",
        "chemotherapy": "No",
        "previous_radiotherapy": "No",
        "has_pacemaker": "No",
    }


class FakeODBCCursor:
    def __init__(self, columns, rows):
        self.description = [(c,) for c in columns]
        self._rows = rows
        self.executed = None

    def execute(self, sql, patient_id):
        self.executed = (sql, patient_id)

    def fetchall(self):
        return self._rows


class FakeODBCConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def test_fetch_data_zips_columns_and_rows_and_passes_patient_id():
    cursor = FakeODBCCursor(columns=["ChristieNo", "PatientSex"], rows=[("999", "M")])
    cwp = make_cwp(conn=FakeODBCConnection(cursor), patient_id="999")

    result = cwp.fetch_data()

    assert result == [{"ChristieNo": "999", "PatientSex": "M"}]
    sql, passed_id = cursor.executed
    assert "usp_GetLatestRadiotherapyBooking" in sql
    assert passed_id == "999"


def test_get_booking_data_returns_none_when_no_rows(monkeypatch):
    cwp = make_cwp()
    monkeypatch.setattr(cwp, "fetch_data", lambda: [])
    assert cwp.get_booking_data() is None


def test_get_booking_data_wraps_filtered_rows(monkeypatch):
    cwp = make_cwp()
    monkeypatch.setattr(cwp, "fetch_data", lambda: [RAW_ROW])
    result = cwp.get_booking_data()
    assert result["data"] == [cwp.filter_data(RAW_ROW)]


def test_connect_builds_connection_string_and_returns_connection(monkeypatch):
    cwp = make_cwp(
        driver="{FreeTDS}",
        server="CHT-BI-DEV01",
        database="EForms",
        credentials={"username": "svc", "password": "secret"},
    )
    captured = {}

    def fake_connect(conn_string):
        captured["conn_string"] = conn_string
        return "fake-connection"

    monkeypatch.setattr("api.cwp_client.pyodbc.connect", fake_connect)

    result = cwp.connect()

    assert result == "fake-connection"
    assert "SERVER=CHT-BI-DEV01" in captured["conn_string"]
    assert "DATABASE=EForms" in captured["conn_string"]
    assert "UID=svc" in captured["conn_string"]
    assert "PWD=secret" in captured["conn_string"]


def test_connect_logs_and_reraises_on_failure(monkeypatch):
    cwp = make_cwp(
        driver="{FreeTDS}",
        server="CHT-BI-DEV01",
        database="EForms",
        credentials={"username": "svc", "password": "secret"},
    )

    def fake_connect(conn_string):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr("api.cwp_client.pyodbc.connect", fake_connect)

    with pytest.raises(RuntimeError, match="network unreachable"):
        cwp.connect()


def test_init_loads_credentials_and_connects(tmp_path, monkeypatch):
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(json.dumps({"username": "svc", "password": "secret"}))
    monkeypatch.setenv("CHRISTIE_CREDS", str(creds_path))
    monkeypatch.setattr("api.cwp_client.pyodbc.connect", lambda conn_string: "fake-connection")

    cwp = AskCWP(patient_id="42")

    assert cwp.patient_id == "42"
    assert cwp.credentials == {"username": "svc", "password": "secret"}
    assert cwp.conn == "fake-connection"
