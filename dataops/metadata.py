"""Per-series metadata in one fixed schema for every dataset (`FIELDS`): what was extracted from the source,
what the converters write into the DICOM headers (pilot to the local SCP, Central after approval) and what is
registered in the management DB. Every claimed destination is checked against the real DB-row projection
(`records.build_records`) and, once the pilot is frozen, against the real DICOM headers, so the schema cannot
drift from the code. Source headers are read once into a cache in the run folder.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import nibabel as nib
import numpy as np
import pydicom

from . import deid
from .legacy import n2d
from .local_send import journals
from .prepare import MANIFESTS, image_comments, pilot_series
from .records import build_records, subject_uuid
from .rtstruct import RTSTRUCT_SOP_CLASS, RTSTRUCT_VERSION
from .runs import Run
from .util import now_iso, write_json

HEADERS_FILE = "nifti_headers.json"
NIFTI_FIELDS = ["dim", "pixdim", "datatype", "bitpix", "scl_slope", "scl_inter", "cal_min", "cal_max",
                "qform_code", "sform_code", "quatern_b", "quatern_c", "quatern_d", "qoffset_x", "qoffset_y",
                "qoffset_z", "srow_x", "srow_y", "srow_z", "xyzt_units", "descrip", "aux_file", "intent_code",
                "intent_name", "intent_p1", "intent_p2", "intent_p3", "dim_info", "slice_code", "slice_start",
                "slice_end", "slice_duration", "toffset", "vox_offset", "magic"]


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


KIND_LABEL = {"nifti": "영상", "rtstruct": "라벨", "dicom": "영상(DICOM)"}
FORMAT = {"nifti": "NIfTI", "rtstruct": "NIfTI 라벨", "dicom": "DICOM"}


# ---------------------------------------------------------------------- Local SCP: the DICOM tags per series
@dataclass
class Tag:
    """A series-level DICOM value as it lands in the files sent to the local SCP (and later Central):
    `expect` predicts it from the plan before conversion, `read` takes it from a real converted file."""
    key: str
    tag: str
    desc: str
    expect: Callable[[dict], Any]
    read: Callable[[Any, dict], Any] | None = None
    kinds: tuple = ("nifti", "rtstruct", "dicom")


def _kw(keyword):
    return lambda ds, c: ds.get(keyword)


def _conv_or_src(conv_key, meta_key=None):
    """Converted series: the plan's conversion value; DICOM pass-through: the source tag (sent unchanged)."""
    def f(c):
        if c["s"]["kind"] == "dicom":
            return (c["study_meta"] if meta_key == "StudyDescription" else c["s"].get("metadata") or {}).get(
                meta_key) if meta_key else None
        return c["conv"].get(conv_key)
    return f


def _geo(i, read):
    def f(c):
        s = c["s"]
        if s["kind"] == "dicom":
            return (s.get("metadata") or {}).get(read)
        v = (s.get("metadata") or {}).get("spacing_mm" if read in ("PixelSpacing", "SliceThickness") else "shape_xyz")
        if v is None:
            return None
        return [round(float(v[1]), 6), round(float(v[0]), 6)] if read == "PixelSpacing" else (
            round(float(v[i]), 6) if read == "SliceThickness" else int(v[i]))
    return f


def _sop_class(c):
    s = c["s"]
    if s["kind"] == "dicom":
        return ", ".join(s.get("sop_class_uids") or []) or None
    if s["kind"] == "rtstruct":
        return RTSTRUCT_SOP_CLASS
    cls = n2d.SOP_CLASS_BY_MODALITY.get(s.get("modality"))
    return str(cls) if cls else None


def _comments(c):
    s = c["s"]
    if s["kind"] != "nifti":
        return None
    rel = s["sources"][0]
    if c["adapter"] == "agent-spec":
        text = image_comments(c["plan"], s)
    else:
        text = (f"Local review conversion of NIfTI '{rel}'. Patient/study values are local placeholders, not source "
                f"metadata. Modality {s['modality']} from: {s['modality_basis']}.")
    return n2d._clip(text, 10240)


def _series_desc(c):
    d = c["s"].get("description")
    if d is None:
        return None
    return n2d._clip(d) if c["s"]["kind"] == "rtstruct" else (d[:64] if c["s"]["kind"] == "nifti" else d)


def _floats(v):
    return None if v is None else [round(float(x), 6) for x in v]


