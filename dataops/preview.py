"""샘플 보기 (CONTRACT §102): a few small PNG slices of at most 3 subjects of a dataset, label contours on top.

Read only: the source folder (or a run's input folder) is walked level by level in sorted order — at most 6
sub-folders per folder and 60 per level, a time and directory budget — so "the first 3 subjects" is stable and a
collection of thousands of series is not walked whole. Images are NIfTI / NRRD / MHA volumes (SimpleITK) or DICOM
series (pydicom, SimpleITK for compressed pixel data); labels are mask volumes paired by name (nnU-Net / Decathlon
imagesTr ↔ labelsTr, KiTS case/imaging ↔ segmentation, volume-N ↔ labels-N / segmentation-N, X ↔ X_seg / X_label,
HaN-Seg case_01_IMG_CT ↔ case_01_OAR_*) or an RTSTRUCT that references the series. One image per modality where a
subject has several. Axial slices, patient's right on the image's left, anterior up.

The PNGs are a bounded cache outside the backup (`Settings.preview_dir`, default ~/.cache/ai-platform-dataops/
previews; ≤ 3 subjects per dataset, a total cap, oldest evicted, `python -m dataops previews --purge`) — the one
exception to §78.6's "no pixel files besides the run in flight". Internal data: subjects are shown as S1..S3 and no
path is shown; burned-in text is not removed (the framed app is lead / admin only).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from . import nfs
from .util import now_iso

MAX_SUBJECTS = 3
SLICES = 3
PNG_MAX = 256
WALK_DIRS = 4000
WALK_S = 25.0
PER_PARENT, PER_LEVEL = 6, 60   # sub-folders walked per folder and per level (sorted): the first cases of each branch
DICOM_FILES = 1500
CAP_BYTES = 200 * 1024 * 1024
LABEL_FILES = 16           # mask files read per image (one file per structure in HaN-Seg: 30)
VOLUME_EXT = (".nii.gz", ".nii", ".nrrd", ".mha", ".mhd")
SKIP_DIRS = {"backup", "backups", "__macosx", ".git", ".cache"}
LABEL_WORDS = ("label", "labels", "seg", "segmentation", "segmentations", "mask", "masks", "gt", "annotation",
               "annotations", "oar", "contour", "contours")
IMAGE_WORDS = ("image", "images", "imaging", "img", "volume", "vol", "scan", "data")
MOD_WORDS = {"ct": "CT", "cbct": "CBCT", "mr": "MR", "mri": "MR", "t1": "MR", "t2": "MR", "flair": "MR", "t1c": "MR",
             "t1w": "MR", "t2w": "MR", "dwi": "MR", "adc": "MR", "pet": "PT", "pt": "PT", "suv": "PT"}
PALETTE = [(255, 64, 64), (64, 220, 64), (64, 140, 255), (255, 210, 0), (255, 64, 255), (0, 230, 230),
           (255, 140, 0), (160, 100, 255), (180, 255, 120), (255, 160, 180)]

_lock = threading.Lock()
_busy: set[str] = set()
_render_sem = threading.Semaphore(1)        # one render at a time: a volume can take a GB of memory


# --------------------------------------------------------------------------- cache
def cache_key(root: str) -> str:
    return hashlib.sha256(root.encode("utf-8")).hexdigest()[:20]


def cache_dir(preview_dir: Path, root: str) -> Path:
    return Path(preview_dir) / cache_key(root)


def status(preview_dir: Path, root: str) -> dict | None:
    p = cache_dir(preview_dir, root) / "preview.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "rendering"} if cache_key(root) in _busy else None


def usage(preview_dir: Path) -> dict:
    total, n = 0, 0
    if Path(preview_dir).is_dir():
        for d in Path(preview_dir).iterdir():
            if d.is_dir():
                n += 1
                total += sum(f.stat().st_size for f in d.iterdir() if f.is_file())
    return {"datasets": n, "bytes": total}


def evict(preview_dir: Path, cap: int = CAP_BYTES, keep: str | None = None) -> list[str]:
    """Drop the oldest datasets' previews until the cache is under `cap` bytes."""
    base = Path(preview_dir)
    if not base.is_dir():
        return []
    dirs = []
    for d in base.iterdir():
        if d.is_dir() and d.name != keep:
            size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            dirs.append((d.stat().st_mtime, d, size))
    total = sum(x[2] for x in dirs) + (sum(f.stat().st_size for f in (base / keep).iterdir() if f.is_file())
                                       if keep and (base / keep).is_dir() else 0)
    dropped = []
    for _, d, size in sorted(dirs, key=lambda x: x[0]):
        if total <= cap:
            break
        shutil.rmtree(d, ignore_errors=True)
        total -= size
        dropped.append(d.name)
    return dropped


