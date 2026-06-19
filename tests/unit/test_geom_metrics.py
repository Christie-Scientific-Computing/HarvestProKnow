"""
Unit tests for utils/geom_metrics.py.

These helpers operate on already-parsed dicts/arrays (not file paths), so
everything here is tested directly with hand-built shapely/numpy inputs --
no DICOM files needed. The one function that does touch files,
_read_image_planes, is tested with pydicom.dcmread mocked rather than real
DICOM fixtures.
"""
import numpy as np
import pytest
from shapely.geometry import Polygon

from utils import geom_metrics as gm


def square(cx, cy, half=1.0):
    """Axis-aligned square polygon centered at (cx, cy) with side 2*half."""
    return Polygon(
        [
            (cx - half, cy - half),
            (cx + half, cy - half),
            (cx + half, cy + half),
            (cx - half, cy + half),
        ]
    )


def square_ring(cx, cy, half=1.0):
    """Ring coordinates (Nx2 array) for the same square, DICOM-contour style."""
    return np.array(
        [
            [cx - half, cy - half],
            [cx + half, cy - half],
            [cx + half, cy + half],
            [cx - half, cy + half],
        ]
    )


# --------------------------------------------------------------------------
# _classify
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,roi_type,expected",
    [
        ("PTV_60", "", gm.ROLE_TARGET),
        ("GTV", "", gm.ROLE_TARGET),
        ("ctv_high", "", gm.ROLE_TARGET),  # case-insensitive
        ("Heart", "", gm.ROLE_OAR),
        ("Lung_L", "", gm.ROLE_OAR),
        ("BODY", "", gm.ROLE_EXCLUDED),
        ("External", "", gm.ROLE_EXCLUDED),
        ("Skin", "EXTERNAL", gm.ROLE_EXCLUDED),
    ],
)
def test_classify(name, roi_type, expected):
    assert gm._classify(name, roi_type) == expected


# --------------------------------------------------------------------------
# _build_slice_polygon / _structure_slices
# --------------------------------------------------------------------------

def test_build_slice_polygon_single_ring():
    geom = gm._build_slice_polygon([square_ring(0, 0)])
    assert geom.area == pytest.approx(4.0)


def test_build_slice_polygon_drops_degenerate_rings():
    too_few_points = np.array([[0, 0], [1, 1]])
    geom = gm._build_slice_polygon([too_few_points])
    assert geom is None


def test_build_slice_polygon_no_rings_returns_none():
    assert gm._build_slice_polygon([]) is None


def test_build_slice_polygon_disjoint_rings_union():
    rings = [square_ring(0, 0), square_ring(10, 10)]
    geom = gm._build_slice_polygon(rings)
    assert geom.area == pytest.approx(8.0)


def test_build_slice_polygon_nested_ring_is_a_hole():
    outer = square_ring(0, 0, half=2.0)   # area 16
    inner = square_ring(0, 0, half=1.0)   # area 4, fully inside outer
    geom = gm._build_slice_polygon([outer, inner])
    assert geom.area == pytest.approx(12.0)  # annulus: 16 - 4


def test_structure_slices_builds_one_polygon_per_z():
    coords = {
        "0.0": [{"data": square_ring(0, 0).tolist()}],
        "5.0": [{"data": square_ring(0, 0).tolist()}],
    }
    slices = gm._structure_slices(coords)
    assert set(slices.keys()) == {0.0, 5.0}
    assert slices[0.0].area == pytest.approx(4.0)


def test_structure_slices_skips_planes_without_data():
    coords = {"0.0": [{"data": []}]}
    assert gm._structure_slices(coords) == {}


def test_structure_slices_merges_rounded_z_collision():
    # Two raw z strings that round to the same z at Z_PRECISION_DP=2.
    coords = {
        "0.001": [{"data": square_ring(0, 0).tolist()}],
        "0.004": [{"data": square_ring(10, 10).tolist()}],
    }
    slices = gm._structure_slices(coords)
    assert list(slices.keys()) == [0.0]
    # symmetric_difference of two disjoint squares == their union.
    assert slices[0.0].area == pytest.approx(8.0)


