"""Tests for send-local-scp with synthetic data and an in-process test SCP.

  python -m pytest .claude/skills/send-local-scp/tests -q

Optional: NII2ONCO_REPO=<path to nii2OncoNii repo> also checks the geometry with
the existing nii2OncoNii DICOM->NIfTI code (utils/affine_utils.py) and builds
Onco NIfTI inputs with its own converter.
"""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import nibabel as nib
import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (CTImageStorage, ExplicitVRLittleEndian, ImplicitVRLittleEndian, MRImageStorage,
                         RLELossless, generate_uid)
from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import Verification

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import nifti_to_dicom as n2d  # noqa: E402
import send_local_scp as slscp  # noqa: E402

REPO = os.environ.get("NII2ONCO_REPO")


# --------------------------------------------------------------------------- #
# test SCP
# --------------------------------------------------------------------------- #
class LocalTestSCP:
    def __init__(self, out_dir: Path, restricted: bool = False, fail_sop_uids=()):
        self.out_dir = out_dir
        self.received: dict[str, Dataset] = {}
        self.calling_aes = set()
        self.fail_sop_uids = set(fail_sop_uids)
        self.lock = threading.Lock()
        ae = AE(ae_title="TESTSCP")
        if restricted:  # like OncoStudio's local SCP: CT only here, uncompressed transfer syntaxes only
            ae.add_supported_context(CTImageStorage, [ExplicitVRLittleEndian, ImplicitVRLittleEndian])
        else:
            ae.supported_contexts = StoragePresentationContexts
        ae.add_supported_context(Verification)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = ae.start_server(("127.0.0.1", self.port), block=False,
                                      evt_handlers=[(evt.EVT_C_STORE, self._on_store)])

    def _on_store(self, event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        uid = str(ds.SOPInstanceUID)
        if uid in self.fail_sop_uids:
            return 0xA700
        with self.lock:
            self.received[uid] = ds
            self.calling_aes.add(event.assoc.requestor.ae_title)
            d = self.out_dir / str(ds.SeriesInstanceUID)
            d.mkdir(parents=True, exist_ok=True)
            ds.save_as(d / f"{uid}.dcm", enforce_file_format=True)
        return 0x0000

    def series(self, series_uid: str) -> list[Dataset]:
        return [d for d in self.received.values() if str(d.SeriesInstanceUID) == series_uid]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def scp(tmp_path):
    s = LocalTestSCP(tmp_path / "scp_store")
    yield s
    s.close()


def write_config(path: Path, port: int, host="127.0.0.1", **nifti) -> Path:
    import json
    path.write_text(json.dumps({
        "destination": {"host": host, "port": port, "called_ae": "TESTSCP", "calling_ae": "SLSCP_TEST",
                        "timeout_s": 10},
        "nifti": {"patient_id_prefix": "LOCALREVIEW", **nifti},
    }), encoding="utf-8")
    return path


def run(cfg: Path, input_dir: Path, out: Path, *extra) -> int:
    return slscp.main(["--config", str(cfg), "send", str(input_dir), "--output-dir", str(out), *extra])


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob("*") if p.is_file()}


# --------------------------------------------------------------------------- #
# synthetic NIfTI
# --------------------------------------------------------------------------- #
def rot(axis: str, deg: float) -> np.ndarray:
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return {"x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]),
            "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]),
            "z": np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])}[axis]


def affine_from(dirs: np.ndarray, spacing, origin) -> np.ndarray:
    a = np.eye(4)
    a[:3, :3] = dirs @ np.diag(spacing)
    a[:3, 3] = origin
    return a


def pattern(shape) -> np.ndarray:
    i, j, k = np.indices(shape)
    return (97 * i + 13 * j + 3 * k + (i * j) % 7).astype(np.float64)   # no symmetry: flips are detectable


