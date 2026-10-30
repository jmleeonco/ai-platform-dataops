"""Fast read-only overview of an input folder for the analysis agent and the report.

Walks the tree once (same skip rules as the inventory), counts files per directory and kind,
keeps a few example names, reads NIfTI headers (not voxels) and a few DICOM headers per directory,
and lists small text documents (README, dataset.json, CSV, licence). Nothing is hashed here.
"""
from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path

import nibabel as nib
import numpy as np

from .inventory import SERIES_TAGS, header_metadata, read_dicom_header, walk
from .legacy import n2d
from .util import now_iso

DOC_NAMES = ("readme", "license", "licence", "dataset", "description", "citation", "info", "notes")
DOC_EXT = {".md", ".txt", ".json", ".csv", ".tsv", ".rst", ".yaml", ".yml", ".xlsx", ".pdf", ".html"}
ARCHIVE_EXT = {".zip", ".tar", ".tgz", ".7z", ".rar"}
MAX_DIRS = 400
EXAMPLES = 4
SAMPLES_PER_DIR = 2
DICOM_PROBES_PER_DIR = 3
STAT_PER_DIR = 200        # walk stats every entry; this caps only the extra stats of symlinked files
TIME_BUDGET_S = 120       # the overview stops here; the agent is told it is partial


def file_kind(path: Path) -> str:
    name = path.name.lower()
    if n2d.is_nifti(path):
        return "nifti"
    ext = path.suffix.lower()
    if name.endswith((".tar.gz", ".tgz")) or ext in ARCHIVE_EXT:
        return "archive"
    if ext == ".dcm" or ext == "":
        return "dicom?"
    if ext in DOC_EXT:
        return "doc"
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}:
        return "picture"
    if ext in {".mha", ".mhd", ".nrrd", ".raw", ".npy", ".npz", ".h5", ".hdf5", ".pkl"}:
        return "other-volume"
    return "other"


def nifti_header(path: Path) -> dict:
    try:
        img = nib.load(str(path))
        hdr = img.header
        out = {"shape": [int(x) for x in img.shape], "zooms": [round(float(z), 4) for z in hdr.get_zooms()],
               "dtype": str(hdr.get_data_dtype()), "sform_code": int(hdr["sform_code"]),
               "qform_code": int(hdr["qform_code"]),
               "axcodes": "".join(nib.aff2axcodes(img.affine))}
        slope, inter = hdr.get_slope_inter()
        if slope not in (None, 1.0) or inter not in (None, 0.0):
            out["scl"] = [None if slope is None or not np.isfinite(slope) else float(slope),
                          None if inter is None or not np.isfinite(inter) else float(inter)]
        return out
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        return {"error": f"{type(e).__name__}: {e}"[:200]}


def scan(root: Path, time_budget_s: float = TIME_BUDGET_S) -> dict:
    root = root.resolve()
    deadline = time.monotonic() + time_budget_s
    stopped = False
    if not root.is_dir():
        raise ValueError(f"입력 폴더가 없습니다: {root}")
    dirs: dict[str, dict] = {}
    skipped_dirs, docs, errors = [], [], []
    totals = Counter()
    for path, skipped, st in walk(root):
        if time.monotonic() > deadline:
            stopped = True
            break
        if skipped is not None:
            skipped_dirs.append(skipped)
            continue
        rel = path.relative_to(root).as_posix()
        drel = path.parent.relative_to(root).as_posix() if path.parent != root else "."
        d = dirs.setdefault(drel, {"dir": drel, "files": 0, "bytes": 0, "kinds": Counter(), "examples": [],
                                   "nifti_samples": [], "dicom_samples": [], "_dicom_probes": 0, "_stat": 0})
        size = 0
        if st is not None:                    # walk already stat'ed it
            size = st.st_size
            d["_stat"] += 1
        elif d["_stat"] < STAT_PER_DIR:
            try:
                size = path.stat().st_size
                d["_stat"] += 1
            except OSError as e:
                errors.append({"rel": rel, "error": str(e)[:200]})
                continue
        kind = file_kind(path)
        if kind == "dicom?":
            d["_dicom_probes"] += 1
            if d["_dicom_probes"] <= DICOM_PROBES_PER_DIR:
                ds = read_dicom_header(path)
                if ds is not None:
                    kind = "dicom"
                    if len(d["dicom_samples"]) < SAMPLES_PER_DIR:
                        d["dicom_samples"].append({"file": path.name, **header_metadata(ds, ["Modality", "SeriesDescription", "BodyPartExamined", "Rows", "Columns"])})
                else:
                    kind = "other"
            else:
                kind = "dicom(추정)" if d["kinds"].get("dicom") else "other"
        d["files"] += 1
        d["bytes"] += size
        d["kinds"][kind] += 1
        totals[kind] += 1
        totals["bytes"] += size
        if len(d["examples"]) < EXAMPLES:
            d["examples"].append(path.name)
        if kind == "nifti" and len(d["nifti_samples"]) < SAMPLES_PER_DIR:
            d["nifti_samples"].append({"file": path.name, **nifti_header(path)})
        low = path.name.lower()
        if kind in ("doc", "archive") and (kind == "archive" or any(low.startswith(n) for n in DOC_NAMES)
                                           or path.suffix.lower() in {".md", ".csv", ".tsv"}):
            docs.append({"rel": rel, "bytes": size, "kind": kind})
    out_dirs = []
    for d in sorted(dirs.values(), key=lambda x: x["dir"]):
        d.pop("_dicom_probes")
        measured = d.pop("_stat")
        if 0 < measured < d["files"]:          # the rest of a large directory: average size of the measured
            extra = round(d["bytes"] / measured * (d["files"] - measured))
            d["bytes"] += extra
            d["bytes_estimated"] = True
            totals["bytes"] += extra
        d["kinds"] = dict(d["kinds"])
        out_dirs.append(d)
    return {"schema_version": 1, "input_root": str(root), "created_at": now_iso(), "totals": dict(totals),
            "dir_count": len(out_dirs), "dirs_truncated": len(out_dirs) > MAX_DIRS,
            "stopped_after_s": time_budget_s if stopped else None,
            "dirs": out_dirs[:MAX_DIRS], "docs": docs[:200], "skipped_dirs": skipped_dirs, "errors": errors[:100]}


def list_files(root: Path, rel_dir: str, sizes: bool = False) -> list:
    """Every file under root/rel_dir (or the file itself), relative to root, with the inventory's skip rules.
    sizes=True gives (rel, size) pairs, size None when walk could not stat the entry (a symlink)."""
    base = (root / rel_dir).resolve() if rel_dir not in ("", ".") else root
    if base.is_file():
        rel = base.relative_to(root).as_posix()
        return [(rel, base.stat().st_size)] if sizes else [rel]
    out = []
    for path, skipped, st in walk(base):
        if skipped is None and path is not None:
            rel = path.relative_to(root).as_posix()
            out.append((rel, st.st_size if st is not None else None) if sizes else rel)
    return out


def tree_text(scan_result: dict, limit: int = 120) -> str:
    """Compact text tree for prompts and the report."""
    lines = []
    for d in scan_result["dirs"][:limit]:
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(d["kinds"].items()))
        lines.append(f"{d['dir']}/  ({d['files']} files, {d['bytes'] / 1e9:.2f} GB: {kinds})  e.g. "
                     + ", ".join(d["examples"][:3]))
    if scan_result["dir_count"] > limit:
        lines.append(f"... {scan_result['dir_count'] - limit} more directories")
    return "\n".join(lines)


def disk_usage_ok(path: Path, need_bytes: int) -> bool:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize > need_bytes