# --------------------------------------------------------------------------
# _surface_points
# --------------------------------------------------------------------------

def test_surface_points_empty_slices():
    points = gm._surface_points({})
    assert points.shape == (0, 3)


def test_surface_points_includes_z_and_ring_coords():
    slices = {3.0: square(0, 0)}
    points = gm._surface_points(slices)
    assert points.shape[1] == 3
    assert np.all(points[:, 2] == 3.0)
    # The square's 4 corners should all be present in xy.
    xy = set(map(tuple, np.round(points[:, :2], 6)))
    assert (-1.0, -1.0) in xy and (1.0, 1.0) in xy


# --------------------------------------------------------------------------
# _slice_thickness_mm
# --------------------------------------------------------------------------

def test_slice_thickness_mm_uniform_spacing():
    assert gm._slice_thickness_mm(np.array([0, 1, 2, 3])) == pytest.approx(1.0)


def test_slice_thickness_mm_median_of_gaps():
    # gaps are 1 and 2 -> median 1.5
    assert gm._slice_thickness_mm(np.array([0, 1, 3])) == pytest.approx(1.5)


def test_slice_thickness_mm_requires_two_planes():
    assert gm._slice_thickness_mm(np.array([0.0])) is None
    assert gm._slice_thickness_mm(np.array([])) is None


# --------------------------------------------------------------------------
# _assert_axial_orientation
# --------------------------------------------------------------------------

def test_assert_axial_orientation_passes_for_axial():
    iop = np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
    gm._assert_axial_orientation([(0.0, iop)])  # should not raise


def test_assert_axial_orientation_rejects_oblique():
    iop = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 1.0])  # column axis along z
    with pytest.raises(ValueError):
        gm._assert_axial_orientation([(0.0, iop)])


# --------------------------------------------------------------------------
# _volume_mm3 / _centroid_mm
# --------------------------------------------------------------------------

def test_volume_mm3_sums_slice_areas_times_thickness():
    slices = {0.0: square(0, 0), 1.0: square(0, 0)}
    assert gm._volume_mm3(slices, thickness=2.0) == pytest.approx(16.0)  # (4+4)*2


def test_centroid_mm_zero_volume_returns_none():
    assert gm._centroid_mm({}, thickness=1.0, volume_mm3=0.0) is None


def test_centroid_mm_weighted_average_across_slices():
    slices = {0.0: square(0, 0), 10.0: square(0, 0)}
    volume = gm._volume_mm3(slices, thickness=1.0)  # 8.0
    centroid = gm._centroid_mm(slices, thickness=1.0, volume_mm3=volume)
    assert centroid == pytest.approx([0.0, 0.0, 5.0])


# --------------------------------------------------------------------------
# _dice / _relative_overlap
# --------------------------------------------------------------------------

def _structure(slices, thickness):
    volume = gm._volume_mm3(slices, thickness)
    return gm.StructureGeometry(
        name="s", role=gm.ROLE_OAR, slices=slices, volume_mm3=volume,
        centroid_mm=None, surface_points_mm=np.empty((0, 3)),
    )


def test_dice_identical_structures_is_one():
    slices = {0.0: square(0, 0)}
    a = _structure(slices, thickness=1.0)
    b = _structure(dict(slices), thickness=1.0)
    assert gm._dice(a, b, thickness=1.0) == pytest.approx(1.0)


def test_dice_disjoint_structures_is_zero():
    a = _structure({0.0: square(0, 0)}, thickness=1.0)
    b = _structure({0.0: square(10, 10)}, thickness=1.0)
    assert gm._dice(a, b, thickness=1.0) == pytest.approx(0.0)