def save_nifti(path: Path, data: np.ndarray, affine: np.ndarray, slope=None, inter=None) -> Path:
    img = nib.Nifti1Image(data, affine)
    img.set_qform(affine, code=1)
    img.set_sform(affine, code=1)
    if slope is not None:
        img.header.set_slope_inter(slope, inter)
    path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(img, str(path))
    return path


SHAPE = (23, 19, 7)
CASES = {
    # dcm2niix-like LAS axial CT, integer HU
    "axial_las_ct": dict(dirs=np.diag([-1.0, 1.0, 1.0]), spacing=(0.8, 0.9, 2.5), origin=(101.2, -120.4, -50.0),
                         dtype=np.int16, offset=-1024),
    # oblique MR (rotated about x and z), float values -> quantised
    "oblique_mr_float": dict(dirs=rot("z", 15) @ rot("x", 20), spacing=(1.1, 0.95, 3.0), origin=(-80, 40, 12.5),
                             dtype=np.float32, scale=0.37, offset=0.123),
    # sagittal stack: i -> posterior(RAS -y), j -> superior, k -> left(RAS -x); scl_slope/inter kept exact
    "sagittal_scaled": dict(dirs=np.array([[0, 0, -1.0], [-1.0, 0, 0], [0, 1.0, 0]]), spacing=(1.2, 1.0, 4.0),
                            origin=(60, 90, -30), dtype=np.int16, slope=2.5, inter=-100.0),
    # coronal stack with j pointing up (needs flip for display)
    "coronal": dict(dirs=np.array([[1.0, 0, 0], [0, 0, 1.0], [0, 1.0, 0]]), spacing=(0.7, 0.7, 5.0),
                    origin=(-90, -20, -60), dtype=np.uint16),
    # left-handed voxel grid (k runs inferior)
    "left_handed": dict(dirs=np.diag([1.0, 1.0, -1.0]), spacing=(0.6, 0.6, 1.5), origin=(-70, -70, 90),
                        dtype=np.int16),
}


def make_case(root: Path, name: str, rel: str | None = None, shape=SHAPE) -> Path:
    c = CASES[name]
    vals = pattern(shape) * c.get("scale", 1.0) + c.get("offset", 0.0)
    data = vals.astype(c["dtype"])
    return save_nifti(root / (rel or f"{name}/{name}.nii.gz"), data,
                      affine_from(c["dirs"], c["spacing"], c["origin"]), c.get("slope"), c.get("inter"))


# --------------------------------------------------------------------------- #
# independent geometry checks
# --------------------------------------------------------------------------- #
def dicom_grid(dss: list[Dataset]):
    """Rebuild the voxel->LPS mapping from IPP/IOP/PixelSpacing only."""
    iop = np.array(dss[0].ImageOrientationPatient, dtype=float)
    r, c = iop[:3], iop[3:]
    n = np.cross(r, c)
    dss = sorted(dss, key=lambda d: float(np.dot(np.array(d.ImagePositionPatient, float), n)))
    ipps = np.array([d.ImagePositionPatient for d in dss], dtype=float)
    row_sp, col_sp = map(float, dss[0].PixelSpacing)
    if len(dss) > 1:
        step = (ipps[-1] - ipps[0]) / (len(dss) - 1)
        assert np.allclose(ipps, ipps[0] + np.outer(np.arange(len(dss)), step), atol=1e-3), "slices not uniform"
    else:
        step = n * float(dss[0].SliceThickness)
    vol = np.stack([d.pixel_array.astype(np.float64) * float(d.RescaleSlope) + float(d.RescaleIntercept)
                    for d in dss])                                   # (slice, row, col)
    m = np.column_stack([r * col_sp, c * row_sp, step])              # (col, row, slice) -> LPS
    return vol, m, ipps[0]


def nifti_world(path: Path):
    img = nib.load(str(path))
    data = img.get_fdata()
    if data.ndim == 2:
        data = data[..., None]
    idx = np.indices(data.shape).reshape(3, -1).astype(float)
    ras = img.affine[:3, :3] @ idx + img.affine[:3, 3:4]
    return data.reshape(-1), ras * np.array([[-1.0], [-1.0], [1.0]])   # values, LPS points