def purge(preview_dir: Path, root: str | None = None) -> int:
    """Delete one dataset's previews (root) or all of them; returns how many dataset folders went."""
    base = Path(preview_dir)
    if root is not None:
        d = cache_dir(base, root)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            return 1
        return 0
    n = 0
    if base.is_dir():
        for d in base.iterdir():
            if d.is_dir() and re.fullmatch(r"[0-9a-f]{20}", d.name):
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n


def start(preview_dir: Path, root: str, internal: bool, cap: int = CAP_BYTES) -> bool:
    """Render in the background unless done or already rendering. Returns True when a render was started."""
    key = cache_key(root)
    with _lock:
        st = status(preview_dir, root)
        if key in _busy or (st and st.get("state") in ("done", "failed")):
            return False
        _busy.add(key)
    threading.Thread(target=_render_job, args=(Path(preview_dir), root, internal, cap), daemon=True).start()
    return True


def _render_job(preview_dir: Path, root: str, internal: bool, cap: int) -> None:
    key = cache_key(root)
    try:
        with _render_sem:
            render(preview_dir, root, internal, cap)
    finally:
        with _lock:
            _busy.discard(key)


# --------------------------------------------------------------------------- finding subjects
def _is_volume(name: str) -> bool:
    n = name.lower()
    return n.endswith(VOLUME_EXT) and not n.startswith(".")


def _stem(name: str) -> str:
    n = name
    for ext in VOLUME_EXT:
        if n.lower().endswith(ext):
            return n[: -len(ext)]
    return n


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-zA-Z]+", s.lower()) if t]


def _is_label(path: Path) -> bool:
    name = _tokens(_stem(path.name))
    if any(t in LABEL_WORDS for t in name) or path.name.lower().endswith((".seg.nrrd",)):
        return True
    if any(t in IMAGE_WORDS or t in MOD_WORDS for t in name):
        return False
    return any(_label_dir(p) for p in path.parent.parts[-2:])


def _label_dir(part: str) -> bool:
    """labelsTr, Segmentations, masks, GT … (camel case and suffixes included)."""
    p = part.lower()
    return any(w in p for w in ("label", "segmentation", "mask", "annotation")) or p.startswith(("seg", "gt"))


def _modality(path: Path) -> str | None:
    toks = _tokens(_stem(path.name)) + _tokens(path.parent.name)
    for t in toks:
        if t in MOD_WORDS:
            return MOD_WORDS[t]
    return None


def _subject_id(path: Path) -> str:
    """The subject a volume belongs to: its name without channel suffix, image / label / modality words; a generic
    name (imaging.nii.gz) takes its folder's name."""
    stem = _stem(path.name)
    m = re.fullmatch(r"(.*\d)_\d{4}", stem)              # nnU-Net channel suffix: case_0001_0000 -> case_0001
    stem = m.group(1) if m else stem
    stem = re.sub(r"\.seg$", "", stem, flags=re.I)
    toks = _tokens(stem)
    drop = set(LABEL_WORDS) | set(IMAGE_WORDS) | set(MOD_WORDS)
    if "oar" in toks:                                      # HaN-Seg: case_01_OAR_Brainstem -> case_01
        toks = toks[: toks.index("oar")]
    if "img" in toks:                                      # HaN-Seg: case_01_IMG_CT -> case_01
        toks = toks[: toks.index("img")]
    keep = [t for t in toks if t not in drop]
    if not keep:
        return path.parent.name
    return "_".join(keep)