def _for_uid(ds, c):
    if ds.get("FrameOfReferenceUID"):
        return ds.FrameOfReferenceUID
    seq = ds.get("ReferencedFrameOfReferenceSequence")
    return seq[0].FrameOfReferenceUID if seq else None


def _roi_names(ds, c):
    seq = ds.get("StructureSetROISequence")
    return [str(i.ROIName) for i in seq] if seq else None


TAGS = [
    Tag("PatientID", "(0010,0020)", "접두어_대상자 키", lambda c: c["s"].get("patient_id"), _kw("PatientID")),
    Tag("PatientName", "(0010,0010)", "접두어^대상자 키", _conv_or_src("patient_name"),
        lambda ds, c: str(ds.get("PatientName", "")) or None),
    Tag("StudyInstanceUID", "(0020,000D)", "dataset UUID + 대상자로 결정적 생성", lambda c: c["s"].get("study_uid"),
        _kw("StudyInstanceUID")),
    Tag("StudyDescription", "(0008,1030)", "데이터셋 이름 (64자 제한)",
        _conv_or_src("study_description", "StudyDescription"), _kw("StudyDescription")),
    Tag("SeriesInstanceUID", "(0020,000E)", "원본 경로로 결정적 생성", lambda c: c["s"].get("series_uid"),
        _kw("SeriesInstanceUID")),
    Tag("SeriesNumber", "(0020,0011)", "대상자 안의 순번 (라벨 1000+)",
        lambda c: int(c["s"]["series_number"]) if str(c["s"].get("series_number") or "").lstrip("-").isdigit()
        else None, lambda ds, c: int(ds.SeriesNumber) if ds.get("SeriesNumber") is not None else None),
    Tag("SeriesDescription", "(0008,103E)", "대상자 키 / '… labels' (64자 제한)", _series_desc,
        _kw("SeriesDescription")),
    Tag("Modality", "(0008,0060)", "에이전트 규칙으로 정한 modality", lambda c: c["s"].get("modality"), _kw("Modality")),
    Tag("SOPClassUID", "(0008,0016)", "CT/MR Image Storage, RT Structure Set Storage", _sop_class,
        lambda ds, c: ds.get("SOPClassUID")),
    Tag("FrameOfReferenceUID", "(0020,0052)", "영상과 라벨이 공유하는 좌표계 UID",
        lambda c: c["conv"].get("frame_of_reference_uid") if c["s"]["kind"] != "dicom" else None, _for_uid),
    Tag("NumberOfInstances", "", "시리즈의 DICOM 파일 수 (영상 장 수, 라벨 1)", lambda c: c["s"].get("instance_count"),
        lambda ds, c: c["frozen_n"]),
    Tag("Rows", "(0028,0010)", "영상 행 수 (원본 y 크기)", _geo(1, "Rows"), lambda ds, c: ds.get("Rows"),
        kinds=("nifti", "dicom")),
    Tag("Columns", "(0028,0011)", "영상 열 수 (원본 x 크기)", _geo(0, "Columns"), lambda ds, c: ds.get("Columns"),
        kinds=("nifti", "dicom")),
    Tag("PixelSpacing", "(0028,0030)", "행\\열 간격 mm (원본 y, x 간격)", _geo(None, "PixelSpacing"),
        lambda ds, c: _floats(ds.get("PixelSpacing")), kinds=("nifti", "dicom")),
    Tag("SliceThickness", "(0018,0050)", "z 간격 mm", _geo(2, "SliceThickness"),
        lambda ds, c: round(float(ds.SliceThickness), 6) if ds.get("SliceThickness") not in (None, "") else None,
        kinds=("nifti", "dicom")),
    Tag("RescaleType", "(0028,1054)", "CT는 HU, 그 외 US",
        lambda c: "HU" if c["s"].get("modality") == "CT" else "US", _kw("RescaleType"), kinds=("nifti",)),
    Tag("ImageComments", "(0020,4000)", "원본 경로 · 데이터셋 이름 · modality 근거", _comments, _kw("ImageComments"),
        kinds=("nifti",)),
    Tag("SoftwareVersions", "(0018,1020)", "변환기 버전",
        lambda c: n2d.CONVERTER_VERSION if c["s"]["kind"] == "nifti" else RTSTRUCT_VERSION,
        lambda ds, c: str(ds.get("SoftwareVersions", "")) or None, kinds=("nifti", "rtstruct")),
    Tag("ReferencedSeriesInstanceUID", "(0020,000E) in (3006,0010)", "라벨이 가리키는 영상 시리즈",
        lambda c: (c["image"] or {}).get("series_uid"), lambda ds, c: _rs_ref_series(ds), kinds=("rtstruct",)),
    Tag("ROIName", "(3006,0026) in (3006,0020)", "라벨 값 → ROI 이름 (실제 파일에는 라벨 볼륨에 있는 값만)",
        lambda c: [v for _, v in sorted((c["s"].get("conversion") or {}).get("label_names", {}).items(),
                                          key=lambda kv: int(kv[0]))] or None,
        _roi_names, kinds=("rtstruct",)),
]
TAG_KEYS = [t.key for t in TAGS]
UNCHECKED = {"ROIName"}            # the file holds only the values present in the label volume
DICOM_LEAD = [("subject_key", "대상자 키"), ("split", "분할"), ("kind", "종류"), ("pilot", "파일럿"),
              ("local_status", "Local SCP"), ("origin", "값 출처")]