def assert_same_volume(nifti_path: Path, values_lps, dss: list[Dataset], atol: float):
    """Every NIfTI voxel lands on exactly one DICOM voxel with the same value."""
    vals, lps = values_lps if values_lps else nifti_world(nifti_path)
    vol, m, ipp0 = dicom_grid(dss)
    cont = np.linalg.solve(m, lps - ipp0[:, None])
    idx = np.round(cont)
    assert np.abs(cont - idx).max() < 1e-3, "NIfTI voxel centres do not fall on the DICOM grid"
    col, row, sl = idx.astype(int)
    assert vol.size == vals.size
    assert col.min() >= 0 and row.min() >= 0 and sl.min() >= 0
    assert col.max() < vol.shape[2] and row.max() < vol.shape[1] and sl.max() < vol.shape[0]
    assert len(set(zip(col.tolist(), row.tolist(), sl.tolist()))) == vals.size, "mapping is not one-to-one"
    np.testing.assert_allclose(vol[sl, row, col], vals, atol=atol, rtol=0)


def assert_sitk_matches(nifti_path: Path, series_dir: Path, atol: float):
    sitk = pytest.importorskip("SimpleITK")
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(reader.GetGDCMSeriesFileNames(str(series_dir)))
    dimg = reader.Execute()
    nimg = sitk.ReadImage(str(nifti_path), sitk.sitkFloat64)
    narr = sitk.GetArrayFromImage(nimg)                               # (z, y, x)
    darr = sitk.GetArrayFromImage(dimg).astype(np.float64)
    zyx = np.indices(narr.shape).reshape(3, -1)
    xyz = zyx[::-1].astype(float)

    def to_phys(im, ijk):
        d = np.array(im.GetDirection()).reshape(3, 3)
        return d @ (np.array(im.GetSpacing())[:, None] * ijk) + np.array(im.GetOrigin())[:, None]

    phys = to_phys(nimg, xyz)
    d = np.array(dimg.GetDirection()).reshape(3, 3)
    cont = (np.linalg.solve(d, phys - np.array(dimg.GetOrigin())[:, None])) / np.array(dimg.GetSpacing())[:, None]
    idx = np.round(cont)
    assert np.abs(cont - idx).max() < 1e-3
    x, y, z = idx.astype(int)
    np.testing.assert_allclose(darr[z, y, x], narr.reshape(-1), atol=atol, rtol=0)


