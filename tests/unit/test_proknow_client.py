"""
Unit tests for api/proknow_client.py.

AskProKnow.__init__ opens a real ProKnow SDK client and immediately queries
a workspace, so most tests bypass __init__ via __new__ and set only the
attributes the method under test needs. DICOM-file-touching calls
(dicomparser.DicomParser, dvhcalc.get_dvh, pydicom.dcmread,
gm._read_image_planes) are mocked at their call sites rather than backed by
real/synthetic DICOM files -- see tests/README.md for the rationale.
"""
import logging
from datetime import date
from hashlib import sha256

import numpy as np
import pydicom
import pytest

import api.proknow_client as pk_module
from api.proknow_client import AskProKnow

from conftest import FakeEntity, FakePatientItem, FakeProKnow, make_patient_summary


def make_ask(**attrs):
    """Construct an AskProKnow instance without running __init__."""
    ask = AskProKnow.__new__(AskProKnow)
    for key, value in attrs.items():
        setattr(ask, key, value)
    return ask


def square_ring(cx, cy, half=1.0):
    return [
        [cx - half, cy - half],
        [cx + half, cy - half],
        [cx + half, cy + half],
        [cx - half, cy + half],
    ]


# --------------------------------------------------------------------------
# _calc_patient_hash
# --------------------------------------------------------------------------

def test_calc_patient_hash_is_deterministic():
    data = {"a": 1, "b": [1, 2, 3]}
    expected = sha256(b'{"a": 1, "b": [1, 2, 3]}').hexdigest()
    assert AskProKnow._calc_patient_hash(data) == expected
    assert AskProKnow._calc_patient_hash(data) == AskProKnow._calc_patient_hash(dict(data))


def test_calc_patient_hash_differs_for_different_data():
    h1 = AskProKnow._calc_patient_hash({"a": 1})
    h2 = AskProKnow._calc_patient_hash({"a": 2})
    assert h1 != h2


# --------------------------------------------------------------------------
# find_patient
# --------------------------------------------------------------------------

def test_find_patient_returns_none_and_warns_when_not_found(caplog):
    ask = make_ask(pk=FakeProKnow(None, None, query_result=[]), workspace="WS")
    with caplog.at_level(logging.WARNING):
        result = ask.find_patient("12345")
    assert result is None
    assert "not found in Proknow" in caplog.text


def test_find_patient_returns_single_match():
    summary = make_patient_summary(patient_id="12345")
    ask = make_ask(pk=FakeProKnow(None, None, query_result=[summary]), workspace="WS")
    assert ask.find_patient("12345") is summary


def test_find_patient_returns_none_when_ambiguous():
    # Current behavior: >1 result falls through with no explicit return (None).
    # This is the README TODO item about not relying on patient IDs.
    s1 = make_patient_summary(patient_id="12345")
    s2 = make_patient_summary(patient_id="12345")
    ask = make_ask(pk=FakeProKnow(None, None, query_result=[s1, s2]), workspace="WS")
    assert ask.find_patient("12345") is None


# --------------------------------------------------------------------------
# get_patient_data
# --------------------------------------------------------------------------

def test_get_patient_data_builds_expected_payload():
    summary = make_patient_summary(
        patient_id="12345",
        extra_patient_data={"collections": [{"name": "Cohort A"}, {"name": "Cohort B"}]},
    )
    ask = make_ask(
        patient=summary.get(),
        patient_summary=summary,
        patient_hash="deadbeef",
        patient_id="12345",
    )
    payload = ask.get_patient_data()
    assert payload["sha256"] == "deadbeef"
    assert payload["MRN"] == "12345"
    assert payload["collections"] == ["Cohort A", "Cohort B"]
    assert payload["study_count"] == 1


def test_get_patient_data_collections_none_when_empty():
    summary = make_patient_summary(patient_id="12345", extra_patient_data={"collections": []})
    ask = make_ask(
        patient=summary.get(), patient_summary=summary, patient_hash="x", patient_id="12345",
    )
    assert ask.get_patient_data()["collections"] is None


def test_get_patient_data_asserts_mrn_matches_requested_id():
    summary = make_patient_summary(patient_id="12345")
    ask = make_ask(
        patient=summary.get(), patient_summary=summary, patient_hash="x", patient_id="WRONG-ID",
    )
    with pytest.raises(AssertionError):
        ask.get_patient_data()


# --------------------------------------------------------------------------
# _filter_doses
# --------------------------------------------------------------------------