def _probe_dicom(path: Path):
    import pydicom
    try:
        return pydicom.dcmread(nfs.readable(path), stop_before_pixels=True,
                               specific_tags=["Modality", "SeriesInstanceUID", "PatientID", "StudyInstanceUID",
                                              "ReferencedFrameOfReferenceSequence", "SOPClassUID"])
    except Exception:  # noqa: BLE001 - not DICOM / unreadable
        return None


def find_subjects(root: str, max_subjects: int = MAX_SUBJECTS, budget_s: float = WALK_S) -> dict:
    """{"subjects": [{"id", "images": [{"kind", "path" | "files", "modality", "labels", "rtstruct"}]}], "partial",
    "dirs", "found"}. The walk goes level by level in sorted order and stops after the level where enough subjects
    showed up (labels sit beside their images: imagesTr / labelsTr, case folders, sibling series), so the subjects
    are the first ones found — stable from one call to the next."""
    t0 = time.monotonic()
    level, dirs, partial, probes, narrowed, extra = [Path(root)], 0, False, 0, False, 0
    order: list[str] = []                                   # subject ids in discovery order
    subjects: dict[str, dict] = {}
    vol_labels: list[Path] = []
    rtstructs: list[Path] = []
    series: dict[str, dict] = {}
    per_parent: dict = {}

    def subject(sid: str) -> dict:
        if sid not in subjects:
            subjects[sid] = {"id": sid, "images": []}
            order.append(sid)
        return subjects[sid]

    while level:
        nxt = []
        for d in level:
            if dirs >= WALK_DIRS or time.monotonic() - t0 > budget_s:
                partial = True
                break
            dirs += 1
            try:
                names = sorted(nfs.listdir(d))
            except OSError:
                continue
            files, subs = [], 0
            for n in names:
                if n.startswith("."):
                    continue
                p = d / n
                if _is_volume(n) or (len(names) > 200 and "." in n[-6:]):
                    files.append(p)               # a file by its name: no stat per entry in a folder of many files
                elif nfs.is_dir(p):
                    if n.lower() not in SKIP_DIRS:
                        if subs < PER_PARENT and len(nxt) < PER_LEVEL:
                            nxt.append(p)
                        else:
                            narrowed = True       # the rest of this folder's sub-folders is not walked
                        subs += 1
                else:
                    files.append(p)
            for p in files:
                if _is_volume(p.name):
                    if _is_label(p):
                        vol_labels.append(p)
                    else:
                        subject(_subject_id(p))["images"].append(
                            {"kind": "volume", "path": str(p), "modality": _modality(p), "labels": []})
            cand = [p for p in files if p.suffix.lower() in (".dcm", "") and not _is_volume(p.name)]
            if not cand or probes >= 200 or per_parent.get(d.parent, 0) >= 6:
                continue                  # at most 6 series probed per study folder: the probes reach several cases
            probes += 1
            per_parent[d.parent] = per_parent.get(d.parent, 0) + 1
            first = _probe_dicom(cand[0])
            if first is None:
                continue
            mod = str(first.get("Modality", ""))
            if mod == "RTSTRUCT":
                rtstructs.extend(cand[:5])
                continue
            if mod in ("SEG", "RTDOSE", "RTPLAN", "REG", "SR", "PR", "KO"):
                continue
            uid = str(first.get("SeriesInstanceUID", "")) or str(d)
            sid = str(first.get("PatientID", "")) or d.parent.name
            if uid not in series:
                im = {"kind": "dicom", "files": [], "modality": mod or None, "series_uid": uid, "labels": [],
                      "rtstruct": None}
                series[uid] = im
                subject(sid)["images"].append(im)
            series[uid]["files"].extend(str(p) for p in cand[:DICOM_FILES])
        if partial:
            break
        if sum(1 for k in order if subjects[k]["images"]) >= max_subjects:
            # enough images; their masks may still sit in a label folder below (PANORAMA labels/automatic_labels)
            nxt = [d for d in nxt if _label_dir(d.name)]
            if extra >= 2 or not nxt or _paired(subjects, vol_labels):
                break
            extra += 1
        level = nxt
    for p in vol_labels:
        sid = _subject_id(p)
        for im in subjects.get(sid, {}).get("images", []):
            if im["kind"] == "volume":
                im["labels"].append(str(p))
    if rtstructs:
        _pair_rtstructs(subjects, rtstructs)
    ordered = [subjects[k] for k in order if subjects[k]["images"]]
    labelled = [s for s in ordered if any(im["labels"] or im.get("rtstruct") for im in s["images"])]
    chosen = (labelled + [s for s in ordered if s not in labelled])[:max_subjects] if labelled else ordered[:max_subjects]
    for s in chosen:                                       # one image per modality, a labelled one first
        seen, keep = set(), []
        for im in sorted(s["images"], key=lambda im: not (im["labels"] or im.get("rtstruct"))):
            m = im["modality"] or "?"
            if m not in seen:
                seen.add(m)
                keep.append(im)
        s["images"] = keep[:3]
    return {"subjects": chosen, "partial": partial or narrowed, "dirs": dirs, "found": len(ordered)}