def assert_repo_matches(nifti_path: Path, dss: list[Dataset], atol: float):
    """nii2OncoNii's own DICOM->standard NIfTI path must reproduce the source NIfTI."""
    if not REPO:
        pytest.skip("NII2ONCO_REPO not set")
    sys.path.insert(0, REPO)
    from utils.affine_utils import adjust_affine_from_dicom
    iop = np.array(dss[0].ImageOrientationPatient, dtype=float)
    n = np.cross(iop[:3], iop[3:])
    dss = sorted(dss, key=lambda d: float(np.dot(np.array(d.ImagePositionPatient, float), n)))
    zyx = np.stack([d.pixel_array.astype(np.float64) * float(d.RescaleSlope) + float(d.RescaleIntercept)
                    for d in dss])
    img_xyz, _, aff_ras = adjust_affine_from_dicom(dss, zyx, None, to_onconii=False)
    vals, lps = nifti_world(nifti_path)
    ras = lps * np.array([[-1.0], [-1.0], [1.0]])
    cont = np.linalg.solve(aff_ras[:3, :3], ras - aff_ras[:3, 3:4])
    idx = np.round(cont)
    assert np.abs(cont - idx).max() < 1e-3
    i, j, k = idx.astype(int)
    np.testing.assert_allclose(img_xyz[i, j, k], vals, atol=atol, rtol=0)


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", list(CASES))
def test_nifti_geometry_and_values_via_cstore(tmp_path, scp, case):
    inp = tmp_path / "in"
    nii = make_case(inp, case)
    modality = "MR" if "mr" in case else "CT"
    before = snapshot(inp)
    cfg = write_config(tmp_path / "cfg.json", scp.port)
    assert run(cfg, inp, tmp_path / "runs", "--modality", modality) == 0
    assert snapshot(inp) == before, "source tree changed"

    assert len(scp.received) == SHAPE[2]
    series_uid = str(next(iter(scp.received.values())).SeriesInstanceUID)
    dss = scp.series(series_uid)
    ds0 = dss[0]
    assert ds0.Modality == modality
    assert ds0.SOPClassUID == (MRImageStorage if modality == "MR" else CTImageStorage)
    assert ds0.PatientID == f"LOCALREVIEW_{case}"
    assert ds0.PatientBirthDate == "" and ds0.PatientSex == "" and ds0.StudyDate == ""
    assert list(ds0.ImageType) == ["DERIVED", "SECONDARY"]
    assert scp.calling_aes == {"SLSCP_TEST"}
    # integer inputs are bit exact; float input is quantised to 16 bit
    c = CASES[case]
    atol = 1e-9 if c["dtype"] != np.float32 else (pattern(SHAPE).max() * c["scale"]) / 65535
    assert_same_volume(nii, None, dss, atol)
    assert_sitk_matches(nii, scp.out_dir / series_uid, atol + 1e-4)
    if REPO:
        assert_repo_matches(nii, dss, atol + 1e-4)
    # slice order follows the slice normal: instance numbers increase along it
    iop = np.array(ds0.ImageOrientationPatient, dtype=float)
    n = np.cross(iop[:3], iop[3:])
    pos = [float(np.dot(np.array(d.ImagePositionPatient, float), n))
           for d in sorted(dss, key=lambda d: int(d.InstanceNumber))]
    assert np.all(np.diff(pos) > 0)


def test_display_orientation_is_conventional(tmp_path, scp):
    inp = tmp_path / "in"
    make_case(inp, "axial_las_ct")
    make_case(inp, "coronal")
    make_case(inp, "sagittal_scaled")
    cfg = write_config(tmp_path / "cfg.json", scp.port)
    assert run(cfg, inp, tmp_path / "runs", "--modality", "CT") == 0
    iops = {str(d.SeriesDescription): [round(float(v), 6) for v in d.ImageOrientationPatient]
            for d in scp.received.values()}
    assert iops["axial_las_ct"] == [1, 0, 0, 0, 1, 0]
    assert iops["coronal"] == [1, 0, 0, 0, 0, -1]
    assert iops["sagittal_scaled"] == [0, 1, 0, 0, 0, -1]


def test_single_slice_2d(tmp_path, scp):
    inp = tmp_path / "in"
    a = affine_from(np.eye(3), (0.5, 0.5, 3.0), (-10, -10, 7))
    nii = save_nifti(inp / "p1" / "slice2d.nii.gz", pattern((16, 12, 1))[..., 0].astype(np.int16), a)
    cfg = write_config(tmp_path / "cfg.json", scp.port)
    assert run(cfg, inp, tmp_path / "runs", "--modality", "CT") == 0
    dss = list(scp.received.values())
    assert len(dss) == 1 and float(dss[0].SliceThickness) == 3.0
    assert_same_volume(nii, None, dss, 1e-9)