def test_filter_doses_keeps_only_plan_summation_type(monkeypatch):
    plan_dose = FakeEntity({"id": "d-plan", "type": "dose"}, download_path="/tmp/plan_dose.dcm")
    beam_dose = FakeEntity({"id": "d-beam", "type": "dose"}, download_path="/tmp/beam_dose.dcm")
    patient = FakePatientItem(
        id="p", mrn="MRN1", name="N", birth_date="2000-01-01", sex="M", data={},
        entities=[plan_dose, beam_dose],
    )
    ask = make_ask(patient=patient, _dose_dataset_cache={})

    def fake_dcmread(path):
        ds = pydicom.Dataset()
        ds.DoseSummationType = "PLAN" if path == "/tmp/plan_dose.dcm" else "BEAM"
        return ds

    monkeypatch.setattr(pk_module.pydicom, "dcmread", fake_dcmread)

    assert ask._filter_doses() == ["d-plan"]
    # Only the accepted dose's parsed Dataset is cached for reuse by
    # get_dose_metrics()/calculate_dvhs() -- the rejected BEAM dose isn't.
    assert list(ask._dose_dataset_cache.keys()) == ["d-plan"]
    assert ask._dose_dataset_cache["d-plan"].DoseSummationType == "PLAN"


# --------------------------------------------------------------------------
# get_data_from_plan
# --------------------------------------------------------------------------

def _plan_dataset(**tags):
    ds = pydicom.Dataset()
    for key, value in tags.items():
        setattr(ds, key, value)
    return ds


def test_get_data_from_plan_prefers_manufacturer_model_name(monkeypatch):
    ds = _plan_dataset(ManufacturerModelName="TrueBeam", Manufacturer="Varian",
                        RTPlanLabel="Plan1", RTPlanDate="20240115")
    monkeypatch.setattr(pk_module.pydicom, "dcmread", lambda path: ds)
    data = AskProKnow.get_data_from_plan("ignored-path")
    assert data["planning_system"] == "TrueBeam"
    assert data["plan_name"] == "Plan1"


def test_get_data_from_plan_falls_back_to_manufacturer(monkeypatch):
    ds = _plan_dataset(Manufacturer="Varian", RTPlanLabel="Plan1", RTPlanDate="20240115")
    monkeypatch.setattr(pk_module.pydicom, "dcmread", lambda path: ds)
    data = AskProKnow.get_data_from_plan("ignored-path")
    assert data["planning_system"] == "Varian"


def test_get_data_from_plan_falls_back_to_series_date(monkeypatch):
    ds = _plan_dataset(Manufacturer="Varian", RTPlanLabel="Plan1", SeriesDate="20240301")
    monkeypatch.setattr(pk_module.pydicom, "dcmread", lambda path: ds)
    data = AskProKnow.get_data_from_plan("ignored-path")
    assert data["plan_date"] == date(2024, 3, 1)


def test_get_data_from_plan_parses_month_correctly(monkeypatch):
    ds = _plan_dataset(Manufacturer="Varian", RTPlanLabel="Plan1", RTPlanDate="20241231")
    monkeypatch.setattr(pk_module.pydicom, "dcmread", lambda path: ds)
    data = AskProKnow.get_data_from_plan("ignored-path")
    assert data["plan_date"] == date(2024, 12, 31)


def test_get_data_from_plan_dose_and_fraction_sequences(monkeypatch):
    dose_ref = pydicom.Dataset()
    dose_ref.TargetPrescriptionDose = 60.0
    fraction_group = pydicom.Dataset()
    fraction_group.NumberOfFractionsPlanned = 30

    ds = _plan_dataset(
        Manufacturer="Varian", RTPlanLabel="Plan1", RTPlanDate="20240115",
        DoseReferenceSequence=pydicom.Sequence([dose_ref]),
        FractionGroupSequence=pydicom.Sequence([fraction_group]),
        TreatmentSites="Pelvis",
    )
    monkeypatch.setattr(pk_module.pydicom, "dcmread", lambda path: ds)
    data = AskProKnow.get_data_from_plan("ignored-path")
    assert data["prescribed_dose"] == 60.0
    assert data["prescribed_fractions"] == 30
    assert data["treatment_site"] == "Pelvis"


def test_get_data_from_plan_missing_sequences_are_none(monkeypatch):
    ds = _plan_dataset(Manufacturer="Varian", RTPlanLabel="Plan1", RTPlanDate="20240115")
    monkeypatch.setattr(pk_module.pydicom, "dcmread", lambda path: ds)
    data = AskProKnow.get_data_from_plan("ignored-path")
    assert data["prescribed_dose"] is None
    assert data["prescribed_fractions"] is None


# --------------------------------------------------------------------------
# get_treatment_data
# --------------------------------------------------------------------------

