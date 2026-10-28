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

from .local_send import journals
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


# ---------------------------------------------------------------------- the fixed series schema
@dataclass
class Field:
    key: str
    label: str
    desc: str
    value: Callable[[dict], Any]
    kinds: tuple = ("nifti", "rtstruct", "dicom")
    dicom: str = ""                                   # where it lands in the DICOM header ("" = not written)
    dicom_val: Callable[[Any], Any] | None = None     # read it back from a real header
    dicom_expect: Callable[[dict], Any] | None = None
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


KIND_LABEL = {"nifti": "영상", "rtstruct": "라벨", "dicom": "영상(DICOM)"}
FORMAT = {"nifti": "NIfTI", "rtstruct": "NIfTI 라벨", "dicom": "DICOM"}

FIELDS = [
    Field("subject_key", "대상자 키", "원본 파일명에서 뽑은 대상자 식별자", lambda c: c["s"]["subject_key"],
          dicom="PatientID·PatientName 일부", db="subjects.source_subject_key",
          db_val=lambda i, s: (i["subjects"].get(s["subject_key"]) or {}).get("source_subject_key")),
    Field("split", "분할", "train / validation / test / other / none", lambda c: c["s"].get("split"),
          db="series.metadata_json.split", db_val=_ser_meta("split")),
    Field("kind", "종류", "영상 / 라벨 / 영상(DICOM)", lambda c: KIND_LABEL.get(c["s"]["kind"]),
          db="series.source_kind", db_val=lambda i, s: KIND_LABEL.get(_ser(i, s).get("source_kind"))),
    Field("modality", "Modality", "CT / MR / RTSTRUCT", lambda c: c["s"].get("modality"),
          dicom="Modality (0008,0060)", dicom_val=lambda ds: ds.get("Modality"),
          db="series.modality", db_val=lambda i, s: _ser(i, s).get("modality")),
    Field("modality_basis", "Modality 근거", "modality를 정한 규칙과 근거 문서", lambda c: c["s"].get("modality_basis"),
          dicom="ImageComments (0020,4000) 일부, 영상만", db="series.modality_basis",
          db_val=lambda i, s: _ser(i, s).get("modality_basis")),
    Field("source", "원본 파일", "데이터셋 폴더 기준 상대 경로", lambda c: (c["s"].get("sources") or [None])[0],
          dicom="ImageComments (0020,4000) 일부, 영상만", db="instances.source_relative_path"),
    Field("format", "원본 형식", "NIfTI / NIfTI 라벨 / DICOM", lambda c: FORMAT.get(c["s"]["kind"])),
    Field("size_mb", "원본 크기(MB)", "원본 파일 크기",
          lambda c: round(c["size"] / 1e6, 1) if c["size"] is not None else None),
    Field("shape", "크기 x·y·z", "변환 기준 격자 크기(열·행·장)", lambda c: _m(c, "shape_xyz"), kinds=("nifti",),
          dicom="Columns·Rows (0028,0011/0010), 장 수", dicom_val=lambda ds: [int(ds.Columns), int(ds.Rows)],
          dicom_expect=lambda c: (_m(c, "shape_xyz") or [None, None])[:2],
          db="series.metadata_json.shape_xyz", db_val=_ser_meta("shape_xyz")),
    Field("spacing", "간격 mm", "x·y·z 화소 간격", lambda c: _m(c, "spacing_mm"), kinds=("nifti",),
          dicom="PixelSpacing (0028,0030)·SliceThickness (0018,0050)",
          dicom_val=lambda ds: [float(ds.PixelSpacing[1]), float(ds.PixelSpacing[0]), float(ds.SliceThickness)],
          db="series.metadata_json.spacing_mm", db_val=_ser_meta("spacing_mm")),
    Field("axcodes", "축 방향", "원본 축 방향(RAS 기준)", lambda c: _m(c, "axcodes"), kinds=("nifti",),
          dicom="ImageOrientationPatient (0020,0037) 계산", db="series.metadata_json.axcodes",
          db_val=_ser_meta("axcodes")),
    Field("dtype", "자료형", "원본 화소 자료형", lambda c: _m(c, "dtype"), kinds=("nifti",),
          dicom="BitsStored·PixelRepresentation 계산", db="series.metadata_json.dtype", db_val=_ser_meta("dtype")),
    Field("label_of", "라벨 대상 영상", "라벨이 붙는 영상의 원본 파일", lambda c: _m(c, "label_of"), kinds=("rtstruct",),
          dicom="ReferencedFrameOfReferenceSequence (3006,0010)", dicom_val=_rs_ref_series,
          dicom_expect=lambda c: (c["image"] or {}).get("series_uid"),
          db="series.metadata_json.label_of", db_val=_ser_meta("label_of")),
    Field("rois", "ROI", "라벨 값과 이름", lambda c: c["roi_names"], kinds=("rtstruct",),
          dicom="StructureSetROISequence (3006,0020), 맵에 있는 값만", db="ingest_runs.plan_json"),
    Field("source_extra", "원본 기타 메타데이터", "그 밖의 원본 헤더 값 전체 (0·빈 값 제외)", _extra),
    Field("patient_id", "PatientID", "접두어_대상자 키", lambda c: c["s"].get("patient_id"),
          dicom="PatientID (0010,0020)", dicom_val=lambda ds: ds.get("PatientID"),
          db="subjects.patient_id", db_val=lambda i, s: (i["subjects"].get(s["subject_key"]) or {}).get("patient_id")),
    Field("patient_name", "PatientName", "접두어^대상자 키", lambda c: c["conv"].get("patient_name"),
          dicom="PatientName (0010,0010)", dicom_val=lambda ds: str(ds.get("PatientName", "")),
          db="ingest_runs.plan_json"),
    Field("study_uid", "Study UID", "대상자별 검사 UID", lambda c: c["s"].get("study_uid"),
          dicom="StudyInstanceUID (0020,000D)", dicom_val=lambda ds: ds.get("StudyInstanceUID"),
          db="studies.study_uid", db_val=lambda i, s: (i["studies"].get(s["study_uid"]) or {}).get("study_uid")),
    Field("study_description", "StudyDescription", "데이터셋 이름 (64자 제한)", lambda c: c["conv"].get("study_description"),
          dicom="StudyDescription (0008,1030)", dicom_val=lambda ds: ds.get("StudyDescription"),
          db="studies.description", db_val=lambda i, s: (i["studies"].get(s["study_uid"]) or {}).get("description")),
    Field("series_uid", "Series UID", "시리즈 UID", lambda c: c["s"].get("series_uid"),
          dicom="SeriesInstanceUID (0020,000E)", dicom_val=lambda ds: ds.get("SeriesInstanceUID"),
          db="series.series_uid", db_val=lambda i, s: _ser(i, s).get("series_uid")),
    Field("series_number", "SeriesNumber", "대상자 안의 순번 (라벨 1000+)", lambda c: c["s"].get("series_number"),
          dicom="SeriesNumber (0020,0011)", dicom_val=lambda ds: int(ds.SeriesNumber),
          db="series.series_number", db_val=lambda i, s: _ser(i, s).get("series_number")),
    Field("series_description", "SeriesDescription", "대상자 키 / '… labels'",
          lambda c: (c["s"].get("description") or "")[:64] or None,
          dicom="SeriesDescription (0008,103E)", dicom_val=lambda ds: ds.get("SeriesDescription"),
          db="series.description", db_val=lambda i, s: (_ser(i, s).get("description") or "")[:64] or None),
    Field("frame_of_reference", "FrameOfReference UID", "영상과 라벨이 공유하는 좌표계 UID",
          lambda c: (c["image"] or c["s"]).get("conversion", {}).get("frame_of_reference_uid"),
          kinds=("nifti", "rtstruct"), dicom="FrameOfReferenceUID (0020,0052)",
          dicom_val=lambda ds: ds.get("FrameOfReferenceUID") or
          ds.ReferencedFrameOfReferenceSequence[0].FrameOfReferenceUID, db="ingest_runs.plan_json"),
    Field("instance_count", "인스턴스 수", "DICOM 파일 수 (영상 장 수, 라벨 1)", lambda c: c["s"].get("instance_count"),
          dicom="SOP Instance 개수", db="series.instance_count", db_val=lambda i, s: _ser(i, s).get("instance_count")),
    Field("subject_uuid", "subject UUID", "DB 대상자 키 (dataset UUID + 대상자 키)", lambda c: c["subject_uuid"],
          db="subjects.subject_uuid",
          db_val=lambda i, s: (i["subjects"].get(s["subject_key"]) or {}).get("subject_uuid")),
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
    """Series rows in the fixed schema, the field definitions with their checks, the real pilot DICOM headers
    and the DB rows registration would write."""
    plan = run.load("plan.json") or {}
    if not plan.get("series"):
        return {"ready": False}
    headers = nifti_headers(run, plan, refresh)
    ctxs = _contexts(plan, headers, series_delivery(run), bool(run.load("registration.json")))
    records = build_records(run, preview=True)
    idx = _index(records)
    active = [c for c in ctxs if not c["s"].get("excluded")]
    real = _frozen_headers(run, {c["s"]["series_uid"] for c in active if c["pilot"]})

    rows, checks = [], {f.key: {"db_ok": 0, "db_bad": [], "dicom_ok": 0, "dicom_bad": []} for f in FIELDS}
    for c in ctxs:
        s = c["s"]
        row = {"_uid": s["series_uid"], "_kind": s["kind"]}
        for f in FIELDS:
            v = f.value(c) if s["kind"] in f.kinds else None
            row[f.key] = v
            if s.get("excluded") or s["kind"] not in f.kinds:
                continue
            ck = checks[f.key]
            if f.db_val:
                got = f.db_val(idx, s)
                if _same(v, got):
                    ck["db_ok"] += 1
                elif len(ck["db_bad"]) < 5:
                    ck["db_bad"].append({"series": s["series_key"], "expected": v, "db": got})
            ds = real.get(s["series_uid"])
            if f.dicom_val and ds is not None:
                want = f.dicom_expect(c) if f.dicom_expect else v
                try:
                    got = f.dicom_val(ds)
                except (AttributeError, IndexError, KeyError, ValueError) as e:
                    got = f"읽기 실패: {e}"
                if _same(want, got):
                    ck["dicom_ok"] += 1
                elif len(ck["dicom_bad"]) < 5:
                    ck["dicom_bad"].append({"series": s["series_key"], "expected": want, "dicom": str(got)})
        rows.append(row)

    fields = []
    for no, f in enumerate(FIELDS, start=1):
        ck = checks[f.key]
        fields.append({"no": no, "key": f.key, "label": f.label, "desc": f.desc, "dicom": f.dicom, "db": f.db,
                       "dicom_check": _verdict(ck["dicom_ok"], ck["dicom_bad"], "해당 없음" if real else "파일럿 후")
                       if f.dicom_val else None,
                       "db_check": _verdict(ck["db_ok"], ck["db_bad"], "해당 없음") if f.db_val else None,
                       "bad": ck["db_bad"] + ck["dicom_bad"]})
    headers_view = []
    for c in active:
        ds = real.get(c["s"]["series_uid"])
        if ds is None or any(h["modality"] == ds.get("Modality") for h in headers_view):
            continue
        headers_view.append({"modality": ds.get("Modality"), "series": c["s"]["series_key"],
                             "tags": [_tag_row(el) for el in ds if el.keyword != "PixelData"]})
    return {"ready": True, "plan": plan, "rows": rows, "fields": fields, "headers": headers_view,
            "records": records,
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