def test_grouping_uids_and_determinism(tmp_path, scp):
    inp = tmp_path / "in"
    make_case(inp, "axial_las_ct", "caseA/t1.nii.gz")
    make_case(inp, "axial_las_ct", "caseA/t2.nii.gz")
    make_case(inp, "coronal", "caseB/ct.nii.gz")
    cfg = write_config(tmp_path / "cfg.json", scp.port)
    assert run(cfg, inp, tmp_path / "runs", "--modality", "MR") == 0
    by_series = {}
    for d in scp.received.values():
        by_series.setdefault(str(d.SeriesInstanceUID), d)
    assert len(by_series) == 3
    pats = {(d.PatientID, str(d.StudyInstanceUID)) for d in by_series.values()}
    assert {p for p, _ in pats} == {"LOCALREVIEW_caseA", "LOCALREVIEW_caseB"}
    assert len({s for _, s in pats}) == 2                       # one study per patient group
    a_series = sorted((int(d.SeriesNumber), str(d.SeriesDescription)) for d in by_series.values()
                      if d.PatientID == "LOCALREVIEW_caseA")
    assert a_series == [(1, "t1"), (2, "t2")]
    assert len({str(d.FrameOfReferenceUID) for d in by_series.values()}) == 3   # never claims shared space
    first = set(scp.received)
    # same input again -> identical instance UIDs (no duplicate studies on re-send)
    scp.received.clear()
    assert run(cfg, inp, tmp_path / "runs", "--modality", "MR") == 0
    assert set(scp.received) == first
    # --group-by file -> one patient per file
    scp.received.clear()
    assert run(cfg, inp, tmp_path / "runs", "--modality", "MR", "--group-by", "file") == 0
    assert {d.PatientID for d in scp.received.values()} == {
        "LOCALREVIEW_caseA_t1", "LOCALREVIEW_caseA_t2", "LOCALREVIEW_caseB_ct"}


def test_masks_ambiguous_and_unsupported_are_skipped(tmp_path, scp, capsys):
    inp = tmp_path / "in"
    a = affine_from(np.eye(3), (1, 1, 1), (0, 0, 0))
    labels = np.zeros(SHAPE, np.uint8)
    labels[5:10, 5:10, 2:4] = 1
    labels[12:15, 3:6, 1:3] = 2
    save_nifti(inp / "c1" / "c1_seg.nii.gz", labels, a)                       # mask by name + values
    save_nifti(inp / "labelsTr" / "c2.nii.gz", labels, a)                     # mask by directory name
    save_nifti(inp / "c1" / "c1_extra.nii.gz", labels.astype(np.float32), a)  # label-like values, neutral name
    save_nifti(inp / "c1" / "t1_brain_masked.nii.gz", pattern(SHAPE).astype(np.float32) * 0.5, a)  # image named mask
    save_nifti(inp / "c1" / "t1.nii.gz", pattern(SHAPE).astype(np.int16), a)
    save_nifti(inp / "c3" / "dwi4d.nii.gz", np.stack([pattern(SHAPE)] * 3, -1).astype(np.int16), a)
    shear = a.copy()
    shear[0, 1] = 0.3
    save_nifti(inp / "c4" / "sheared.nii.gz", pattern(SHAPE).astype(np.int16), shear)
    noxf = nib.Nifti1Image(pattern(SHAPE).astype(np.int16), a)
    noxf.set_qform(None, code=0)
    noxf.set_sform(None, code=0)
    (inp / "c5").mkdir()
    nib.save(noxf, str(inp / "c5" / "noxform.nii.gz"))

    cfg = write_config(tmp_path / "cfg.json", scp.port)
    assert run(cfg, inp, tmp_path / "runs", "--modality", "MR") == 0
    assert {str(d.SeriesDescription) for d in scp.received.values()} == {"t1"}
    out = capsys.readouterr().out
    assert "[mask] c1/c1_seg.nii.gz" in out
    assert "[mask] labelsTr/c2.nii.gz" in out
    assert "[ambiguous] c1/c1_extra.nii.gz" in out
    assert "[ambiguous] c1/t1_brain_masked.nii.gz" in out
    assert "[unsupported] c3/dwi4d.nii.gz" in out and "4D" in out
    assert "[unsupported] c4/sheared.nii.gz" in out and "shear" in out
    assert "[unsupported] c5/noxform.nii.gz" in out

    scp.received.clear()
    assert run(cfg, inp, tmp_path / "runs", "--modality", "MR", "--force-image", "t1_brain_masked*") == 0
    assert {str(d.SeriesDescription) for d in scp.received.values()} == {"t1", "t1_brain_masked"}


