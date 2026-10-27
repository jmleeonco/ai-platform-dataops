"""Read-only inventory of an input directory: what each file is, and DICOM header metadata.

Nothing under the input root is modified. Directories named backup/backups and hidden
directories are not entered; symlinked directories are not followed.
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pydicom

from .legacy import n2d
from .util import now_iso

SKIP_DIR_NAMES = {"backup", "backups"}
SIDECAR_EXT = {".json"}
NON_DICOM_EXT = {".txt", ".md", ".csv", ".tsv", ".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg", ".gif",
                 ".zip", ".gz", ".tar", ".7z", ".py", ".sh", ".sql", ".html", ".htm", ".xml", ".yaml", ".yml",
                 ".mha", ".mhd", ".raw", ".nrrd", ".npy", ".npz", ".h5", ".pkl"}
DICOMDIR_SOP = "1.2.840.10008.1.3.10"

# Series-level metadata kept in the DB. PatientName and birth date are never collected.
SERIES_TAGS = ["Modality", "SeriesDescription", "SeriesNumber", "SeriesDate", "BodyPartExamined",
               "Manufacturer", "ManufacturerModelName", "SliceThickness", "SpacingBetweenSlices", "PixelSpacing",
               "Rows", "Columns", "KVP", "ConvolutionKernel", "ContrastBolusAgent", "MagneticFieldStrength",
               "SequenceName", "ScanningSequence", "EchoTime", "RepetitionTime", "ImageOrientationPatient",
               "PhotometricInterpretation", "BitsStored", "PatientSex", "PatientAge", "InstitutionName"]
STUDY_TAGS = ["StudyDate", "StudyDescription"]


def _jsonable(value):
    if value is None:
        return None
    if isinstance(value, (pydicom.multival.MultiValue, list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, pydicom.valuerep.DSfloat):
        return float(value)
    if isinstance(value, pydicom.valuerep.IS):
        return int(value)
    return str(value)


def header_metadata(ds, tags) -> dict:
    out = {}
    for tag in tags:
        if tag in ds:
            v = _jsonable(ds.get(tag))
            if v not in ("", None, []):
                out[tag] = v
    return out


def read_dicom_header(path: Path):
    try:
        with open(path, "rb") as f:
            head = f.read(132)
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=head[128:132] != b"DICM")
    except Exception:  # noqa: BLE001 - anything unreadable is simply not DICOM
        return None
    if "SOPClassUID" not in ds or "SOPInstanceUID" not in ds:
        return None
    return ds


def _dicom_record(ds) -> dict:
    ts = ""
    if getattr(ds, "file_meta", None) is not None and "TransferSyntaxUID" in ds.file_meta:
        ts = str(ds.file_meta.TransferSyntaxUID)
    ipp = ds.get("ImagePositionPatient")
    return {
        "patient_id": str(ds.get("PatientID", "")),
        "study_uid": str(ds.get("StudyInstanceUID", "")),
        "series_uid": str(ds.get("SeriesInstanceUID", "")),
        "sop_uid": str(ds.SOPInstanceUID),
        "sop_class_uid": str(ds.SOPClassUID),
        "transfer_syntax": ts,
        "instance_number": _jsonable(ds.get("InstanceNumber")),
        "ipp": _jsonable(ipp) if ipp is not None else None,
        "study": header_metadata(ds, STUDY_TAGS),
        "series": header_metadata(ds, SERIES_TAGS),
    }


def walk(root: Path):
    """Yield (path, skipped_dir_rel) pairs; skipped directories are reported, not entered."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            rel = (here / d).relative_to(root).as_posix()
            if d.lower() in SKIP_DIR_NAMES or d.startswith(".") or (here / d).is_symlink():
                yield None, rel
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            yield here / name, None


def inventory(root: Path) -> dict:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"input directory not found: {root}")
    files, skipped_dirs = [], []
    counts = Counter()
    for path, skipped in walk(root):
        if skipped is not None:
            skipped_dirs.append(skipped)
            continue
        rel = path.relative_to(root).as_posix()
        rec = {"rel": rel, "size": None, "kind": "other"}
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(root):
                rec.update(kind="skipped", reason="symlink outside input root")
                files.append(rec)
                counts["skipped"] += 1
                continue
        try:
            rec["size"] = path.stat().st_size
        except OSError as e:
            rec.update(kind="unreadable", reason=str(e))
        if rec["kind"] == "other":
            ext = path.suffix.lower()
            if n2d.is_nifti(path):
                rec["kind"] = "nifti"
            elif ext in SIDECAR_EXT:
                rec["kind"] = "sidecar"
            elif ext not in NON_DICOM_EXT:
                ds = read_dicom_header(path)
                if ds is not None:
                    if str(ds.SOPClassUID) == DICOMDIR_SOP:
                        rec.update(kind="dicomdir", reason="DICOMDIR index (not an image)")
                    else:
                        rec["kind"] = "dicom"
                        rec["dicom"] = _dicom_record(ds)
        counts[rec["kind"]] += 1
        files.append(rec)
    return {"schema_version": 1, "input_root": str(root), "created_at": now_iso(),
            "counts": dict(counts), "skipped_dirs": skipped_dirs, "files": files}
