"""Metadata view of a run: every value extracted from the dataset, per series, and where each one goes —
into the DICOM headers (the pilot to the local SCP and, after approval, Central), into the management DB, or
nowhere (it stays in the run folder). A read-only projection of the plan, a cached NIfTI header extract and,
once the pilot is frozen, the real DICOM headers; every claimed destination is checked against the real
DB-row projection (`records.build_records`) and the real pilot headers, so the table cannot drift from the code.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import nibabel as nib
import numpy as np
import pydicom

from .prepare import MANIFESTS, pilot_series
from .records import build_records, subject_uuid
from .runs import Run
from .util import now_iso, write_json

HEADERS_FILE = "nifti_headers.json"
NIFTI_FIELDS = ["dim", "pixdim", "datatype", "bitpix", "scl_slope", "scl_inter", "cal_min", "cal_max",
                "qform_code", "sform_code", "quatern_b", "quatern_c", "quatern_d", "qoffset_x", "qoffset_y",
                "qoffset_z", "srow_x", "srow_y", "srow_z", "xyzt_units", "descrip", "aux_file", "intent_code",
                "intent_name", "intent_p1", "intent_p2", "intent_p3", "dim_info", "slice_code", "slice_start",
                "slice_end", "slice_duration", "toffset", "vox_offset", "magic"]
GROUPS = {"plan": "판정 (분석 결과)", "file": "원본 파일", "nifti": "NIfTI 헤더 (주요)",
          "nifti_all": "NIfTI 헤더 (나머지)", "conv": "정규화한 기하", "ids": "DICOM 식별자", "db": "DB 전용"}


# ---------------------------------------------------------------------- NIfTI header extract (cached)
def _plain(v):
    if isinstance(v, (bytes, np.bytes_)):
        return bytes(v).split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    if isinstance(v, np.ndarray):
        return _plain(v.item()) if v.ndim == 0 else [_plain(x) for x in v.tolist()]
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if not math.isfinite(f) else round(f, 6)
    if isinstance(v, (np.integer, int)):
        return int(v)
    return v


def read_nifti_header(path: Path) -> dict:
    try:
        hdr = nib.load(str(path)).header
    except Exception as e:  # noqa: BLE001 - shown in the table, never fatal
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    out = {}
    for k in NIFTI_FIELDS:
        try:
            out[k] = _plain(hdr[k])
        except (KeyError, ValueError):
            pass
    return out


def nifti_headers(run: Run, plan: dict, refresh: bool = False) -> dict:
    """{rel: header} for every NIfTI source in the plan, cached in the run folder. Written once after the
    analysis; `refresh` re-reads files whose size/mtime changed (a stat per file is slow on network mounts).
    Conversion never uses this cache: prepare re-hashes every source it freezes."""
    root = Path(plan["input_root"])
    rels = sorted({rel for s in plan.get("series", []) if s["kind"] in ("nifti", "rtstruct")
                   for rel in s.get("sources", [])})
    cache = run.load(HEADERS_FILE) or {}
    files = cache.get("files", {}) if cache.get("input_root") == str(root) else {}
    out, changed = {}, False
    for rel in rels:
        hit = files.get(rel)
        if hit is not None and not refresh:
            out[rel] = hit
            continue
        try:
            st = (root / rel).stat()
        except OSError as e:
            out[rel] = {"stamp": None, "size": None, "header": {"error": str(e)[:200]}}
            changed = True
            continue
        stamp = [st.st_size, st.st_mtime_ns]
        if hit is None or hit.get("stamp") != stamp:
            hit = {"stamp": stamp, "size": st.st_size, "header": read_nifti_header(root / rel)}
            changed = True
        out[rel] = hit
    if changed or set(files) != set(out):
        write_json(run.path(HEADERS_FILE), {"input_root": str(root), "read_at": now_iso(), "files": out})
    return out


# ---------------------------------------------------------------------- columns
@dataclass
class Col:
    key: str
    label: str
    group: str
    value: Callable[[dict], Any]
    kinds: tuple = ("nifti", "rtstruct", "dicom")
    dicom: str | None = None                     # where it lands in the DICOM header
    dicom_val: Callable[[Any], Any] | None = None    # read that value back from a real header
    dicom_expect: Callable[[dict], Any] | None = None
    db: str | None = None                        # table.column it is registered in
    db_val: Callable[[dict, dict], Any] | None = None    # read it back from the DB-row projection
    note: str = ""


def _h(ctx, field):
    return (ctx["hdr"] or {}).get(field)


def _origin(ctx):
    h = ctx["hdr"] or {}
    if h.get("sform_code"):
        return [r[3] for r in (h.get("srow_x"), h.get("srow_y"), h.get("srow_z")) if r]
    if "qoffset_x" in h:
        return [h.get("qoffset_x"), h.get("qoffset_y"), h.get("qoffset_z")]
    return None


def _m(ctx, k):
    return (ctx["s"].get("metadata") or {}).get(k)


def _ser(idx, s):
    return idx["series"].get(s["series_uid"]) or {}


def _rois(ds):
    return [str(r.ROIName) for r in ds.get("StructureSetROISequence", [])]


def _rs_ref_series(ds):
    try:
        return str(ds.ReferencedFrameOfReferenceSequence[0].RTReferencedStudySequence[0]
                   .RTReferencedSeriesSequence[0].SeriesInstanceUID)
    except (AttributeError, IndexError):
        return None


COLUMNS = [
    Col("included", "포함", "plan", lambda c: not c["s"].get("excluded"),
        note="제외된 시리즈는 어디로도 보내지 않습니다"),
    Col("pilot", "파일럿", "plan", lambda c: c["pilot"],
        note="파일럿 대상자 2명의 시리즈만 Local SCP로 갑니다"),
    Col("split", "분할", "plan", lambda c: c["s"].get("split"),
        db="series.metadata_json.split", db_val=lambda i, s: (_ser(i, s).get("metadata_json") or {}).get("split")),
    Col("subject_key", "대상자 키", "plan", lambda c: c["s"]["subject_key"],
        dicom="PatientID·PatientName 안에 포함",
        db="subjects.source_subject_key",
        db_val=lambda i, s: (i["subjects"].get(s["subject_key"]) or {}).get("source_subject_key")),
    Col("kind", "종류", "plan", lambda c: c["s"]["kind"],
        db="series.source_kind", db_val=lambda i, s: _ser(i, s).get("source_kind")),
    Col("modality", "Modality", "plan", lambda c: c["s"].get("modality"),
        dicom="Modality (0008,0060)", dicom_val=lambda ds: ds.get("Modality"),
        db="series.modality", db_val=lambda i, s: _ser(i, s).get("modality")),
    Col("modality_basis", "Modality 근거", "plan", lambda c: c["s"].get("modality_basis"),
        dicom="ImageComments (0020,4000) 안에 포함 (영상만)",
        db="series.modality_basis", db_val=lambda i, s: _ser(i, s).get("modality_basis")),
    Col("label_of", "라벨의 영상", "plan", lambda c: _m(c, "label_of"), kinds=("rtstruct",),
        dicom="ReferencedFrameOfReferenceSequence → 참조 Series UID",
        dicom_val=_rs_ref_series, dicom_expect=lambda c: (c["image"] or {}).get("series_uid"),
        db="series.metadata_json.label_of",
        db_val=lambda i, s: (_ser(i, s).get("metadata_json") or {}).get("label_of")),
    Col("roi_names", "ROI 이름", "plan", lambda c: c["roi_names"], kinds=("rtstruct",),
        dicom="StructureSetROISequence.ROIName (라벨 맵에 있는 값만)",
        db="ingest_runs.plan_json (conversion.label_names)",
        note="RTSTRUCT에는 실제 라벨 맵에 있는 값만 ROI로 들어갑니다"),
    Col("source", "원본 파일", "file", lambda c: (c["s"].get("sources") or [None])[0],
        dicom="ImageComments (0020,4000) 안에 포함 (영상)",
        db="instances.source_relative_path", note="instances 행은 전체 변환 뒤에 생깁니다"),
    Col("size", "파일 크기(B)", "file", lambda c: c["size"], note="어디에도 기록하지 않음"),
    Col("dicom_metadata", "원본 DICOM 태그", "file", lambda c: c["s"].get("metadata"), kinds=("dicom",),
        dicom="원본 파일 그대로 전송", db="series.metadata_json",
        db_val=lambda i, s: _ser(i, s).get("metadata_json")),
    # NIfTI header — main fields
    Col("n_dim", "dim", "nifti", lambda c: (_h(c, "dim") or [None])[1:1 + (_h(c, "dim") or [0])[0]] or None,
        kinds=("nifti", "rtstruct")),
    Col("n_pixdim", "pixdim", "nifti", lambda c: (_h(c, "pixdim") or [None] * 4)[1:4], kinds=("nifti", "rtstruct")),
    Col("n_datatype", "datatype", "nifti", lambda c: _h(c, "datatype"), kinds=("nifti", "rtstruct")),
    Col("n_scl", "scl_slope/inter", "nifti", lambda c: [_h(c, "scl_slope"), _h(c, "scl_inter")],
        kinds=("nifti",), dicom="RescaleSlope/Intercept 계산에 반영"),
    Col("n_codes", "qform/sform", "nifti", lambda c: [_h(c, "qform_code"), _h(c, "sform_code")],
        kinds=("nifti", "rtstruct"), dicom="방향·위치 계산에 사용"),
    Col("n_origin", "원점(RAS)", "nifti", _origin, kinds=("nifti", "rtstruct"),
        dicom="ImagePositionPatient 계산에 사용 (LPS로 변환)"),
    Col("n_units", "xyzt_units", "nifti", lambda c: _h(c, "xyzt_units"), kinds=("nifti", "rtstruct")),
    Col("n_descrip", "descrip", "nifti", lambda c: _h(c, "descrip"), kinds=("nifti", "rtstruct"),
        note="원본에만 있음"),
    Col("n_aux", "aux_file", "nifti", lambda c: _h(c, "aux_file"), kinds=("nifti", "rtstruct"),
        note="원본에만 있음"),
    Col("n_intent", "intent", "nifti", lambda c: [_h(c, "intent_code"), _h(c, "intent_name")],
        kinds=("nifti", "rtstruct")),
    Col("n_cal", "cal_min/max", "nifti", lambda c: [_h(c, "cal_min"), _h(c, "cal_max")], kinds=("nifti", "rtstruct")),
    *[Col(f"n_{f}", f, "nifti_all", (lambda f: lambda c: _h(c, f))(f), kinds=("nifti", "rtstruct"))
      for f in ["bitpix", "quatern_b", "quatern_c", "quatern_d", "qoffset_x", "qoffset_y", "qoffset_z",
                "srow_x", "srow_y", "srow_z", "intent_p1", "intent_p2", "intent_p3", "dim_info", "slice_code",
                "slice_start", "slice_end", "slice_duration", "toffset", "vox_offset", "magic"]],
    # normalised geometry (what the converter writes)
    Col("shape_xyz", "크기 x·y·z", "conv", lambda c: _m(c, "shape_xyz"), kinds=("nifti",),
        dicom="Columns·Rows (0028,0011/0010)", dicom_val=lambda ds: [int(ds.Columns), int(ds.Rows)],
        dicom_expect=lambda c: (_m(c, "shape_xyz") or [None, None])[:2],
        db="series.metadata_json.shape_xyz",
        db_val=lambda i, s: (_ser(i, s).get("metadata_json") or {}).get("shape_xyz")),
    Col("spacing_mm", "간격 mm", "conv", lambda c: _m(c, "spacing_mm"), kinds=("nifti",),
        dicom="PixelSpacing·SliceThickness",
        dicom_val=lambda ds: [float(ds.PixelSpacing[1]), float(ds.PixelSpacing[0]), float(ds.SliceThickness)],
        db="series.metadata_json.spacing_mm",
        db_val=lambda i, s: (_ser(i, s).get("metadata_json") or {}).get("spacing_mm")),
    Col("axcodes", "축 방향", "conv", lambda c: _m(c, "axcodes"), kinds=("nifti",),
        dicom="ImageOrientationPatient 계산에 반영",
        db="series.metadata_json.axcodes",
        db_val=lambda i, s: (_ser(i, s).get("metadata_json") or {}).get("axcodes")),
    Col("dtype", "자료형", "conv", lambda c: _m(c, "dtype"), kinds=("nifti",),
        dicom="BitsStored·PixelRepresentation 계산에 반영",
        db="series.metadata_json.dtype",
        db_val=lambda i, s: (_ser(i, s).get("metadata_json") or {}).get("dtype")),
    # DICOM identifiers
    Col("patient_id", "PatientID", "ids", lambda c: c["s"].get("patient_id"),
        dicom="PatientID (0010,0020)", dicom_val=lambda ds: ds.get("PatientID"),
        db="subjects.patient_id", db_val=lambda i, s: (i["subjects"].get(s["subject_key"]) or {}).get("patient_id")),
    Col("patient_name", "PatientName", "ids", lambda c: c["conv"].get("patient_name"),
        dicom="PatientName (0010,0010)", dicom_val=lambda ds: str(ds.get("PatientName", "")),
        db="ingest_runs.plan_json (conversion.patient_name)"),
    Col("study_uid", "Study UID", "ids", lambda c: c["s"].get("study_uid"),
        dicom="StudyInstanceUID (0020,000D)", dicom_val=lambda ds: ds.get("StudyInstanceUID"),
        db="studies.study_uid", db_val=lambda i, s: (i["studies"].get(s["study_uid"]) or {}).get("study_uid")),
    Col("study_description", "StudyDescription", "ids", lambda c: c["conv"].get("study_description"),
        dicom="StudyDescription (0008,1030)", dicom_val=lambda ds: ds.get("StudyDescription"),
        db="studies.description", db_val=lambda i, s: (i["studies"].get(s["study_uid"]) or {}).get("description"),
        note="데이터셋 이름 (64자 제한, 길면 끝에 해시)"),
    Col("series_uid", "Series UID", "ids", lambda c: c["s"].get("series_uid"),
        dicom="SeriesInstanceUID (0020,000E)", dicom_val=lambda ds: ds.get("SeriesInstanceUID"),
        db="series.series_uid", db_val=lambda i, s: _ser(i, s).get("series_uid")),
    Col("series_number", "SeriesNumber", "ids", lambda c: c["s"].get("series_number"),
        dicom="SeriesNumber (0020,0011)", dicom_val=lambda ds: int(ds.SeriesNumber),
        db="series.series_number", db_val=lambda i, s: _ser(i, s).get("series_number")),
    Col("series_description", "SeriesDescription", "ids", lambda c: (c["s"].get("description") or "")[:64] or None,
        dicom="SeriesDescription (0008,103E)", dicom_val=lambda ds: ds.get("SeriesDescription"),
        db="series.description", db_val=lambda i, s: (_ser(i, s).get("description") or "")[:64] or None),
    Col("frame_of_reference", "FrameOfReference UID", "ids",
        lambda c: (c["image"] or c["s"]).get("conversion", {}).get("frame_of_reference_uid"),
        kinds=("nifti", "rtstruct"), dicom="FrameOfReferenceUID / ReferencedFrameOfReference",
        dicom_val=lambda ds: ds.get("FrameOfReferenceUID") or
        ds.ReferencedFrameOfReferenceSequence[0].FrameOfReferenceUID,
        db="ingest_runs.plan_json (conversion.frame_of_reference_uid)", note="영상과 RTSTRUCT가 같은 값"),
    Col("instance_count", "인스턴스 수", "ids", lambda c: c["s"].get("instance_count"),
        dicom="파일(SOP Instance) 개수", db="series.instance_count",
        db_val=lambda i, s: _ser(i, s).get("instance_count")),
    Col("subject_uuid", "subject UUID", "db", lambda c: c["subject_uuid"],
        db="subjects.subject_uuid", db_val=lambda i, s: (i["subjects"].get(s["subject_key"]) or {}).get("subject_uuid"),
        note="DB 전용 식별자 (dataset UUID + 대상자 키)"),
]

DATASET_ITEMS = [  # (key, label, DICOM destination, DB destination)
    ("name", "데이터셋 이름", "StudyDescription (64자) · ImageComments", "datasets.name"),
    ("description", "설명", None, "datasets.description"),
    ("license", "라이선스", None, "datasets.license"),
    ("commercial_use", "상업적 이용", None, "datasets.commercial_use"),
    ("source_url", "출처 URL", None, "datasets.source_url"),
    ("patient_id_prefix", "PatientID 접두어", "PatientID·PatientName 앞부분", "datasets.patient_id_prefix"),
    ("dataset_uuid", "dataset UUID", None, "datasets.dataset_uuid · subjects · dataset_series"),
]

TAG_SOURCE = {   # keyword -> (kind, explanation) for every top-level element the converters write
    "SpecificCharacterSet": ("고정", "ISO_IR 192 (UTF-8)"),
    "ImageType": ("고정", "DERIVED\\SECONDARY"),
    "SOPClassUID": ("분석", "Modality에 따른 저장 클래스 (CT/MR Image, RT Structure Set)"),
    "SOPInstanceUID": ("UID", "Series UID + 슬라이스 번호로 결정적으로 생성"),
    "InstanceCreationDate": ("빈 값", "결정적 변환이라 넣지 않음"),
    "InstanceCreationTime": ("빈 값", "결정적 변환이라 넣지 않음"),
    "StudyDate": ("빈 값", "원본에 없음"), "StudyTime": ("빈 값", "원본에 없음"),
    "ContentDate": ("빈 값", "원본에 없음"), "ContentTime": ("빈 값", "원본에 없음"),
    "AccessionNumber": ("빈 값", "원본에 없음"), "ReferringPhysicianName": ("빈 값", "원본에 없음"),
    "StudyID": ("빈 값", "원본에 없음"), "Manufacturer": ("빈 값", "원본에 없음"),
    "PatientBirthDate": ("빈 값", "원본에 없음"), "PatientSex": ("빈 값", "원본에 없음"),
    "PatientPosition": ("빈 값", "원본에 없음"), "PositionReferenceIndicator": ("빈 값", "원본에 없음"),
    "AcquisitionNumber": ("빈 값", "원본에 없음"), "KVP": ("빈 값", "원본에 없음 (CT 필수 태그라 빈 값)"),
    "ScanOptions": ("빈 값", "원본에 없음"), "MRAcquisitionType": ("빈 값", "원본에 없음"),
    "EchoTime": ("빈 값", "원본에 없음"), "EchoTrainLength": ("빈 값", "원본에 없음"),
    "ScanningSequence": ("고정", "MR 필수 태그: RM (연구용)"), "SequenceVariant": ("고정", "MR 필수 태그: NONE"),
    "Modality": ("분석", "에이전트 규칙으로 정한 modality"),
    "StudyDescription": ("데이터셋", "데이터셋 이름 (64자 제한)"),
    "SeriesDescription": ("분석", "시리즈 설명 (대상자 키 / 'labels')"),
    "DerivationDescription": ("고정", "NIfTI에서 리샘플링 없이 변환 + 변환기 버전"),
    "ImageComments": ("분석", "원본 경로 · 데이터셋 이름 · modality 근거"),
    "PatientName": ("분석", "접두어^대상자 키"), "PatientID": ("분석", "접두어_대상자 키"),
    "StudyInstanceUID": ("UID", "dataset UUID + 대상자로 결정적 생성"),
    "SeriesInstanceUID": ("UID", "원본 경로로 결정적 생성"),
    "FrameOfReferenceUID": ("UID", "영상 시리즈별 결정적 생성"),
    "SeriesNumber": ("분석", "대상자 안의 순번 (라벨은 1000+)"),
    "SoftwareVersions": ("고정", "변환기 버전"),
    "InstanceNumber": ("계산", "슬라이스 순서 (RTSTRUCT는 1)"),
    "ImagePositionPatient": ("기하", "NIfTI affine에서 LPS 위치 계산"),
    "ImageOrientationPatient": ("기하", "NIfTI affine에서 방향 계산"),
    "SliceLocation": ("기하", "위치를 법선에 투영"), "SliceThickness": ("기하", "NIfTI 간격"),
    "SpacingBetweenSlices": ("기하", "NIfTI 간격"), "PixelSpacing": ("기하", "NIfTI 간격 (행\\열)"),
    "Rows": ("기하", "NIfTI 크기"), "Columns": ("기하", "NIfTI 크기"),
    "SamplesPerPixel": ("고정", "1"), "PhotometricInterpretation": ("고정", "MONOCHROME2"),
    "BitsAllocated": ("고정", "16"), "BitsStored": ("고정", "16"), "HighBit": ("고정", "15"),
    "PixelRepresentation": ("화소", "값 범위에 따라 부호 있음/없음"),
    "RescaleIntercept": ("화소", "NIfTI scl·값 범위에서 계산 (값 보존)"),
    "RescaleSlope": ("화소", "NIfTI scl·값 범위에서 계산 (값 보존)"),
    "RescaleType": ("분석", "CT는 HU, 그 외 US"),
    "WindowCenter": ("화소", "값 분포에서 계산한 기본 창"), "WindowWidth": ("화소", "값 분포에서 계산한 기본 창"),
    "PixelData": ("화소", "NIfTI 값 그대로 (리샘플링 없음)"),
    "StructureSetLabel": ("고정", "LABELS"), "StructureSetName": ("분석", "시리즈 설명"),
    "StructureSetDate": ("빈 값", "원본에 없음"), "StructureSetTime": ("빈 값", "원본에 없음"),
    "ReferencedFrameOfReferenceSequence": ("UID", "참조 영상의 FoR·Study·Series·슬라이스 SOP"),
    "StructureSetROISequence": ("분석", "라벨 값 → ROI 이름 (dataset.json 등의 라벨 정의)"),
    "ROIContourSequence": ("라벨", "라벨 맵 윤곽선 (슬라이스별, 참조 SOP 포함)"),
    "RTROIObservationsSequence": ("라벨", "ROI별 관찰 항목"),
}


# ---------------------------------------------------------------------- projection
def _same(a, b) -> bool:
    if a is None and b in (None, ""):
        return True
    try:
        fa, fb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
        if fa.shape == fb.shape and fa.size:
            return bool(np.allclose(fa, fb, atol=1e-3))
    except (TypeError, ValueError):
        pass
    return str(a) == str(b)


def _contexts(plan: dict, headers: dict) -> list[dict]:
    by_key = {s["series_key"]: s for s in plan.get("series", [])}
    pilot = {s["series_key"] for s in pilot_series(plan)} if plan.get("series") else set()
    duuid = plan["dataset"]["dataset_uuid"]
    out = []
    for s in plan.get("series", []):
        image = by_key.get(s.get("reference")) if s["kind"] == "rtstruct" else None
        src = (s.get("sources") or [None])[0]
        h = headers.get(src) or {}
        conv = dict((image or s).get("conversion") or {})
        names = (s.get("conversion") or {}).get("label_names")
        out.append({"s": s, "image": image, "hdr": h.get("header"), "size": h.get("size"),
                    "conv": conv, "pilot": s["series_key"] in pilot,
                    "roi_names": [f"{k} {v}" for k, v in sorted(names.items(), key=lambda kv: int(kv[0]))]
                    if names else None,
                    "subject_uuid": subject_uuid(duuid, s["subject_key"])})
    return out


def _index(records: dict) -> dict:
    return {"subjects": {r["source_subject_key"]: r for r in records["subjects"]},
            "studies": {r["study_uid"]: r for r in records["studies"]},
            "series": {r["series_uid"]: r for r in records["series"]}}


def _frozen_headers(run: Run, uids: set) -> dict:
    """{series_uid: first instance header} for frozen series (pilot first)."""
    out = {}
    for name in MANIFESTS.values():
        m = run.load(name)
        for inst in (m or {}).get("instances", []):
            uid = inst["series_uid"]
            if uid in uids and uid not in out and inst.get("instance_number") in (1, None):
                try:
                    out[uid] = pydicom.dcmread(run.dir / inst["path"], stop_before_pixels=True)
                except Exception:  # noqa: BLE001 - shown as "not checked"
                    pass
    return out


def build(run: Run, refresh: bool = False) -> dict:
    """Everything the metadata page shows; also the source of the JSON/CSV downloads."""
    plan = run.load("plan.json") or {}
    if not plan.get("series"):
        return {"ready": False}
    headers = nifti_headers(run, plan, refresh)
    ctxs = _contexts(plan, headers)
    records = build_records(run, preview=True)
    idx = _index(records)
    active = [c for c in ctxs if not c["s"].get("excluded")]
    pilot_uids = {c["s"]["series_uid"] for c in active if c["pilot"]}
    real = _frozen_headers(run, pilot_uids)

    rows, checks = [], {col.key: {"db_ok": 0, "db_bad": [], "dicom_ok": 0, "dicom_bad": []} for col in COLUMNS}
    for c in ctxs:
        s = c["s"]
        row = {"_uid": s["series_uid"], "_kind": s["kind"]}
        for col in COLUMNS:
            v = col.value(c) if s["kind"] in col.kinds else None
            row[col.key] = v
            if s.get("excluded") or s["kind"] not in col.kinds:
                continue
            ck = checks[col.key]
            if col.db_val:
                got = col.db_val(idx, s)
                if _same(v, got):
                    ck["db_ok"] += 1
                elif len(ck["db_bad"]) < 5:
                    ck["db_bad"].append({"series": s["series_key"], "expected": v, "db": got})
            ds = real.get(s["series_uid"])
            if col.dicom_val and ds is not None:
                want = col.dicom_expect(c) if col.dicom_expect else v
                try:
                    got = col.dicom_val(ds)
                except (AttributeError, IndexError, KeyError, ValueError) as e:
                    got = f"읽기 실패: {e}"
                if _same(want, got):
                    ck["dicom_ok"] += 1
                elif len(ck["dicom_bad"]) < 5:
                    ck["dicom_bad"].append({"series": s["series_key"], "expected": want, "dicom": str(got)})
        rows.append(row)

    example = next((c for c in active if c["pilot"] and c["s"]["kind"] != "rtstruct"), active[0] if active else None)
    example_rt = next((c for c in active if c["pilot"] and c["s"]["kind"] == "rtstruct"), None)
    flow = []
    kinds = {c["s"]["kind"] for c in active}
    for col in COLUMNS:
        if col.group == "nifti_all" or col.key in ("included", "pilot") or not kinds & set(col.kinds):
            continue                                   # filters of the table, not data that travels
        ex = example_rt if (example and example["s"]["kind"] not in col.kinds) else example
        ck = checks[col.key]
        flow.append({"key": col.key, "label": col.label, "group": GROUPS[col.group],
                     "example": col.value(ex) if ex and ex["s"]["kind"] in col.kinds else None,
                     "dicom": col.dicom, "db": col.db, "note": col.note,
                     "db_check": ("ok" if not ck["db_bad"] else "bad") if ck["db_ok"] or ck["db_bad"] else None,
                     "db_checked": ck["db_ok"] + len(ck["db_bad"]), "db_bad": ck["db_bad"],
                     "dicom_check": (("ok" if not ck["dicom_bad"] else "bad") if ck["dicom_ok"] or ck["dicom_bad"]
                                     else "pending") if col.dicom_val else None,
                     "dicom_checked": ck["dicom_ok"] + len(ck["dicom_bad"]), "dicom_bad": ck["dicom_bad"]})

    ds_row = records["datasets"][0]
    dataset = [{"label": label, "value": plan["dataset"].get(k), "dicom": dicom, "db": db,
                "db_check": _same(plan["dataset"].get(k), ds_row.get(k))} for k, label, dicom, db in DATASET_ITEMS]
    a = plan.get("analysis") or {}
    dataset += [{"label": "인용", "value": a.get("citation"), "dicom": None, "db": None},
                {"label": "에이전트 요약·사실·질문", "value": f"{len(a.get('findings', []))}개 사실, "
                 f"{len(a.get('questions', []))}개 질문", "dicom": None, "db": None},
                {"label": "plan (규칙·라벨 이름·변환 설정)", "value": f"rev {run.meta.get('revision')}",
                 "dicom": None, "db": "ingest_runs.plan_json"},
                {"label": "승인 기록", "value": (run.meta.get("approval") or {}).get("approved_by"),
                 "dicom": None, "db": "ingest_runs.approval_json"}]

    headers_view = []
    for c in active:
        ds = real.get(c["s"]["series_uid"])
        if ds is None or any(h["modality"] == ds.get("Modality") for h in headers_view):
            continue
        headers_view.append({"modality": ds.get("Modality"), "series": c["s"]["series_key"],
                             "tags": [_tag_row(el) for el in ds if el.keyword != "PixelData"]})
    pilot_m = run.load(MANIFESTS["pilot"])
    dest = {
        "local": {"subjects": (plan.get("pilot") or {}).get("subjects", []),
                  "series": len(pilot_uids),
                  "instances": len(pilot_m["instances"]) if pilot_m else sum(
                      c["s"].get("instance_count") or 0 for c in active if c["pilot"]),
                  "frozen": bool(pilot_m)},
        "central": {"series": len(active), "instances": sum(c["s"].get("instance_count") or 0 for c in active),
                    "subjects": len({c["s"]["subject_key"] for c in active})},
        "db": {t: len(v) for t, v in records.items()},
    }
    unknown = sorted({t["keyword"] for h in headers_view for t in h["tags"] if t["source"] == "?"})
    unreadable = sorted(rel for rel, h in headers.items() if "error" in (h.get("header") or {}))
    return {"ready": True, "plan": plan, "rows": rows, "columns": columns_meta(), "flow": flow,
            "dataset": dataset, "headers": headers_view, "dest": dest, "records": records,
            "unknown_tags": unknown, "unreadable": unreadable, "excluded": sum(1 for c in ctxs if c["s"].get("excluded"))}


def _tag_row(el) -> dict:
    if el.VR == "SQ":
        value = f"시퀀스 {len(el.value)}개 항목"
        if el.keyword == "StructureSetROISequence":
            value += ": " + ", ".join(str(i.ROIName) for i in el.value)
    else:
        value = str(el.value) if el.value not in (None, "") else ""
        value = value if len(value) <= 300 else value[:300] + " …"
    kind, why = TAG_SOURCE.get(el.keyword, ("?", "출처 미정"))
    return {"tag": f"({el.tag.group:04X},{el.tag.element:04X})", "keyword": el.keyword, "vr": el.VR,
            "value": value, "source": kind, "why": why}


def columns_meta() -> list[dict]:
    return [{"key": c.key, "label": c.label, "group": c.group, "group_label": GROUPS[c.group],
             "dicom": c.dicom, "db": c.db, "note": c.note} for c in COLUMNS]