def test_dir_group_with_too_many_images_is_flagged(tmp_path, scp, capsys):
    inp = tmp_path / "in"
    for n in range(4):
        make_case(inp, "left_handed", f"imagesTr/case_{n:03d}.nii.gz")
    cfg = write_config(tmp_path / "cfg.json", scp.port, max_series_per_study=3)
    assert run(cfg, inp, tmp_path / "runs", "--modality", "CT") == 0
    assert not scp.received
    assert "--group-by file" in capsys.readouterr().out
    assert run(cfg, inp, tmp_path / "runs", "--modality", "CT", "--group-by", "file") == 0
    assert len({d.PatientID for d in scp.received.values()}) == 4


def _onco_from_standard(std_path: Path, out_path: Path):
    """Build an Onco NIfTI the way nii2OncoNii does (repo code if available)."""
    img = nib.load(str(std_path))
    data = np.asarray(img.dataobj)
    if REPO:
        sys.path.insert(0, REPO)
        from utils.affine_utils import adjust_affine_from_nifti
        zyx, _, aff = adjust_affine_from_nifti(data, img.affine, None, to_onconii=True)
    else:  # documented convention for an axis aligned input: LPS, data (Z, Y, X), diagonal affine
        lps = np.diag([-1.0, -1.0, 1.0, 1.0]) @ img.affine
        xyz = data
        for ax in range(3):
            if lps[ax, ax] < 0:
                xyz, lps = n2d._flip(xyz, lps, ax)
        zyx, aff = np.transpose(xyz, (2, 1, 0)), lps
    out_path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(nib.Nifti1Image(np.ascontiguousarray(zyx), aff), str(out_path))   # same call as save_onco_nifti


def test_onco_nifti_layout(tmp_path, scp, capsys):
    std = make_case(tmp_path / "std", "axial_las_ct")
    inp = tmp_path / "onco"
    _onco_from_standard(std, inp / "data" / "case01_image.nii.gz")
    cfg = write_config(tmp_path / "cfg.json", scp.port)
    # auto: refuses to guess
    assert run(cfg, inp, tmp_path / "runs", "--modality", "CT") == 0
    assert not scp.received
    assert "--nifti-layout onco" in capsys.readouterr().out
    # explicit onco layout: same world geometry and values as the original standard NIfTI
    assert run(cfg, inp, tmp_path / "runs", "--modality", "CT", "--nifti-layout", "onco") == 0
    dss = list(scp.received.values())
    assert len(dss) == SHAPE[2]
    assert_same_volume(std, None, dss, 1e-9)