def test_get_treatment_data_links_dose_to_plan(monkeypatch):
    dose_entity = FakeEntity(
        {"id": "dose-1", "type": "dose", "image_set_id": "img-1",
         "structure_set_id": "ss-1", "plan_id": "plan-1"}
    )
    plan_entity = FakeEntity({"id": "plan-1", "type": "plan"}, download_path="/tmp/plan1.dcm")
    patient = FakePatientItem(
        id="p", mrn="MRN1", name="N", birth_date="2000-01-01", sex="M", data={},
        entities=[dose_entity, plan_entity],
    )
    ask = make_ask(patient=patient, accepted_dose_ids=["dose-1"])
    monkeypatch.setattr(
        ask, "get_data_from_plan",
        lambda filepath: {
            "planning_system": "Varian", "plan_name": "P1", "plan_date": date(2024, 1, 1),
            "prescribed_dose": 60.0, "prescribed_fractions": 30, "treatment_site": None,
        },
    )

    result = ask.get_treatment_data()

    assert result == [{
        "id": "dose-1", "MRN": "MRN1", "image_set_id": "img-1",
        "structure_set_id": "ss-1", "plan_id": "plan-1",
        "planning_system": "Varian", "plan_name": "P1", "plan_date": date(2024, 1, 1),
        "prescribed_dose": 60.0, "prescribed_fractions": 30, "treatment_site": None,
    }]


# --------------------------------------------------------------------------
# _download_image_slices
# --------------------------------------------------------------------------

class FakeRequestor:
    def __init__(self):
        self.calls = []

    def stream(self, url, path):
        self.calls.append((url, path))


class FakeImageSet:
    def __init__(self):
        self.data = {
            "data": {"images": [
                {"uid": "uid-b", "pos": 2.0, "id": "img-b"},
                {"uid": "uid-a", "pos": 1.0, "id": "img-a"},
            ]},
            "modality": "CT",
        }
        self._requestor = FakeRequestor()
        self._workspace_id = "ws1"
        self._id = "imgset1"


def test_download_image_slices_sorts_by_position_and_streams_each(tmp_path):
    image_set = FakeImageSet()

    paths = AskProKnow._download_image_slices(image_set, str(tmp_path), indices=[0, 1])

    # Sorted by position: uid-a (pos 1.0) before uid-b (pos 2.0), even though
    # uid-b appears first in the unsorted image list.
    assert [p.split("/")[-1] for p in paths] == ["CT.uid-a", "CT.uid-b"]
    assert image_set._requestor.calls == [
        ("/workspaces/ws1/imagesets/imgset1/images/img-a/dicom", paths[0]),
        ("/workspaces/ws1/imagesets/imgset1/images/img-b/dicom", paths[1]),
    ]


# --------------------------------------------------------------------------
# calculate_dvhs
# --------------------------------------------------------------------------

class FakeDVHResult:
    def __init__(self, counts):
        self.counts = np.array(counts)


class FakeDVH:
    def __init__(self, volume, counts):
        self.volume = volume
        self._counts = counts

    @property
    def relative_volume(self):
        return FakeDVHResult(self._counts)


def test_calculate_dvhs_drops_zero_volume_structures(monkeypatch):
    ask = make_ask()

    fake_ds_struct = object()
    fake_ds_dose = object()  # already-parsed by _filter_doses; not re-read from disk here.

    def fake_dcmread(path):
        return {"structpath": fake_ds_struct}[path]

    class FakeDicomParser:
        def __init__(self, dataset):
            assert dataset is fake_ds_struct

        def GetStructures(self):
            return {1: {"name": "PTV"}, 2: {"name": "ZeroVolStruct"}}

    def fake_get_dvh(ds_struct, ds_dose, idx, interpolation_resolution=1.0):
        assert ds_struct is fake_ds_struct
        assert ds_dose is fake_ds_dose
        if idx == 1:
            return FakeDVH(volume=12.34567, counts=[1.0, 0.9, 0.5, 0.0])
        return FakeDVH(volume=0.0, counts=[0.0])

    monkeypatch.setattr(pk_module.pydicom, "dcmread", fake_dcmread)
    monkeypatch.setattr(pk_module.dicomparser, "DicomParser", FakeDicomParser)
    monkeypatch.setattr(pk_module.dvhcalc, "get_dvh", fake_get_dvh)

    result = ask.calculate_dvhs("dose-1", "structpath", fake_ds_dose)

    assert len(result) == 1
    assert result[0] == {
        "dose_id": "dose-1", "structure_name": "PTV",
        "cumulative_dvh": [1.0, 0.9, 0.5, 0.0], "volume": 12.3457, "bin_width": 0.01,
    }


# --------------------------------------------------------------------------
# calculate_geometrical_metrics
# --------------------------------------------------------------------------

