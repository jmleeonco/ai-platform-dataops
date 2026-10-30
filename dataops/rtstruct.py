"""Label map (NIfTI) -> DICOM RTSTRUCT referencing the converted image series.

The label volume is read with the image converter's geometry rules and canonicalised the same
way, so voxel (i, j, k) of the label is voxel (i, j, k) of the written image slices. Contours
are traced per slice and per label value at the 0.5 iso-line (marching squares), slightly
simplified, and written in patient (LPS) millimetres. Every contour references the image slice it
lies on. A round trip (contours rasterised back) gives a per-ROI Dice that is stored in the report;
nothing is resampled. Same inputs give byte-identical output (no timestamps, derived UIDs).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, PYDICOM_IMPLEMENTATION_UID
from skimage.draw import polygon as fill_polygon
from skimage.measure import approximate_polygon, find_contours

from .legacy import n2d

RTSTRUCT_VERSION = "dataops rtstruct 1"
RTSTRUCT_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.481.3"
STUDY_REF_SOP_CLASS = "1.2.840.10008.3.1.2.3.1"      # conventional value in RTReferencedStudySequence
SIMPLIFY_PX = 0.3                                     # max deviation of the simplified polygon, in pixels
GEOMETRY_TOL_MM = 1e-3
MIN_DICE = 0.9
PALETTE = [(230, 25, 75), (60, 180, 75), (255, 225, 25), (0, 130, 200), (245, 130, 48), (145, 30, 180),
           (70, 240, 240), (240, 50, 230), (210, 245, 60), (250, 190, 212), (0, 128, 128), (220, 190, 255),
           (170, 110, 40), (255, 250, 200), (128, 0, 0), (170, 255, 195), (128, 128, 0), (255, 215, 180),
           (0, 0, 128), (128, 128, 128)]


class RtstructError(Exception):
    pass


def rtstruct_ids(image_series_uid: str, label_identity: str) -> tuple[str, str]:
    series_uid = n2d._uid("rtstruct-series", image_series_uid, label_identity, RTSTRUCT_VERSION)
    return series_uid, n2d._uid("rtstruct-sop", series_uid)


def canonical_label(path: Path, layout: str):
    vol, reason = n2d.load_volume(path, layout)
    if vol is None:
        raise RtstructError(f"라벨 NIfTI를 읽을 수 없음: {reason}")
    values = vol.data.astype(np.float64) * vol.slope + vol.inter if (vol.slope, vol.inter) != (1.0, 0.0) \
        else vol.data
    if not np.all(np.isfinite(values)) or np.any(values < 0) or np.any(values != np.round(values)):
        raise RtstructError("라벨 값이 0 이상의 정수가 아닙니다 (label map이 아님)")
    data, affine, _ = n2d.canonicalize(np.asarray(values).astype(np.int32), vol.affine_lps)
    return data, affine


def _contours(mask2d: np.ndarray) -> list[np.ndarray]:
    """Closed (i, j) polygons around True pixels, holes included as separate contours."""
    padded = np.pad(mask2d.astype(np.uint8), 1)
    out = []
    for c in find_contours(padded, 0.5):
        c = approximate_polygon(c - 1.0, tolerance=SIMPLIFY_PX)
        if len(c) > 1 and np.allclose(c[0], c[-1]):
            c = c[:-1]
        if len(c) >= 3:
            out.append(c)
    return out


def _rasterize(contours: list[np.ndarray], shape) -> np.ndarray:
    """Even-odd fill (holes subtract), matching how CLOSED_PLANAR contours are read by most viewers."""
    acc = np.zeros(shape, dtype=np.uint8)
    for c in contours:
        rr, cc = fill_polygon(c[:, 0], c[:, 1], shape)
        m = np.zeros(shape, dtype=np.uint8)
        m[rr, cc] = 1
        acc ^= m
    return acc.astype(bool)


def _on_grid(path: Path, layout: str, image: dict):
    data, affine = canonical_label(path, layout)
    if tuple(data.shape) != tuple(image["shape"]):
        raise RtstructError(f"라벨 크기 {tuple(data.shape)} 가 영상 {tuple(image['shape'])} 과 다릅니다")
    if not np.allclose(affine, np.asarray(image["affine"]), atol=GEOMETRY_TOL_MM):
        raise RtstructError("라벨과 영상의 위치/방향(affine)이 다릅니다 — 같은 격자가 아니면 변환하지 않습니다")
    return data, affine


def build_rtstruct(label_path: Path | list[Path], layout: str, image: dict, label_names: dict[int, str],
                   series_uid: str, sop_uid: str, series_number: int, description: str, out_path: Path) -> dict:
    """label_path: one label map (one ROI per non-zero value), or a list of binary masks (one ROI per file, its
    non-zero voxels; ROI i is label_names[i]). image: patient_id, patient_name, study_uid, study_description,
    series_uid, frame_of_reference_uid, sop_class_uid, sop_uids (one per canonical slice k), affine (4x4
    canonical LPS), shape (ni, nj, nk)."""
    if isinstance(label_path, list):
        masks = []
        for i, p in enumerate(label_path, start=1):
            data, affine = _on_grid(p, layout, image)
            masks.append((i, data != 0))
        present = [i for i, m in masks if m.any()]
        values = present
        mask_of = dict(masks)
        if not present:
            raise RtstructError("모든 마스크가 비어 있습니다")
    else:
        data, affine = _on_grid(label_path, layout, image)
        values = sorted(int(v) for v in np.unique(data) if v != 0)
        mask_of = None
    rois, report_rois = [], []
    for number, value in enumerate(values, start=1):
        name = label_names.get(value) or f"label_{value}"
        vol = mask_of[value] if mask_of is not None else data == value
        contours, inter, total_mask, total_raster = [], 0, 0, 0
        for k in np.nonzero(np.any(vol, axis=(0, 1)))[0]:
            m = vol[:, :, k]
            cs = _contours(m)
            r = _rasterize(cs, m.shape)
            inter += int(np.count_nonzero(m & r))
            total_mask += int(np.count_nonzero(m))
            total_raster += int(np.count_nonzero(r))
            for c in cs:
                ijk = np.column_stack([c[:, 0], c[:, 1], np.full(len(c), float(k)), np.ones(len(c))])
                xyz = (affine @ ijk.T).T[:, :3]
                contours.append((int(k), xyz))
        dice = 2.0 * inter / (total_mask + total_raster) if total_mask + total_raster else 0.0
        rois.append((number, name, value, contours))
        report_rois.append({"number": number, "value": value, "name": name, "voxels": total_mask,
                            "contours": len(contours), "dice_roundtrip": round(dice, 4),
                            "named": value in label_names})
    if not rois:
        raise RtstructError("라벨 볼륨에 0이 아닌 값이 없습니다")
    missing = sorted(set(label_names) - set(values) - {0})          # a value absent, or an empty mask

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = RTSTRUCT_SOP_CLASS
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.SOPClassUID = RTSTRUCT_SOP_CLASS
    ds.SOPInstanceUID = sop_uid
    ds.PatientName = image["patient_name"]
    ds.PatientID = image["patient_id"]
    ds.PatientBirthDate = ""
    ds.PatientSex = ""
    ds.StudyInstanceUID = image["study_uid"]
    ds.StudyDate = ""
    ds.StudyTime = ""
    ds.ReferringPhysicianName = ""
    ds.StudyID = ""
    ds.AccessionNumber = ""
    ds.StudyDescription = image["study_description"]
    ds.Modality = "RTSTRUCT"
    ds.SeriesInstanceUID = series_uid
    ds.SeriesNumber = series_number
    ds.SeriesDescription = n2d._clip(description)
    ds.Manufacturer = ""
    ds.SoftwareVersions = RTSTRUCT_VERSION
    ds.InstanceNumber = 1
    ds.StructureSetLabel = "LABELS"
    ds.StructureSetName = n2d._clip(description)
    ds.StructureSetDate = ""
    ds.StructureSetTime = ""

    contour_images = Sequence()
    for sop in image["sop_uids"]:
        it = Dataset()
        it.ReferencedSOPClassUID = image["sop_class_uid"]
        it.ReferencedSOPInstanceUID = sop
        contour_images.append(it)
    rt_series = Dataset()
    rt_series.SeriesInstanceUID = image["series_uid"]
    rt_series.ContourImageSequence = contour_images
    rt_study = Dataset()
    rt_study.ReferencedSOPClassUID = STUDY_REF_SOP_CLASS
    rt_study.ReferencedSOPInstanceUID = image["study_uid"]
    rt_study.RTReferencedSeriesSequence = Sequence([rt_series])
    for_ref = Dataset()
    for_ref.FrameOfReferenceUID = image["frame_of_reference_uid"]
    for_ref.RTReferencedStudySequence = Sequence([rt_study])
    ds.ReferencedFrameOfReferenceSequence = Sequence([for_ref])

    ds.StructureSetROISequence = Sequence()
    ds.ROIContourSequence = Sequence()
    ds.RTROIObservationsSequence = Sequence()
    for number, name, value, contours in rois:
        roi = Dataset()
        roi.ROINumber = number
        roi.ReferencedFrameOfReferenceUID = image["frame_of_reference_uid"]
        roi.ROIName = n2d._clip(name)
        roi.ROIGenerationAlgorithm = ""
        ds.StructureSetROISequence.append(roi)
        rc = Dataset()
        rc.ROIDisplayColor = list(PALETTE[(value - 1) % len(PALETTE)])
        rc.ReferencedROINumber = number
        rc.ContourSequence = Sequence()
        for k, xyz in contours:
            c = Dataset()
            ref = Dataset()
            ref.ReferencedSOPClassUID = image["sop_class_uid"]
            ref.ReferencedSOPInstanceUID = image["sop_uids"][k]
            c.ContourImageSequence = Sequence([ref])
            c.ContourGeometricType = "CLOSED_PLANAR"
            c.NumberOfContourPoints = len(xyz)
            c.ContourData = [f"{v:.3f}" for v in xyz.reshape(-1)]
            rc.ContourSequence.append(c)
        ds.ROIContourSequence.append(rc)
        obs = Dataset()
        obs.ObservationNumber = number
        obs.ReferencedROINumber = number
        obs.RTROIInterpretedType = "ORGAN"
        obs.ROIInterpreter = ""
        ds.RTROIObservationsSequence.append(obs)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(out_path, enforce_file_format=True)
    worst = min(r["dice_roundtrip"] for r in report_rois)
    return {"converter": RTSTRUCT_VERSION, "rois": report_rois, "absent_labels": missing,
            "unnamed_values": [r["value"] for r in report_rois if not r["named"]],
            "min_dice_roundtrip": worst, "dice_ok": worst >= MIN_DICE,
            "contours": sum(r["contours"] for r in report_rois)}


def read_overlay(path: Path) -> dict:
    """{referenced image SOP UID: [(rgb, [(x, y, z), ...]), ...]} for the viewer."""
    import pydicom
    ds = pydicom.dcmread(path)
    colors = {}
    for rc in ds.get("ROIContourSequence", []):
        colors[int(rc.ReferencedROINumber)] = tuple(int(v) for v in rc.get("ROIDisplayColor", [255, 255, 0]))
    out: dict[str, list] = {}
    for rc in ds.get("ROIContourSequence", []):
        color = colors.get(int(rc.ReferencedROINumber), (255, 255, 0))
        for c in rc.get("ContourSequence", []):
            refs = c.get("ContourImageSequence", [])
            if not refs:
                continue
            pts = np.asarray([float(v) for v in c.ContourData]).reshape(-1, 3)
            out.setdefault(str(refs[0].ReferencedSOPInstanceUID), []).append((color, pts))
    names = {int(r.ROINumber): str(r.ROIName) for r in ds.get("StructureSetROISequence", [])}
    legend = [{"name": names.get(n, str(n)), "color": colors[n]} for n in sorted(colors)]
    return {"contours": out, "legend": legend}
