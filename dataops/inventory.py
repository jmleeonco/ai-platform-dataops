"""Read-only inventory of an input directory: what each file is, and DICOM header metadata.

Nothing under the input root is modified. Directories named backup/backups and hidden
directories are not entered; symlinked directories are not followed.
"""
from __future__ import annotations

import os
import stat
from collections import Counter
from pathlib import Path

import pydicom

from .legacy import n2d
from .util import now_iso, pmap

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


def _lstat(path: Path):
    try:
        return path.lstat()
    except OSError:
        return None


def walk(root: Path):
    """Yield (path, skipped_dir_rel, stat) triples in sorted top-down order; skipped directories are reported,
    not entered. Each directory is listed once and its entries are stat'ed in parallel (NFS: one round trip
    each). stat is None for a symlink or an entry that vanished; the caller stats those itself."""
    def visit(here: Path):
        try:
            names = sorted(os.listdir(here))
        except OSError:
            return
        paths = [here / n for n in names]
        stats = pmap(_lstat, paths)
        subdirs, files = [], []
        for p, st in zip(paths, stats):
            if st is None:
                files.append((p, None))
            elif stat.S_ISLNK(st.st_mode):
                if p.is_dir():                        # a symlinked directory is reported, not followed
                    yield None, p.relative_to(root).as_posix(), None
                else:
                    files.append((p, None))
            elif stat.S_ISDIR(st.st_mode):
                if p.name.lower() in SKIP_DIR_NAMES or p.name.startswith("."):
                    yield None, p.relative_to(root).as_posix(), None
                else:
                    subdirs.append(p)
            else:
                files.append((p, st))
        for p, st in files:
            yield p, None, st
        for d in subdirs:
            yield from visit(d)
    yield from visit(root)


def inventory(root: Path) -> dict:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"input directory not found: {root}")
    files, skipped_dirs, probe = [], [], []
    counts = Counter()
    for path, skipped, st in walk(root):
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
                continue
        try:
            rec["size"] = st.st_size if st is not None else path.stat().st_size
        except OSError as e:
            rec.update(kind="unreadable", reason=str(e))
        if rec["kind"] == "other":
            ext = path.suffix.lower()
            if n2d.is_nifti(path):
                rec["kind"] = "nifti"
            elif ext in SIDECAR_EXT:
                rec["kind"] = "sidecar"
            elif ext not in NON_DICOM_EXT:
                probe.append((rec, path))
        files.append(rec)
    for (rec, _), ds in zip(probe, pmap(read_dicom_header, [p for _, p in probe])):
        if ds is not None:
            if str(ds.SOPClassUID) == DICOMDIR_SOP:
                rec.update(kind="dicomdir", reason="DICOMDIR index (not an image)")
            else:
                rec["kind"] = "dicom"
                rec["dicom"] = _dicom_record(ds)
    counts.update(rec["kind"] for rec in files)
    return {"schema_version": 1, "input_root": str(root), "created_at": now_iso(),
            "counts": dict(counts), "skipped_dirs": skipped_dirs, "files": files}