def test_dice_partial_overlap():
    # square [0,2]x[0,2] vs square [1,3]x[1,3]: overlap is [1,2]x[1,2], area 1.
    a = _structure({0.0: Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])}, thickness=1.0)
    b = _structure({0.0: Polygon([(1, 1), (3, 1), (3, 3), (1, 3)])}, thickness=1.0)
    # volumes: a=4, b=4, denom=8; intersection_area=1*thickness=1; dice=2*1/8
    assert gm._dice(a, b, thickness=1.0) == pytest.approx(0.25)


def test_relative_overlap_target_fully_inside_oar():
    target = _structure({0.0: square(0, 0, half=0.5)}, thickness=1.0)  # area 1
    oar = _structure({0.0: square(0, 0, half=1.0)}, thickness=1.0)     # area 4
    # intersection = target's full area = 1; relative to oar volume (4) -> 0.25
    assert gm._relative_overlap(target, oar, thickness=1.0) == pytest.approx(0.25)


def test_relative_overlap_no_overlap_is_zero():
    target = _structure({0.0: square(0, 0)}, thickness=1.0)
    oar = _structure({0.0: square(10, 10)}, thickness=1.0)
    assert gm._relative_overlap(target, oar, thickness=1.0) == pytest.approx(0.0)


# --------------------------------------------------------------------------
# _surface_distances
# --------------------------------------------------------------------------

def test_surface_distances_empty_inputs_returns_none():
    assert gm._surface_distances(np.empty((0, 3)), np.array([[0, 0, 0]])) == (None, None)
    assert gm._surface_distances(np.array([[0, 0, 0]]), np.empty((0, 3))) == (None, None)


def test_surface_distances_min_and_hd95_for_shifted_squares():
    a_points = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
    b_points = a_points + np.array([2.0, 0.0, 0.0])  # shifted 2mm along x
    min_dist, hd95 = gm._surface_distances(a_points, b_points)
    assert min_dist == pytest.approx(1.0)
    assert hd95 == pytest.approx(2.0)


# --------------------------------------------------------------------------
# StructureGeometry.volume_cc
# --------------------------------------------------------------------------

def test_structure_geometry_volume_cc():
    sg = gm.StructureGeometry(
        name="x", role=gm.ROLE_OAR, slices={}, volume_mm3=2000.0,
        centroid_mm=None, surface_points_mm=np.empty((0, 3)),
    )
    assert sg.volume_cc == pytest.approx(2.0)


# --------------------------------------------------------------------------
# _read_image_planes -- the one function that touches files. We mock
# pydicom.dcmread so no real DICOM content is needed; the files on disk
# just need to exist for glob/isfile to find them.
# --------------------------------------------------------------------------

class _FakeDataset:
    def __init__(self, ipp, iop):
        self.ImagePositionPatient = ipp
        self.ImageOrientationPatient = iop


def test_read_image_planes_reads_position_and_orientation(tmp_path, monkeypatch):
    (tmp_path / "ct1.dcm").write_bytes(b"not real dicom")
    (tmp_path / "ct2.dcm").write_bytes(b"not real dicom")

    fake_planes = {
        str(tmp_path / "ct1.dcm"): _FakeDataset([0.0, 0.0, 0.0], [1, 0, 0, 0, 1, 0]),
        str(tmp_path / "ct2.dcm"): _FakeDataset([0.0, 0.0, 5.0], [1, 0, 0, 0, 1, 0]),
    }

    def fake_dcmread(path, stop_before_pixels=False):
        return fake_planes[path]

    monkeypatch.setattr(gm.pydicom, "dcmread", fake_dcmread)

    planes = gm._read_image_planes(str(tmp_path))
    z_values = sorted(z for z, _ in planes)
    assert z_values == [0.0, 5.0]


def test_read_image_planes_skips_unreadable_files(tmp_path, monkeypatch):
    (tmp_path / "broken.dcm").write_bytes(b"garbage")

    def fake_dcmread(path, stop_before_pixels=False):
        raise Exception("not a dicom file")

    monkeypatch.setattr(gm.pydicom, "dcmread", fake_dcmread)

    assert gm._read_image_planes(str(tmp_path)) == []