def test_dicom_passthrough_restricted_scp_and_failures(tmp_path):
    inp = tmp_path / "dicom_in"
    inp.mkdir()
    study = generate_uid()

    def mk(name, sop_class, modality, series, n, compress=False, raw=False):
        uids = []
        for k in range(n):
            ds = Dataset()
            ds.file_meta = FileMetaDataset()
            ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            ds.SOPClassUID = sop_class
            ds.SOPInstanceUID = generate_uid()
            ds.file_meta.MediaStorageSOPClassUID = sop_class
            ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
            ds.PatientID, ds.PatientName = "TEST001", "TEST^PATIENT"
            ds.StudyInstanceUID, ds.SeriesInstanceUID = study, series
            ds.Modality, ds.SeriesNumber, ds.SeriesDescription, ds.InstanceNumber = modality, 1, name, k + 1
            ds.Rows = ds.Columns = 8
            ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
            ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 16, 15, 1
            ds.PixelData = (np.arange(64, dtype=np.int16) * (k + 1)).tobytes()
            if compress:
                ds.compress(RLELossless)
            if raw:   # no preamble / file meta, implicit VR (older devices)
                del ds.file_meta
                ds.save_as(inp / f"{name}_{k}", implicit_vr=True, little_endian=True)
            else:
                ds.save_as(inp / f"{name}_{k}" if k == 0 else inp / f"{name}_{k}.dcm", enforce_file_format=True)
            uids.append(str(ds.SOPInstanceUID))
        return uids

    ct = mk("ct", CTImageStorage, "CT", generate_uid(), 3)
    rle = mk("ctrle", CTImageStorage, "CT", generate_uid(), 2, compress=True)
    mr = mk("mr", MRImageStorage, "MR", generate_uid(), 2)
    rawct = mk("ctraw", CTImageStorage, "CT", generate_uid(), 1, raw=True)
    (inp / "notes.txt").write_text("not dicom")
    (inp / "garbage.bin").write_bytes(b"\x00" * 300)
    before = snapshot(inp)

    scp = LocalTestSCP(tmp_path / "store", restricted=True, fail_sop_uids={ct[1]})
    try:
        cfg = write_config(tmp_path / "cfg.json", scp.port)
        rc = run(cfg, inp, tmp_path / "runs")
    finally:
        scp.close()
    assert rc == 1                                            # some instances failed
    assert snapshot(inp) == before                            # sources untouched, nothing written
    assert set(scp.received) == {ct[0], ct[2], *rle, *rawct}        # RLE decompressed for an uncompressed-only SCP
    for uid in rle:                                           # same instance UID and pixels as the source
        src = pydicom.dcmread(next(p for p in inp.iterdir() if p.name.startswith("ctrle")
                                   and pydicom.dcmread(p).SOPInstanceUID == uid))
        np.testing.assert_array_equal(scp.received[uid].pixel_array, src.pixel_array)
        assert scp.received[uid].file_meta.TransferSyntaxUID in (ExplicitVRLittleEndian, ImplicitVRLittleEndian)
    import json
    summary = json.loads(next((tmp_path / "runs").glob("*/summary.json")).read_text(encoding="utf-8"))
    rows = {r["series_description"]: r for r in summary["series"]}
    assert (rows["ct"]["sent_ok"], rows["ct"]["failed"]) == (2, 1)
    assert "0xA700" in rows["ct"]["failures"][0]["detail"]
    assert (rows["ctrle"]["sent_ok"], rows["ctrle"]["failed"]) == (2, 0)
    assert (rows["mr"]["sent_ok"], rows["mr"]["failed"]) == (0, 2)
    assert (rows["ctraw"]["sent_ok"], rows["ctraw"]["failed"]) == (1, 0)
    assert rows["mr"]["patient_id"] == "TEST001"
    assert summary["ignored"] == {".txt": 1, ".bin": 1}


def test_refuses_non_local_host_and_missing_settings(tmp_path, capsys):
    inp = tmp_path / "in"
    make_case(inp, "axial_las_ct")
    cfg = write_config(tmp_path / "cfg.json", 4002, host="192.0.2.1")
    assert run(cfg, inp, tmp_path / "runs", "--modality", "CT") == 2
    assert "로컬 SCP 전용" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()
    cfg2 = write_config(tmp_path / "cfg2.json", 1)
    # no destination config at all
    assert slscp.main(["--config", str(tmp_path / "none.json"), "send", str(inp), "--modality", "CT"]) == 2
    assert "destination.host" in capsys.readouterr().err
    # output inside the input tree is refused
    assert run(cfg2, inp, inp / "runs", "--modality", "CT") == 2