def test_calculate_geometrical_metrics_orchestration(monkeypatch):
    ask = make_ask()

    class FakeStructParser:
        def __init__(self, path):
            pass

        def GetStructures(self):
            return {1: {"name": "PTV", "type": ""}, 2: {"name": "Heart", "type": ""}}

        def GetStructureCoordinates(self, roi_number):
            half = 0.5 if roi_number == 1 else 2.0
            return {"0.0": [{"data": square_ring(0, 0, half=half)}]}

    monkeypatch.setattr(pk_module.dicomparser, "DicomParser", FakeStructParser)
    monkeypatch.setattr(
        pk_module.gm, "_read_image_planes",
        lambda image_dir: [
            (0.0, np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])),
            (5.0, np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])),
        ],
    )

    result = ask.calculate_geometrical_metrics(
        structure_set_id="ss-1", image_set_id="img-1",
        struct_path="ignored-struct-path", image_dir="ignored-image-dir",
    )

    assert result["structure_set_id"] == "ss-1"
    assert result["image_set_id"] == "img-1"
    assert result["slice_thickness_mm"] == pytest.approx(5.0)
    assert len(result["pairwise_metrics"]) == 1
    pm = result["pairwise_metrics"][0]
    assert pm["target"] == "PTV"
    assert pm["oar"] == "Heart"


# --------------------------------------------------------------------------
# get_dose_metrics
# --------------------------------------------------------------------------

def test_get_dose_metrics_orchestrates_dvh_and_geometry_per_accepted_dose(monkeypatch):
    dose_entity = FakeEntity(
        {"id": "d1", "type": "dose", "structure_set_id": "ss1", "image_set_id": "img1"},
    )
    ss_entity = FakeEntity({"id": "ss1", "type": "structure_set"}, download_path="/tmp/ss1-struct.dcm")
    img_entity = FakeEntity({"id": "img1", "type": "image_set"})
    patient = FakePatientItem(
        id="p", mrn="MRN1", name="N", birth_date="2000-01-01", sex="M", data={},
        entities=[dose_entity, ss_entity, img_entity],
    )
    fake_ds_dose = object()  # already parsed by _filter_doses; get_dose_metrics shouldn't re-download it.
    ask = make_ask(patient=patient, accepted_dose_ids=["d1"], _dose_dataset_cache={"d1": fake_ds_dose})

    monkeypatch.setattr(ask, "_download_image_slices", lambda image_set, tmpdir, indices: [])
    monkeypatch.setattr(
        ask, "calculate_dvhs",
        lambda dose_id, structpath, ds_dose: [{"dose_id": dose_id, "structure_name": "PTV"}],
    )
    monkeypatch.setattr(
        ask, "calculate_geometrical_metrics",
        lambda structure_set_id, image_set_id, struct_path, image_dir: {
            "pairwise_metrics": [{"target": "PTV", "oar": "Heart"}],
        },
    )

    dvh_data, geom_metrics = ask.get_dose_metrics()

    assert dvh_data == [{"dose_id": "d1", "structure_name": "PTV"}]
    assert geom_metrics == [
        {"target": "PTV", "oar": "Heart", "dose_id": "d1", "structure_set_id": "ss1"},
    ]


def test_get_dose_metrics_skips_doses_not_in_accepted_dose_ids(monkeypatch):
    accepted = FakeEntity(
        {"id": "d1", "type": "dose", "structure_set_id": "ss1", "image_set_id": "img1"},
    )
    rejected = FakeEntity(
        {"id": "d2", "type": "dose", "structure_set_id": "ss2", "image_set_id": "img2"},
    )
    ss_entity = FakeEntity({"id": "ss1", "type": "structure_set"}, download_path="/tmp/ss1-struct.dcm")
    img_entity = FakeEntity({"id": "img1", "type": "image_set"})
    patient = FakePatientItem(
        id="p", mrn="MRN1", name="N", birth_date="2000-01-01", sex="M", data={},
        entities=[accepted, rejected, ss_entity, img_entity],
    )
    # d2 is excluded, e.g. because _filter_doses found its DoseSummationType == "BEAM"
    # (and so was never added to _dose_dataset_cache either).
    ask = make_ask(patient=patient, accepted_dose_ids=["d1"], _dose_dataset_cache={"d1": object()})

    calls = []
    monkeypatch.setattr(ask, "_download_image_slices", lambda image_set, tmpdir, indices: [])
    monkeypatch.setattr(
        ask, "calculate_dvhs",
        lambda dose_id, structpath, ds_dose: calls.append(dose_id) or [],
    )
    monkeypatch.setattr(
        ask, "calculate_geometrical_metrics",
        lambda structure_set_id, image_set_id, struct_path, image_dir: {"pairwise_metrics": []},
    )

    ask.get_dose_metrics()

    assert calls == ["d1"]