def _paired(subjects: dict, vol_labels: list[Path]) -> bool:
    ids = {_subject_id(p) for p in vol_labels}
    return any(k in ids for k, s in subjects.items() if any(im["kind"] == "volume" for im in s["images"]))


def _pair_rtstructs(subjects: dict, rtstructs: list[Path]) -> None:
    import pydicom
    by_uid = {im.get("series_uid"): im for s in subjects.values() for im in s["images"] if im["kind"] == "dicom"}
    for p in rtstructs[:200]:
        try:
            ds = pydicom.dcmread(nfs.readable(p), stop_before_pixels=True)
            refs = [str(se.SeriesInstanceUID) for fr in ds.get("ReferencedFrameOfReferenceSequence", [])
                    for st in fr.get("RTReferencedStudySequence", []) for se in st.get("RTReferencedSeriesSequence", [])]
        except Exception:  # noqa: BLE001
            continue
        for uid in refs:
            if uid in by_uid and not by_uid[uid].get("rtstruct"):
                by_uid[uid]["rtstruct"] = str(p)


# --------------------------------------------------------------------------- rendering
def _window(arr: np.ndarray, modality: str | None) -> np.ndarray:
    a = arr.astype(np.float32)
    if modality in ("CT", "CBCT") or (modality is None and float(np.nanmin(a)) < -500):
        lo, hi = 40 - 200, 40 + 200
    else:
        lo, hi = (float(v) for v in np.nanpercentile(a, [0.5, 99.5]))
        hi = max(hi, lo + 1)
    return (np.clip((a - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)


def _edges(mask: np.ndarray) -> np.ndarray:
    from scipy.ndimage import binary_erosion
    return mask & ~binary_erosion(mask)


def _png(gray: np.ndarray, spacing: tuple[float, float], labels: list[np.ndarray] | None, out: Path) -> None:
    """gray: (rows, cols) uint8; spacing (row mm, col mm); labels: integer masks on the same grid."""
    from PIL import Image
    rows, cols = gray.shape
    h, w = rows * spacing[0], cols * spacing[1]
    scale = PNG_MAX / max(h, w)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    img = Image.fromarray(gray, mode="L").resize(size, Image.BILINEAR).convert("RGB")
    if labels:
        px = np.asarray(img).copy()
        k = 0
        for lab in labels:
            small = np.asarray(Image.fromarray(lab.astype(np.int32)).resize(size, Image.NEAREST))
            for v in [v for v in np.unique(small) if v != 0][:24]:
                e = _edges(small == v)
                px[e] = PALETTE[k % len(PALETTE)]
                k += 1
        img = Image.fromarray(px)
    img.save(out, format="PNG", optimize=True)


def _pick_slices(n: int, label_z: list[int]) -> list[int]:
    if label_z:
        z = sorted(set(label_z))
        idx = [z[len(z) // 2]] + ([z[len(z) // 4], z[(3 * len(z)) // 4]] if len(z) > 2 else [])
    else:
        idx = [int(n * f) for f in (0.5, 0.35, 0.65)]
    out = []
    for i in idx:
        i = min(max(i, 0), n - 1)
        if i not in out:
            out.append(i)
    return sorted(out[:SLICES])


def _read_volume(path: str):
    import SimpleITK as sitk
    img = sitk.ReadImage(str(nfs.readable(path)))
    if img.GetDimension() == 4:
        size = list(img.GetSize())
        img = sitk.Extract(img, size[:3] + [0], [0, 0, 0, 0])
    if img.GetNumberOfComponentsPerPixel() > 1:
        img = sitk.VectorIndexSelectionCast(img, 0)
    if img.GetDimension() != 3:
        raise ValueError(f"3D 영상이 아님 (dim {img.GetDimension()})")
    return img


def render_volume(im: dict, outdir: Path, tag: str) -> list[dict]:
    import SimpleITK as sitk
    img = _read_volume(im["path"])
    labels = []
    skipped = 0
    for lp in im["labels"][:LABEL_FILES]:
        try:
            lab = _read_volume(lp)
        except Exception:  # noqa: BLE001 - a label we cannot read is left out, the image still shows
            continue
        if lab.GetSize() != img.GetSize():   # a mask of another grid (HaN-Seg: CT masks beside the MR) is not this image's
            skipped += 1
            continue
        lab.CopyInformation(img)
        labels.append(sitk.Cast(lab, sitk.sitkUInt16))
    im["labels_used"] = len(labels)
    img = sitk.DICOMOrient(img, "LPS")
    arr = sitk.GetArrayFromImage(img)                     # z, y (anterior first), x (patient's right first)
    labs = [sitk.GetArrayFromImage(sitk.DICOMOrient(lab, "LPS")) for lab in labels]
    if len(labs) > 1 and all(l.max() <= 1 for l in labs):   # one binary file per structure (HaN-Seg)
        merged = np.zeros(arr.shape, dtype=np.uint16)
        for i, l in enumerate(labs, 1):
            merged[l > 0] = i
        labs = [merged]
    label_z = [int(z) for l in labs for z in np.nonzero(l.reshape(l.shape[0], -1).any(axis=1))[0]]
    sx, sy, _ = img.GetSpacing()
    out = []
    if not im.get("modality") and float(arr.min()) < -500:
        im["modality"] = "CT"                              # Hounsfield units
    mod = im.get("modality")
    for z in _pick_slices(arr.shape[0], label_z):
        name = f"{tag}_z{z}.png"
        _png(_window(arr[z], mod), (sy, sx), [l[z] for l in labs] if labs else None, outdir / name)
        out.append({"png": name, "slice": z, "of": int(arr.shape[0]), "labelled": bool(labs) and any(l[z].any() for l in labs)})
    return out


def _dicom_order(files: list[str]) -> list[tuple[float, str, object]]:
    import pydicom
    out = []
    for f in files:
        try:
            ds = pydicom.dcmread(nfs.readable(f), stop_before_pixels=True,
                                 specific_tags=["ImagePositionPatient", "ImageOrientationPatient", "InstanceNumber",
                                                "SOPInstanceUID"])
            if "ImagePositionPatient" in ds and "ImageOrientationPatient" in ds:
                iop = [float(v) for v in ds.ImageOrientationPatient]
                pos = float(np.dot(np.cross(iop[:3], iop[3:]), [float(v) for v in ds.ImagePositionPatient]))
            else:
                pos = float(ds.get("InstanceNumber", 0) or 0)
            out.append((pos, f, str(ds.get("SOPInstanceUID", ""))))
        except Exception:  # noqa: BLE001
            continue
    return sorted(out)


def _dicom_pixels(path: str):
    import pydicom
    ds = pydicom.dcmread(nfs.readable(path))
    try:
        arr = ds.pixel_array.astype(np.float64)
    except Exception:  # noqa: BLE001 - compressed pixel data pydicom cannot decode: SimpleITK's GDCM can
        import SimpleITK as sitk
        arr = sitk.GetArrayFromImage(sitk.ReadImage(str(nfs.readable(path)))).astype(np.float64)
        arr = arr[0] if arr.ndim == 3 else arr
        return ds, arr
    if arr.ndim == 3 and ds.get("SamplesPerPixel", 1) == 1:
        arr = arr[arr.shape[0] // 2]
    arr = arr * float(ds.get("RescaleSlope", 1) or 1) + float(ds.get("RescaleIntercept", 0) or 0)
    return ds, arr


def render_dicom(im: dict, outdir: Path, tag: str) -> list[dict]:
    from .rtstruct import read_overlay
    order = _dicom_order(im["files"])
    if not order:
        raise ValueError("DICOM 영상 파일을 읽지 못했습니다")
    contours = {}
    if im.get("rtstruct"):
        try:
            contours = read_overlay(Path(nfs.readable(im["rtstruct"])))["contours"]
        except Exception:  # noqa: BLE001
            contours = {}
    label_z = [i for i, (_, _, sop) in enumerate(order) if sop in contours]
    out = []
    for z in _pick_slices(len(order), label_z):
        _, f, sop = order[z]
        ds, arr = _dicom_pixels(f)
        if arr.ndim != 2:
            raise ValueError(f"2D 단일 채널이 아님 (shape {arr.shape})")
        gray = _window(arr, im.get("modality"))
        if ds.get("PhotometricInterpretation") == "MONOCHROME1":
            gray = 255 - gray
        mask = None
        if contours.get(sop):
            mask = _contour_mask(ds, arr.shape, contours[sop])
        dr, dc = (float(v) for v in ds.get("PixelSpacing", [1, 1]))
        name = f"{tag}_z{z}.png"
        _png(gray, (dr, dc), [mask] if mask is not None else None, outdir / name)
        out.append({"png": name, "slice": z, "of": len(order), "labelled": mask is not None})
    return out


def _contour_mask(ds, shape, contours) -> np.ndarray:
    from PIL import Image, ImageDraw
    ipp = np.array([float(v) for v in ds.ImagePositionPatient])
    iop = [float(v) for v in ds.ImageOrientationPatient]
    row_dir, col_dir = np.array(iop[:3]), np.array(iop[3:])
    dr, dc = (float(v) for v in ds.PixelSpacing)
    img = Image.new("I", (shape[1], shape[0]), 0)
    draw = ImageDraw.Draw(img)
    colors: dict = {}
    for color, pts in contours:
        v = colors.setdefault(tuple(color), len(colors) + 1)
        col = (pts - ipp) @ row_dir / dc
        row = (pts - ipp) @ col_dir / dr
        xy = [(float(c), float(r)) for c, r in zip(col, row)]
        if len(xy) >= 3:
            draw.polygon(xy, outline=v, fill=v)
    return np.asarray(img).astype(np.int32)


def render(preview_dir: Path, root: str, internal: bool, cap: int = CAP_BYTES) -> dict:
    """Find the subjects, render their slices, write preview.json; evict old previews over the cap."""
    outdir = cache_dir(preview_dir, root)
    tmp = outdir.with_name(outdir.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    meta = {"root": None if internal else root, "internal": internal, "started_at": now_iso(), "subjects": []}
    try:
        found = find_subjects(root)
        meta.update(partial=found["partial"], dirs=found["dirs"], found=found["found"])
        for i, s in enumerate(found["subjects"], 1):
            sub = {"id": f"S{i}" if internal else s["id"], "images": []}
            for j, im in enumerate(s["images"], 1):
                tag = f"s{i}_{j}"
                item = {"kind": im["kind"], "source": None if internal else (im.get("path") or str(Path(im["files"][0]).parent)),
                        "slices": []}
                try:
                    item["slices"] = (render_volume if im["kind"] == "volume" else render_dicom)(im, tmp, tag)
                except Exception as e:  # noqa: BLE001 - one unreadable image does not stop the others
                    item["error"] = f"{type(e).__name__}: {e}"[:300]
                used = im.get("labels_used", len(im["labels"]))
                item["modality"] = im.get("modality") or "?"
                item["label"] = "RTSTRUCT" if im.get("rtstruct") else (
                    (f"마스크 {used}개" + (f" (전체 {len(im['labels'])}개 중)" if used != len(im["labels"]) else ""))
                    if used else f"마스크 {len(im['labels'])}개는 다른 영상 격자 — 겹치지 않음") if im["labels"] else None
                sub["images"].append(item)
            meta["subjects"].append(sub)
        meta["state"] = "done"
    except Exception as e:  # noqa: BLE001
        meta.update(state="failed", error=f"{type(e).__name__}: {e}"[:500])
    meta.update(finished_at=now_iso(), seconds=round(time.monotonic() - t0, 1))
    (tmp / "preview.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(outdir, ignore_errors=True)
    os.replace(tmp, outdir)
    evict(preview_dir, cap, keep=outdir.name)
    return meta