def test_cli_subprocess_echo_send_inspect(tmp_path, scp):
    inp = tmp_path / "한글 경로" / "in"
    make_case(inp, "oblique_mr_float")
    cfg = write_config(tmp_path / "cfg.json", scp.port)
    script = str(SCRIPTS / "send_local_scp.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, script, "--config", str(cfg), "echo"], capture_output=True, env=env)
    assert r.returncode == 0 and "C-ECHO 성공" in r.stdout.decode("utf-8")
    r = subprocess.run([sys.executable, script, "--config", str(cfg), "send", str(inp), "--modality", "MR",
                        "--output-dir", str(tmp_path / "runs")], capture_output=True, env=env)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")
    assert "성공 7 / 실패 0 / 전체 7" in out and "LOCALREVIEW_oblique_mr_float" in out
    assert "AXIAL (oblique 20°)" in out and "값 양자화" in out
    run_dir = next((tmp_path / "runs").iterdir())
    r = subprocess.run([sys.executable, script, "inspect", str(run_dir / "nifti_dicom")], capture_output=True,
                       env=env)
    assert r.returncode == 0 and "ImagePositionPatient" in r.stdout.decode("utf-8")


def test_dry_run_needs_no_destination(tmp_path):
    inp = tmp_path / "in"
    make_case(inp, "coronal")
    rc = slscp.main(["--config", str(tmp_path / "none.json"), "send", str(inp), "--modality", "CT", "--dry-run",
                     "--output-dir", str(tmp_path / "runs")])
    assert rc == 0
    files = list((tmp_path / "runs").glob("*/nifti_dicom/*/*/*.dcm"))
    assert len(files) == SHAPE[2]
    ds = pydicom.dcmread(files[0])
    assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian


def test_modality_auto_inference(tmp_path, scp, capsys):
    import json
    inp = tmp_path / "in"
    a = affine_from(np.eye(3), (1, 1, 2), (0, 0, 0))
    mr_vals = pattern(SHAPE).astype(np.int16)              # positive values, MR-like
    hu_vals = (pattern(SHAPE) - 1024).astype(np.int16)      # air around -1024, CT-like
    save_nifti(inp / "p1" / "BraTS-001-t1n.nii.gz", mr_vals, a)          # MR by path word
    save_nifti(inp / "p1" / "scan_a.nii.gz", mr_vals, a)                 # MR by sidecar JSON
    (inp / "p1" / "scan_a.json").write_text(json.dumps({"Modality": "MR"}))
    save_nifti(inp / "p2" / "abdomen.nii.gz", hu_vals, a)                # CT by HU values
    save_nifti(inp / "p2" / "liver_ct.nii.gz", mr_vals, a)               # CT by path word (note: no air)
    save_nifti(inp / "p3" / "t1_hu.nii.gz", hu_vals, a)                  # conflict: MR word, HU values
    save_nifti(inp / "p3" / "volume.nii.gz", mr_vals, a)                 # no evidence
    save_nifti(inp / "p4" / "pet_scan.nii.gz", mr_vals, a)               # other modality word
    save_nifti(inp / "p4" / "x.nii.gz", mr_vals, a)                      # sidecar says PT
    (inp / "p4" / "x.json").write_text(json.dumps({"Modality": "PT"}))

    cfg = write_config(tmp_path / "cfg.json", scp.port)
    assert run(cfg, inp, tmp_path / "runs") == 0
    got = {str(d.SeriesDescription): (d.Modality, str(d.SOPClassUID), str(d.ImageComments))
           for d in scp.received.values()}
    assert {k: v[0] for k, v in got.items()} == {
        "BraTS-001-t1n": "MR", "scan_a": "MR", "abdomen": "CT", "liver_ct": "CT"}
    assert got["abdomen"][1] == CTImageStorage and got["scan_a"][1] == MRImageStorage
    assert "사이드카 JSON" in got["scan_a"][2] and "t1n" in got["BraTS-001-t1n"][2]
    out = capsys.readouterr().out
    for rel in ("p3/t1_hu.nii.gz", "p3/volume.nii.gz", "p4/pet_scan.nii.gz", "p4/x.nii.gz"):
        assert f"[ambiguous] {rel}: modality 판단 불가" in out
    assert "Modality=PT" in out and "HU 보정 확인" in out
    # explicit --modality overrides inference for every image
    scp.received.clear()
    assert run(cfg, inp, tmp_path / "runs", "--modality", "MR") == 0
    assert len({str(d.SeriesDescription) for d in scp.received.values()}) == 8
    assert {d.Modality for d in scp.received.values()} == {"MR"}