# ---------------------------------------------------------------------- the fixed source schema (section 8)
@dataclass
class Field:
    key: str
    label: str
    desc: str
    value: Callable[[dict], Any]
    kinds: tuple = ("nifti", "rtstruct", "dicom")
    dicom: str = ""                                   # TAGS key(s) it lands in ("" = not written)
    db: str = ""                                      # table.column it is registered in ("" = not stored)
    db_val: Callable[[dict, dict], Any] | None = None  # read it back from the DB-row projection


def _m(ctx, k):
    return (ctx["s"].get("metadata") or {}).get(k)


def _ser(idx, s):
    return idx["series"].get(s["series_uid"]) or {}


def _ser_meta(k):
    return lambda i, s: (_ser(i, s).get("metadata_json") or {}).get(k)


def _rs_ref_series(ds):
    try:
        return str(ds.ReferencedFrameOfReferenceSequence[0].RTReferencedStudySequence[0]
                   .RTReferencedSeriesSequence[0].SeriesInstanceUID)
    except (AttributeError, IndexError):
        return None


SHOWN = {"dim", "pixdim"}          # NIfTI fields already shown as size / spacing


def _extra(ctx) -> str | None:
    """Every other source metadata value as `key=value; ...` (NIfTI header fields that are not 0/empty, or the
    DICOM series tags of a pass-through series)."""
    s = ctx["s"]
    if s["kind"] == "dicom":
        items = (s.get("metadata") or {}).items()
    else:
        h = ctx["hdr"] or {}
        if "error" in h:
            return f"헤더 읽기 실패: {h['error'].split(':')[0]}"
        items = [(k, v) for k, v in h.items() if k not in SHOWN and v not in (None, "", 0, 0.0)
                 and not (isinstance(v, list) and not any(v))]
    out = "; ".join(f"{k}={' '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in items)
    return out or None


FIELDS = [
    Field("subject_key", "대상자 키", "원본 파일명에서 뽑은 대상자 식별자", lambda c: c["s"]["subject_key"],
          dicom="PatientID, PatientName", db="subjects.source_subject_key",
          db_val=lambda i, s: (i["subjects"].get(deid.subject(i["plan"], s["subject_key"])) or {}).get(
              "source_subject_key")),
    Field("split", "분할", "train / validation / test / other / none", lambda c: c["s"].get("split"),
          db="series.metadata_json.split", db_val=_ser_meta("split")),
    Field("kind", "종류", "영상 / 라벨 / 영상(DICOM)", lambda c: KIND_LABEL.get(c["s"]["kind"]),
          db="series.source_kind", db_val=lambda i, s: KIND_LABEL.get(_ser(i, s).get("source_kind"))),
    Field("modality", "Modality", "CT / MR / RTSTRUCT", lambda c: c["s"].get("modality"),
          dicom="Modality", db="series.modality", db_val=lambda i, s: _ser(i, s).get("modality")),
    Field("modality_basis", "Modality 근거", "modality를 정한 규칙과 근거 문서", lambda c: c["s"].get("modality_basis"),
          dicom="ImageComments", db="series.modality_basis", db_val=lambda i, s: _ser(i, s).get("modality_basis")),
    Field("source", "원본 파일", "데이터셋 폴더 기준 상대 경로", lambda c: (c["s"].get("sources") or [None])[0],
          dicom="ImageComments", db="instances.source_relative_path"),
    Field("format", "원본 형식", "NIfTI / NIfTI 라벨 / DICOM", lambda c: FORMAT.get(c["s"]["kind"]),
          db="series.source_kind"),
    Field("size_mb", "원본 크기(MB)", "원본 파일 크기",
          lambda c: round(c["size"] / 1e6, 1) if c["size"] is not None else None),
    Field("shape", "크기 x·y·z", "원본 격자 크기(열·행·장)", lambda c: _m(c, "shape_xyz"), kinds=("nifti",),
          dicom="Columns, Rows, NumberOfInstances", db="series.metadata_json.shape_xyz", db_val=_ser_meta("shape_xyz")),
    Field("spacing", "간격 mm", "x·y·z 화소 간격", lambda c: _m(c, "spacing_mm"), kinds=("nifti",),
          dicom="PixelSpacing, SliceThickness", db="series.metadata_json.spacing_mm", db_val=_ser_meta("spacing_mm")),
    Field("axcodes", "축 방향", "원본 축 방향(RAS 기준)", lambda c: _m(c, "axcodes"), kinds=("nifti",),
          dicom="ImageOrientationPatient (슬라이스별 계산)", db="series.metadata_json.axcodes",
          db_val=_ser_meta("axcodes")),
    Field("dtype", "자료형", "원본 화소 자료형", lambda c: _m(c, "dtype"), kinds=("nifti",),
          dicom="PixelRepresentation (계산)", db="series.metadata_json.dtype", db_val=_ser_meta("dtype")),
    Field("label_of", "라벨 대상 영상", "라벨이 붙는 영상의 원본 파일", lambda c: _m(c, "label_of"), kinds=("rtstruct",),
          dicom="ReferencedSeriesInstanceUID", db="series.metadata_json.label_of", db_val=_ser_meta("label_of")),
    Field("rois", "ROI", "라벨 값과 이름", lambda c: c["roi_names"], kinds=("rtstruct",),
          dicom="ROIName", db="ingest_runs.plan_json"),
    Field("source_extra", "원본 기타 메타데이터", "그 밖의 원본 헤더 값 전체 (0·빈 값 제외)", _extra),
    Field("instance_count", "인스턴스 수", "DICOM 파일 수 (영상 장 수, 라벨 1)", lambda c: c["s"].get("instance_count"),
          dicom="NumberOfInstances", db="series.instance_count",
          db_val=lambda i, s: _ser(i, s).get("instance_count")),
    Field("included", "포함", "Y: 전송·등록 대상, N: 제외", lambda c: not c["s"].get("excluded")),
    Field("pilot", "파일럿", "Y: 파일럿 대상자(Local SCP로 전송)", lambda c: c["pilot"]),
    Field("local_status", "Local SCP", "파일럿 전송 결과 (수신/전체)", lambda c: c["status"]["local"]),
    Field("central_status", "Central", "Central 전송 결과 (수신/전체)", lambda c: c["status"]["central"]),
    Field("db_status", "DB", "관리 DB 등록 여부", lambda c: c["status"]["db"]),
]
FIELD_KEYS = [f.key for f in FIELDS]


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


def series_delivery(run: Run) -> dict:
    """{series_uid: {scope: Counter}} over the pilot and full manifests: instances, unsent, "<kind>:<status>"."""
    jlist = journals(run)
    out = {}
    for scope, name in MANIFESTS.items():
        m = run.load(name)
        if not m:
            continue
        status_of = defaultdict(list)
        for j in jlist:
            if j.get("scope", "full") == scope and j["manifest_sha256"] == m["manifest_sha256"]:
                for sop, rec in j["instances"].items():
                    status_of[sop].append((j["target"].get("kind", "local"), rec["status"]))
        for inst in m["instances"]:
            c = out.setdefault(inst["series_uid"], {}).setdefault(scope, Counter())
            c["instances"] += 1
            sts = status_of.get(inst["sop_uid"], [])
            if not sts:
                c["unsent"] += 1
            for kind, st in sts:
                c[f"{kind}:{st}"] += 1
    return out


def _status(counts: Counter | None, kind: str) -> str | None:
    """'수신 90/90' / '일부 80/90' / '실패 0/90' / '미전송' — the same wording for every target."""
    if not counts:
        return None
    total, ok = counts["instances"], counts[f"{kind}:accepted"] + counts[f"{kind}:warning"]
    if not any(k.startswith(kind + ":") for k in counts):
        return "미전송"
    return f"{'수신' if ok == total else ('실패' if ok == 0 else '일부')} {ok}/{total}"


def _contexts(plan: dict, headers: dict, delivery: dict, registered: bool) -> list[dict]:
    by_key = {s["series_key"]: s for s in plan.get("series", [])}
    pilot = {s["series_key"] for s in pilot_series(plan)} if plan.get("series") else set()
    duuid = plan["dataset"]["dataset_uuid"]
    studies = {st["study_uid"]: st.get("metadata") or {} for st in plan.get("studies", [])}
    out = []
    for s in plan.get("series", []):
        image = by_key.get(s.get("reference")) if s["kind"] == "rtstruct" else None
        src = (s.get("sources") or [None])[0]
        h = headers.get(src) or {}
        conv = dict((image or s).get("conversion") or {})
        names = (s.get("conversion") or {}).get("label_names")
        d = delivery.get(s.get("series_uid"), {})
        status = {"local": _status(d.get("pilot"), "local"), "central": _status(d.get("full"), "central"),
                  "db": ("등록" if registered else "미등록") if not s.get("excluded") else None}
        out.append({"s": s, "image": image, "hdr": h.get("header"), "size": h.get("size"), "status": status,
                    "conv": conv, "pilot": s["series_key"] in pilot, "adapter": plan.get("adapter"), "plan": plan,
                    "dataset_name": plan["dataset"].get("name"), "study_meta": studies.get(s.get("study_uid"), {}),
                    "roi_names": [f"{k} {v}" for k, v in sorted(names.items(), key=lambda kv: int(kv[0]))]
                    if names else None,
                    "subject_uuid": subject_uuid(duuid, deid.subject(plan, s["subject_key"]))})
    return out


DEID_FIELDS = {"subject_key": deid.subject, "label_of": deid.path}   # how a field's value is stored when on


def _index(records: dict, plan: dict) -> dict:
    return {"plan": plan, "subjects": {r["source_subject_key"]: r for r in records["subjects"]},
            "studies": {r["study_uid"]: r for r in records["studies"]},
            "series": {r["series_uid"]: r for r in records["series"]}}


REAL_FILE = "dicom_tags.json"


def _json(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (list, tuple, pydicom.multival.MultiValue)):
        return [_json(x) for x in v]
    if isinstance(v, (pydicom.valuerep.DSfloat, pydicom.valuerep.DSdecimal)):
        return round(float(v), 6)
    if isinstance(v, pydicom.valuerep.IS):
        return int(v)
    return str(v)


def real_tags(run: Run, uids: set) -> dict:
    """{series_uid: {"scope", "path", "values": {TAGS key: value}}} read from the first file of every frozen
    series (pilot manifest first, then the full one); cached in the run folder per manifest hash."""
    manifests = [(scope, run.load(name)) for scope, name in MANIFESTS.items()]
    manifests = [(scope, m) for scope, m in manifests if m]
    stamp = {"manifests": [m["manifest_sha256"] for _, m in manifests], "tags": TAG_KEYS}
    cache = run.load(REAL_FILE) or {}
    if cache.get("stamp") == stamp:
        return {u: v for u, v in cache["series"].items() if u in uids}
    out = {}
    for scope, m in manifests:
        counts = Counter(i["series_uid"] for i in m["instances"])
        first = {}
        for inst in m["instances"]:
            u = inst["series_uid"]
            if u not in first or (inst.get("instance_number") or 0) < (first[u].get("instance_number") or 0):
                first[u] = inst
        for u, inst in first.items():
            if u in out:
                continue
            try:
                ds = pydicom.dcmread(run.dir / inst["path"], stop_before_pixels=True)
            except Exception:  # noqa: BLE001 - the series then shows as not checked
                continue
            ctx = {"frozen_n": counts[u]}
            values = {}
            for t in TAGS:
                try:
                    values[t.key] = _json(t.read(ds, ctx))
                except (AttributeError, IndexError, KeyError, ValueError, TypeError) as e:
                    values[t.key] = f"읽기 실패: {e}"
            out[u] = {"scope": scope, "path": inst["path"], "values": values}
    write_json(run.path(REAL_FILE), {"stamp": stamp, "series": out})
    return {u: v for u, v in out.items() if u in uids}


def build(run: Run, refresh: bool = False) -> dict:
    """The series tables of the standard report: the DICOM values per series as they go to the local SCP
    (checked against the converted files once frozen), the source values (checked against the DB-row
    projection), the real pilot headers and the DB rows registration would write."""
    plan = run.load("plan.json") or {}
    if not plan.get("series"):
        return {"ready": False}
    headers = nifti_headers(run, plan, refresh)
    ctxs = _contexts(plan, headers, series_delivery(run), bool(run.load("registration.json")))
    records = build_records(run, preview=True)
    idx = _index(records, plan)
    active = [c for c in ctxs if not c["s"].get("excluded")]
    real = real_tags(run, {c["s"]["series_uid"] for c in active})

    rows, checks = [], {f.key: {"ok": 0, "bad": []} for f in FIELDS}
    for c in ctxs:
        s = c["s"]
        row = {"_uid": s["series_uid"], "_kind": s["kind"]}
        for f in FIELDS:
            v = f.value(c) if s["kind"] in f.kinds else None
            row[f.key] = v
            if s.get("excluded") or s["kind"] not in f.kinds or not f.db_val:
                continue
            got = f.db_val(idx, s)
            if _same(DEID_FIELDS[f.key](plan, v) if f.key in DEID_FIELDS else v, got):
                checks[f.key]["ok"] += 1
            elif len(checks[f.key]["bad"]) < 5:
                checks[f.key]["bad"].append({"series": s["series_key"], "expected": v, "db": got})
        rows.append(row)
    fields = [{"no": no, "key": f.key, "label": f.label, "desc": f.desc, "dicom": f.dicom, "db": f.db,
               "db_check": _verdict(checks[f.key]["ok"], checks[f.key]["bad"], "해당 없음") if f.db_val else None,
               "bad": checks[f.key]["bad"]} for no, f in enumerate(FIELDS, start=1)]

    dicom_rows, tag_checks = [], {t.key: {"ok": 0, "bad": []} for t in TAGS}
    for c in active:
        s = c["s"]
        got = real.get(s["series_uid"])
        row = {"_uid": s["series_uid"], "_kind": s["kind"], "subject_key": s["subject_key"], "split": s.get("split"),
               "kind": KIND_LABEL.get(s["kind"]), "pilot": c["pilot"], "local_status": c["status"]["local"],
               "origin": ("실제 파일 (" + ("파일럿" if got["scope"] == "pilot" else "전체 변환") + ")") if got else
               ("원본 DICOM 그대로 (알려진 값만)" if s["kind"] == "dicom" else "예정 (변환 전, plan 기준)")}
        bad = []
        for t in TAGS:
            if s["kind"] not in t.kinds:
                row[t.key] = None
                continue
            want = t.expect(c)
            if got is None:
                row[t.key] = want
                continue
            have = got["values"].get(t.key)
            row[t.key] = have
            if t.key in UNCHECKED or want is None:
                continue
            if _same(want, have):
                tag_checks[t.key]["ok"] += 1
            else:
                bad.append(t.key)
                if len(tag_checks[t.key]["bad"]) < 5:
                    tag_checks[t.key]["bad"].append({"series": s["series_key"], "expected": want, "dicom": have})
        row["check"] = ("불일치: " + ", ".join(bad)) if bad else ("일치" if got else None)
        dicom_rows.append(row)
    tags = [{"no": no, "key": t.key, "tag": t.tag, "desc": t.desc,
             "source": TAG_SOURCE.get(t.key, ("계산", ""))[0],
             "check": "확인 안 함" if t.key in UNCHECKED else
             _verdict(tag_checks[t.key]["ok"], tag_checks[t.key]["bad"], "해당 없음" if real else "파일럿 후"),
             "bad": tag_checks[t.key]["bad"]} for no, t in enumerate(TAGS, start=1)]

    headers_view = []
    for c in active:
        r = real.get(c["s"]["series_uid"])
        mod = c["s"].get("modality")
        if r is None or any(h["modality"] == mod for h in headers_view):
            continue
        try:
            ds = pydicom.dcmread(run.dir / r["path"], stop_before_pixels=True)
        except Exception:  # noqa: BLE001
            continue
        headers_view.append({"modality": ds.get("Modality"), "series": c["s"]["series_key"],
                             "tags": [_tag_row(el) for el in ds if el.keyword != "PixelData"]})
    return {"ready": True, "plan": plan, "rows": rows, "fields": fields, "headers": headers_view,
            "dicom": {"tags": tags, "rows": dicom_rows}, "records": records,
            "unknown_tags": sorted({t["keyword"] for h in headers_view for t in h["tags"] if t["source"] == "?"}),
            "unreadable": sorted(rel for rel, h in headers.items() if "error" in (h.get("header") or {}))}


def _verdict(ok: int, bad: list, none: str) -> str:
    """'일치 960' / '불일치 2' / `none` when nothing was compared ('파일럿 후', '해당 없음')."""
    if bad:
        return f"불일치 {len(bad)}"
    return f"일치 {ok}" if ok else none


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
