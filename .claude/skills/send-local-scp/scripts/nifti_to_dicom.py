"""NIfTI -> DICOM (CT/MR single-frame) conversion for local SCP review.

No resampling: the NIfTI voxel grid is kept as is. Only lossless index
operations (in-plane axis swap, axis flips) are applied so that slices display
in the usual radiological way and slice order follows the slice normal. Every
voxel keeps its world position, which is written to DICOM through
ImagePositionPatient / ImageOrientationPatient / PixelSpacing.

Supported in v1
  - .nii / .nii.gz (NIfTI-1/2), 2D or 3D scalar volumes (trailing singleton
    dimensions are dropped)
  - standard NIfTI (RAS+ world, data (X, Y, Z)) and, when asked explicitly,
    Onco NIfTI (LPS world, data (Z, Y, X), diagonal affine) from nii2OncoNii
  - output modality CT or MR: given by the user, or inferred from a sidecar JSON,
    path words or CT HU values (no evidence / conflicting evidence -> skipped)
Not supported in v1 (reported as skipped, never guessed)
  - 4D+ data (time series / multi-channel), RGB / complex voxels
  - sheared affines, files without sform/qform
  - segmentation masks (they are detected and skipped, not sent as images)
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import nibabel as nib
import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, MRImageStorage, PYDICOM_IMPLEMENTATION_UID
from pydicom.valuerep import format_number_as_ds

CONVERTER_VERSION = "send-local-scp nifti2dcm 1.0"
UID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:send-local-scp:nifti2dcm")
SOP_CLASS_BY_MODALITY = {"CT": CTImageStorage, "MR": MRImageStorage}
RAS_TO_LPS = np.diag([-1.0, -1.0, 1.0, 1.0])

# Path tokens that suggest a segmentation / label file.
MASK_SUBSTRINGS = ("mask", "seg", "label", "annot")
MASK_TOKENS = {"roi", "rois", "gt", "truth", "groundtruth"}
LABEL_MAX_UNIQUE = 20          # integer volume with <= this many values looks like a label map
ORTHO_TOL = 1e-3               # |cos| between voxel axes above this = shear -> unsupported
ONCO_NAME_RE = re.compile(r"_(image|mask)\.nii(\.gz)?$", re.IGNORECASE)
ONCO_MARKERS = ("Debug_dataset_loader_info.json", "dataset.json")


def is_nifti(path: Path) -> bool:
    n = path.name.lower()
    return n.endswith(".nii") or n.endswith(".nii.gz")


def nifti_stem(path: Path) -> str:
    n = path.name
    return n[:-7] if n.lower().endswith(".nii.gz") else n[:-4]


@dataclass
class NiftiItem:
    path: Path
    rel: str                      # posix path relative to the input root
    kind: str = "image"           # image | mask | ambiguous | unsupported
    reason: str = ""
    layout: str = "standard"
    forced: bool = False
    modality: str | None = None
    modality_basis: str = ""
    notes: list = field(default_factory=list)


@dataclass
class Volume:
    data: np.ndarray              # (X, Y, Z) raw stored values
    slope: float
    inter: float
    affine_lps: np.ndarray        # voxel (i, j, k) -> LPS mm
    notes: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def _path_tokens(rel: str) -> list[str]:
    no_ext = re.sub(r"\.nii(\.gz)?$", "", rel, flags=re.IGNORECASE)
    return [t for t in re.split(r"[^a-z0-9]+", no_ext.lower()) if t]


def name_suggests_mask(rel: str) -> bool:
    for t in _path_tokens(rel):
        if t in MASK_TOKENS or any(s in t for s in MASK_SUBSTRINGS):
            return True
    return False


def _real_values(raw: np.ndarray, slope: float, inter: float) -> np.ndarray:
    if slope == 1.0 and inter == 0.0:
        return raw
    return raw.astype(np.float64) * slope + inter


def looks_like_label_map(raw: np.ndarray, slope: float, inter: float) -> bool:
    """Non-negative integer values with only a handful of distinct levels."""
    sub = _real_values(raw[::3, ::3, ::3] if raw.ndim == 3 else raw, slope, inter)
    if np.unique(sub).size > LABEL_MAX_UNIQUE:
        return False
    vals = np.unique(_real_values(raw, slope, inter))
    if vals.size > LABEL_MAX_UNIQUE or not np.all(np.isfinite(vals)):
        return False
    return bool(np.all(vals >= 0) and np.all(vals == np.round(vals)))


def _diag_positive(affine: np.ndarray) -> bool:
    m = affine[:3, :3]
    return bool(np.allclose(m, np.diag(np.diag(m)), atol=1e-6) and np.all(np.diag(m) > 0))


def _onco_marker_near(path: Path, root: Path) -> bool:
    d = path.parent
    for _ in range(4):
        if any((d / m).exists() for m in ONCO_MARKERS):
            return True
        if d == root or d.parent == d:
            break
        d = d.parent
    return False


def _matches_any(rel: str, patterns: list[str]) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(base, p) for p in patterns)


def classify(path: Path, root: Path, layout: str = "auto",
             force_image: list[str] | None = None) -> tuple[NiftiItem, Volume | None]:
    """Decide what a NIfTI file is and load it as a 3D volume if it is sendable."""
    rel = path.relative_to(root).as_posix()
    item = NiftiItem(path=path, rel=rel)
    try:
        img = nib.load(str(path))
    except Exception as e:  # noqa: BLE001 - report any reader failure
        item.kind, item.reason = "unsupported", f"NIfTI 읽기 실패: {e}"
        return item, None

    hdr = img.header
    if hdr.get_data_dtype().names is not None or hdr.get_data_dtype().kind == "c":
        item.kind, item.reason = "unsupported", f"voxel 형식 {hdr.get_data_dtype()} (RGB/complex) 미지원"
        return item, None

    # ---- layout -------------------------------------------------------------
    stored_affine = img.affine
    if layout == "auto":
        if _diag_positive(stored_affine) and (ONCO_NAME_RE.search(path.name) or _onco_marker_near(path, root)):
            item.kind = "ambiguous"
            item.reason = ("Onco NIfTI(LPS, 배열 Z,Y,X)로 보입니다. 표준 NIfTI로 읽으면 축/방향이 틀어지므로 "
                           "--nifti-layout onco 또는 --nifti-layout standard 를 명시하세요")
            return item, None
        layout = "standard"
    item.layout = layout

    # ---- shape --------------------------------------------------------------
    shape = list(img.shape)
    while len(shape) > 3 and shape[-1] == 1:
        shape.pop()
    if layout == "onco":
        if len(shape) == 4:
            if "_mask.nii" in path.name.lower():
                item.kind, item.reason = "mask", "Onco NIfTI mask (C,Z,Y,X) — v1은 mask를 영상으로 보내지 않음"
            else:
                item.kind, item.reason = "unsupported", f"Onco 4D {tuple(img.shape)} (다채널) 미지원"
            return item, None
        if len(shape) != 3:
            item.kind, item.reason = "unsupported", f"Onco NIfTI 차원 {tuple(img.shape)} 미지원 (3D (Z,Y,X)만 지원)"
            return item, None
    elif len(shape) > 3:
        item.kind = "unsupported"
        item.reason = (f"{len(shape)}D {tuple(img.shape)} 미지원 — 4번째 축(시간/채널)이 {shape[3]}개. "
                       "v1은 2D/3D 단일 볼륨만 변환")
        return item, None

    raw = np.asarray(img.dataobj.get_unscaled()).reshape(shape)
    slope = img.dataobj.slope
    inter = img.dataobj.inter
    slope = 1.0 if slope is None or not np.isfinite(slope) or slope == 0 else float(slope)
    inter = 0.0 if inter is None or not np.isfinite(inter) else float(inter)
    if raw.ndim == 2:
        raw = raw[:, :, np.newaxis]
    if raw.dtype == bool:
        raw = raw.astype(np.uint8)

    # ---- image vs mask ------------------------------------------------------
    name_mask = name_suggests_mask(rel)
    label_like = looks_like_label_map(raw, slope, inter)
    forced = bool(force_image) and _matches_any(rel, force_image)
    if name_mask and label_like:
        item.kind, item.reason = "mask", "파일명/경로와 값(정수 라벨 소수 개)이 모두 segmentation mask — 영상으로 보내지 않음"
    elif name_mask:
        item.kind, item.reason = "ambiguous", ("경로에 mask/seg/label 단어가 있지만 값은 연속 영상입니다 "
                                               "(예: skull-strip 'masked' 영상). 영상이 맞으면 --force-image 로 지정")
    elif label_like:
        item.kind, item.reason = "ambiguous", (f"값이 정수 라벨 {LABEL_MAX_UNIQUE}개 이하라 segmentation mask일 수 있습니다. "
                                               "영상이 맞으면 --force-image 로 지정")
    if item.kind in ("mask", "ambiguous"):
        if not forced:
            return item, None
        item.notes.append(f"--force-image 로 영상 처리 (원래 판정: {item.kind})")
        item.kind, item.reason, item.forced = "image", "", True

    # ---- geometry -----------------------------------------------------------
    notes = []
    if layout == "onco":
        data = np.transpose(raw, (2, 1, 0))            # (Z,Y,X) -> (X,Y,Z)
        affine_lps = np.array(stored_affine, dtype=np.float64)
        notes.append("Onco layout: 배열 (Z,Y,X)->(X,Y,Z), affine을 LPS로 해석")
    else:
        sform, scode = img.get_sform(coded=True)
        qform, qcode = img.get_qform(coded=True)
        if scode:
            affine = sform
            if qcode and not np.allclose(sform, qform, atol=1e-3):
                notes.append("sform/qform 불일치 — sform 사용")
        elif qcode:
            affine = qform
        else:
            item.kind, item.reason = "unsupported", "sform/qform 코드가 모두 0 — 방향/위치 정보가 없어 변환하지 않음"
            return item, None
        unit = hdr.get_xyzt_units()[0]
        scale = {"meter": 1000.0, "micron": 0.001}.get(unit, 1.0)
        if scale != 1.0:
            affine = affine.copy()
            affine[:3, :] *= scale
            notes.append(f"공간 단위 {unit} -> mm 변환")
        data = raw
        affine_lps = RAS_TO_LPS @ np.asarray(affine, dtype=np.float64)

    cols = affine_lps[:3, :3]
    norms = np.linalg.norm(cols, axis=0)
    if np.any(norms <= 0):
        item.kind, item.reason = "unsupported", "affine에 0 길이 축이 있음"
        return item, None
    unit_cols = cols / norms
    cos = np.abs(unit_cols.T @ unit_cols - np.eye(3))
    if cos.max() > ORTHO_TOL:
        item.kind, item.reason = "unsupported", f"affine shear(축 비직교, |cos|={cos.max():.4f}) — DICOM으로 표현 불가"
        return item, None

    vol = Volume(data=data, slope=slope, inter=inter, affine_lps=affine_lps, notes=notes)
    item.notes.extend(notes)
    return item, vol


# --------------------------------------------------------------------------- #
# modality
# --------------------------------------------------------------------------- #
MR_TOKENS = {"mr", "mri", "t1", "t1w", "t1c", "t1ce", "t1gd", "t1n", "t1post", "t1pre", "t2", "t2w", "t2f",
             "t2star", "flair", "dwi", "adc", "swi", "pd", "pdw", "mprage", "bold", "dti", "dce"}
CT_TOKENS = {"ct", "cect", "ncct", "cta", "hrct", "ldct", "cbct"}
OTHER_TOKENS = {"pet", "suv", "spect", "us", "ultrasound"}
HU_AIR = -900.0


def _sidecar_modality(path: Path) -> str | None:
    import json
    side = path.with_name(nifti_stem(path) + ".json")
    if not side.is_file():
        return None
    try:
        meta = json.loads(side.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    value = meta.get("Modality") if isinstance(meta, dict) else None
    return str(value).strip().upper() if value else None


def _hu_like(vol: Volume) -> bool:
    """CT in HU: a real share of voxels is air (<= -900) and nothing is absurdly high."""
    sub = vol.data[::2, ::2, ::2].astype(np.float64) * vol.slope + vol.inter
    sub = sub[np.isfinite(sub)]
    return bool(sub.size and np.mean(sub <= HU_AIR) >= 0.02 and sub.min() >= -3100 and sub.max() <= 5000)


def infer_modality(item: NiftiItem, vol: Volume) -> tuple[str | None, str]:
    """Return (CT|MR|None, basis). None means: do not guess, ask for --modality."""
    side = _sidecar_modality(item.path)
    hu = _hu_like(vol)
    if side:
        if side not in SOP_CLASS_BY_MODALITY:
            return None, f"사이드카 JSON Modality={side} — v1은 CT/MR만 변환"
        return side, "사이드카 JSON Modality"
    tokens = set(_path_tokens(item.rel))
    mr, ct, other = tokens & MR_TOKENS, tokens & CT_TOKENS, tokens & OTHER_TOKENS
    if other:
        return None, f"경로 단어 {sorted(other)} — CT/MR 외 modality로 보임"
    if mr and ct:
        return None, f"경로에 MR 단어 {sorted(mr)}와 CT 단어 {sorted(ct)}가 함께 있음"
    if mr:
        if hu:
            return None, f"경로 단어 {sorted(mr)}는 MR인데 값은 CT(HU, 공기 ≤ -900)처럼 보임"
        return "MR", f"경로 단어 {sorted(mr)}"
    if ct:
        return "CT", f"경로 단어 {sorted(ct)}" + ("" if hu else " (값에 HU 공기 영역 없음 — HU 보정 확인)")
    if hu:
        return "CT", "값 분포(HU, 공기 ≤ -900 영역)"
    return None, "사이드카 JSON·경로 단어·값 분포 어디에도 근거 없음"


# --------------------------------------------------------------------------- #
# geometry canonicalisation (lossless)
# --------------------------------------------------------------------------- #
def _flip(data: np.ndarray, affine: np.ndarray, axis: int):
    n = data.shape[axis]
    data = np.flip(data, axis=axis)
    affine = affine.copy()
    affine[:3, 3] = affine[:3, 3] + affine[:3, axis] * (n - 1)
    affine[:3, axis] = -affine[:3, axis]
    return data, affine


def canonicalize(data: np.ndarray, affine: np.ndarray):
    """Swap/flip voxel axes so slices display conventionally; keeps world mapping.

    Column axis (i) and row axis (j) are chosen like standard DICOM IOPs
    (axial 1\\0\\0\\0\\1\\0, coronal 1\\0\\0\\0\\0\\-1, sagittal 0\\1\\0\\0\\0\\-1) and the
    slice axis (k) is flipped so it points along row x column (slice order
    follows the normal).
    """
    ops = []
    u = affine[:3, :3] / np.linalg.norm(affine[:3, :3], axis=0)
    di, dj = int(np.argmax(np.abs(u[:, 0]))), int(np.argmax(np.abs(u[:, 1])))
    if di > dj:
        data = np.swapaxes(data, 0, 1)
        affine = affine.copy()
        affine[:, [0, 1]] = affine[:, [1, 0]]
        ops.append("swap i/j")
    for ax in (0, 1):
        u = affine[:3, ax] / np.linalg.norm(affine[:3, ax])
        d = int(np.argmax(np.abs(u)))
        want = -1.0 if d == 2 else 1.0
        if np.sign(u[d]) != want:
            data, affine = _flip(data, affine, ax)
            ops.append(f"flip {'ij'[ax]}")
    normal = np.cross(affine[:3, 0], affine[:3, 1])
    if np.dot(normal, affine[:3, 2]) < 0:
        data, affine = _flip(data, affine, 2)
        ops.append("flip k (slice order)")
    return data, affine, ops


def plane_label(normal: np.ndarray) -> str:
    n = np.abs(normal / np.linalg.norm(normal))
    d = int(np.argmax(n))
    name = ("SAGITTAL", "CORONAL", "AXIAL")[d]
    tilt = float(np.degrees(np.arccos(min(n[d], 1.0))))
    return name if tilt < 0.5 else f"{name} (oblique {tilt:.0f}°)"


# --------------------------------------------------------------------------- #
# voxel values
# --------------------------------------------------------------------------- #
@dataclass
class Pixels:
    stored: np.ndarray            # int16 or uint16, same shape as input
    slope: float
    inter: float
    exact: bool
    max_abs_error: float
    nonfinite: int


def _ds_float(v: float) -> float:
    return float(format_number_as_ds(float(v)))


def encode_pixels(raw: np.ndarray, slope: float, inter: float) -> Pixels:
    """Map NIfTI values to 16-bit stored values + RescaleSlope/Intercept.

    Integer data is kept bit-exact (NIfTI scl_slope/scl_inter become
    RescaleSlope/Intercept). Other data is linearly quantised to int16 and the
    worst-case error is reported.
    """
    s_ds, i_ds = _ds_float(slope), _ds_float(inter)
    # DS text keeps ~13 significant digits; that rounding is far below any voxel step.
    if raw.dtype.kind in "iu" and np.isclose(s_ds, slope, rtol=1e-9, atol=0) and np.isclose(i_ds, inter, rtol=1e-9, atol=1e-9):
        lo, hi = int(raw.min()), int(raw.max())
        if lo >= -32768 and hi <= 32767:
            return Pixels(raw.astype(np.int16), s_ds, i_ds, True, 0.0, 0)
        if lo >= 0 and hi <= 65535:
            return Pixels(raw.astype(np.uint16), s_ds, i_ds, True, 0.0, 0)

    real = raw.astype(np.float64) * slope + inter
    finite = np.isfinite(real)
    nonfinite = int(real.size - finite.sum())
    if nonfinite:
        real = np.where(finite, real, real[finite].min() if finite.any() else 0.0)
    lo, hi = float(real.min()), float(real.max())
    if np.all(real == np.round(real)):
        if lo >= -32768 and hi <= 32767:
            return Pixels(real.astype(np.int16), 1.0, 0.0, nonfinite == 0, 0.0, nonfinite)
        if lo >= 0 and hi <= 65535:
            return Pixels(real.astype(np.uint16), 1.0, 0.0, nonfinite == 0, 0.0, nonfinite)
    if hi == lo:
        q_slope, q_inter = 1.0, _ds_float(lo)
    else:
        q_slope = _ds_float((hi - lo) / 65535.0)
        q_inter = _ds_float(lo + 32768.0 * q_slope)
    stored = np.clip(np.round((real - q_inter) / q_slope), -32768, 32767).astype(np.int16)
    err = float(np.max(np.abs(stored.astype(np.float64) * q_slope + q_inter - real)))
    return Pixels(stored, q_slope, q_inter, False, err, nonfinite)


# --------------------------------------------------------------------------- #
# DICOM writing
# --------------------------------------------------------------------------- #
def _ds(v: float) -> str:
    return format_number_as_ds(float(v))


def _clip(s: str, n: int = 64) -> str:
    s = re.sub(r"[\\\x00-\x1f]", "_", s)
    if len(s) <= n:
        return s
    h = hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]
    return s[: n - 7] + "~" + h


def _uid(*parts) -> str:
    # Name-based UUID as 2.25.<uuid> (ISO/IEC 9834-8). pydicom's generate_uid(prefix=None)
    # ignores entropy_srcs and would return a random UID.
    name = "\x1f".join([CONVERTER_VERSION, *map(str, parts)])
    return f"2.25.{uuid.uuid5(UID_NAMESPACE, name).int}"


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SeriesIds:
    patient_id: str
    patient_name: str
    study_uid: str
    study_description: str
    series_uid: str
    series_number: int
    series_description: str
    frame_of_reference_uid: str


def make_series_ids(group_label: str, prefix: str, rel: str, content_hash: str,
                    series_number: int) -> SeriesIds:
    """Deterministic UIDs: re-sending the same file gives the same instances."""
    patient_id = _clip(f"{prefix}_{group_label}")
    study_uid = _uid("study", patient_id)
    series_uid = _uid("series", study_uid, rel, content_hash)
    return SeriesIds(
        patient_id=patient_id,
        patient_name="^".join(_clip(re.sub(r"[\^=]", "_", s)) for s in (prefix, group_label)),
        study_uid=study_uid,
        study_description="LOCAL REVIEW (NIfTI to DICOM)",
        series_uid=series_uid,
        series_number=series_number,
        series_description=_clip(nifti_stem(Path(rel))),
        frame_of_reference_uid=_uid("for", series_uid),
    )


def write_series(vol: Volume, ids: SeriesIds, modality: str, rel: str, out_dir: Path,
                 modality_basis: str = "user setting") -> dict:
    """Write one single-frame DICOM file per slice. Returns a geometry/value report."""
    if modality not in SOP_CLASS_BY_MODALITY:
        raise ValueError(f"modality {modality!r} 미지원 (CT 또는 MR)")
    sop_class = SOP_CLASS_BY_MODALITY[modality]
    data, affine, ops = canonicalize(vol.data, vol.affine_lps)
    px = encode_pixels(data, vol.slope, vol.inter)

    spacing = np.linalg.norm(affine[:3, :3], axis=0)          # i (column), j (row), k (slice)
    row_dir = affine[:3, 0] / spacing[0]                       # direction of increasing column index
    col_dir = affine[:3, 1] / spacing[1]                       # direction of increasing row index
    normal = np.cross(row_dir, col_dir)
    ni, nj, nk = data.shape

    step = max(1, round((px.stored.size / 2_000_000) ** (1 / 3)))   # ~2M voxels is plenty for a window
    real_sample = px.stored[::step, ::step, ::step].astype(np.float64) * px.slope + px.inter
    p_lo, p_hi = np.percentile(real_sample, [0.5, 99.5])
    width = max(float(p_hi - p_lo), 1.0)
    center = float(p_lo + p_hi) / 2.0

    now = _dt.datetime.now()
    date, time = now.strftime("%Y%m%d"), now.strftime("%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for k in range(nk):
        ipp = affine @ np.array([0.0, 0.0, float(k), 1.0])
        sop_uid = _uid("sop", ids.series_uid, k)
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = sop_class
        ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID

        ds.SpecificCharacterSet = "ISO_IR 192"
        ds.SOPClassUID = sop_class
        ds.SOPInstanceUID = sop_uid
        ds.InstanceCreationDate, ds.InstanceCreationTime = date, time
        # Patient / study: explicit local-review values, nothing taken as real patient data
        ds.PatientName = ids.patient_name
        ds.PatientID = ids.patient_id
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        ds.StudyInstanceUID = ids.study_uid
        ds.StudyDate = ""
        ds.StudyTime = ""
        ds.ReferringPhysicianName = ""
        ds.StudyID = ""
        ds.AccessionNumber = ""
        ds.StudyDescription = ids.study_description
        # Series
        ds.Modality = modality
        ds.SeriesInstanceUID = ids.series_uid
        ds.SeriesNumber = ids.series_number
        ds.SeriesDescription = ids.series_description
        ds.PatientPosition = ""
        ds.Manufacturer = ""
        ds.SoftwareVersions = CONVERTER_VERSION
        ds.FrameOfReferenceUID = ids.frame_of_reference_uid
        ds.PositionReferenceIndicator = ""
        # Image
        ds.ImageType = ["DERIVED", "SECONDARY"]
        ds.InstanceNumber = k + 1
        ds.AcquisitionNumber = ""
        ds.ContentDate, ds.ContentTime = date, time
        ds.DerivationDescription = _clip(f"Converted from NIfTI without resampling ({CONVERTER_VERSION})", 1024)
        ds.ImageComments = _clip(f"Local review conversion of NIfTI '{rel}'. Patient/study values are local "
                                 f"placeholders, not source metadata. Modality {modality} from: {modality_basis}.",
                                 10240)
        ds.ImagePositionPatient = [_ds(v) for v in ipp[:3]]
        ds.ImageOrientationPatient = [_ds(v) for v in (*row_dir, *col_dir)]
        ds.PixelSpacing = [_ds(spacing[1]), _ds(spacing[0])]     # row spacing \ column spacing
        ds.SliceThickness = _ds(spacing[2])
        ds.SliceLocation = _ds(float(np.dot(ipp[:3], normal)))
        if modality == "MR":
            ds.SpacingBetweenSlices = _ds(spacing[2])
            # Type 1/2 attributes of the MR Image module; values are explicit "unknown" placeholders.
            ds.ScanningSequence = "RM"
            ds.SequenceVariant = "NONE"
            ds.ScanOptions = ""
            ds.MRAcquisitionType = ""
            ds.EchoTime = ""
            ds.EchoTrainLength = ""
        else:
            ds.KVP = ""
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.Rows = nj
        ds.Columns = ni
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1 if px.stored.dtype == np.int16 else 0
        ds.RescaleIntercept = _ds(px.inter)
        ds.RescaleSlope = _ds(px.slope)
        ds.RescaleType = "HU" if modality == "CT" else "US"
        ds.WindowCenter = _ds(center)
        ds.WindowWidth = _ds(width)
        ds.PixelData = np.ascontiguousarray(px.stored[:, :, k].T).astype(px.stored.dtype.newbyteorder("<")).tobytes()
        ds["PixelData"].VR = "OW"

        fp = out_dir / f"IM{k + 1:04d}.dcm"
        ds.save_as(fp, enforce_file_format=True)
        files.append(fp)

    return {
        "files": files,
        "sop_class": sop_class,
        "shape_xyz": [int(ni), int(nj), int(nk)],
        "spacing_mm": [round(float(s), 6) for s in spacing],
        "plane": plane_label(normal),
        "orientation": [round(float(v), 6) for v in (*row_dir, *col_dir)],
        "first_ipp": [round(float(v), 4) for v in (affine @ np.array([0, 0, 0, 1.0]))[:3]],
        "index_ops": ops,
        "values_exact": px.exact,
        "max_abs_error": px.max_abs_error,
        "nonfinite_voxels": px.nonfinite,
        "rescale": [px.slope, px.inter],
    }
