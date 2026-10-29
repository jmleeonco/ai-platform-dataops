"""Sample view: for each sample series, the real source value (left) next to the value that goes out (right).

"Goes out" is read from the converted sample files themselves (every top-level element of the first DICOM file
the SCPs receive) and from the DB-row projection registration uses (`records`), so the right side is what is
really written, not a description of it. Source values come from the source file (NIfTI header, original DICOM
header) and the plan (subject key, path, modality basis). Values that leave unchanged although they can
identify a person are flagged.
"""
from __future__ import annotations

from pathlib import Path

import pydicom

from .inventory import read_dicom_header
from .metadata import HEADERS_FILE, KIND_LABEL, TAG_SOURCE, read_nifti_header
from .prepare import pilot_series
from .records import build_records, instance_rows
from .runs import Run

CUT = 300
# Attributes that can identify a person or an encounter (DICOM PS3.15 Basic Profile, abridged).
IDENTIFYING = {
    "PatientName", "PatientID", "PatientBirthDate", "PatientBirthTime", "OtherPatientIDs", "OtherPatientNames",
    "OtherPatientIDsSequence", "PatientBirthName", "PatientMotherBirthName", "PatientAddress",
    "PatientTelephoneNumbers", "IssuerOfPatientID", "MedicalRecordLocator", "AccessionNumber", "StudyID",
    "ReferringPhysicianName", "PerformingPhysicianName", "OperatorsName", "PhysiciansOfRecord",
    "NameOfPhysiciansReadingStudy", "RequestingPhysician", "InstitutionName", "InstitutionAddress",
    "InstitutionalDepartmentName", "StationName", "DeviceSerialNumber", "StudyDate", "SeriesDate",
    "AcquisitionDate", "ContentDate", "StudyTime", "SeriesTime", "AcquisitionTime", "ContentTime",
    "AcquisitionDateTime", "InstanceCreationDate", "InstanceCreationTime", "StructureSetDate", "StructureSetTime",
    "PatientAge", "PatientWeight", "PatientSize", "AdmissionID", "RequestAttributesSequence",
}
ACTION = {"고정": "고정값", "UID": "새 UID 생성", "분석": "생성", "데이터셋": "생성", "기하": "계산", "화소": "계산",
          "계산": "계산", "라벨": "계산", "빈 값": "빈 값"}
NIFTI_USED = {"dim", "pixdim", "datatype", "bitpix", "scl_slope", "scl_inter", "qform_code", "sform_code",
              "quatern_b", "quatern_c", "quatern_d", "qoffset_x", "qoffset_y", "qoffset_z", "srow_x", "srow_y",
              "srow_z", "vox_offset", "magic"}


def _text(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, (list, tuple)):
        v = ", ".join("-" if x is None else str(x) for x in v)
    s = str(v)
    return s if len(s) <= CUT else s[:CUT] + " …"


def _el_value(el) -> str | None:
    if el.VR == "SQ":
        items = el.value or []
        if el.keyword == "StructureSetROISequence":
            return f"{len(items)}개: " + ", ".join(str(i.get("ROIName", "")) for i in items)
        if el.keyword == "ReferencedFrameOfReferenceSequence" and items:
            return f"FoR {items[0].get('FrameOfReferenceUID', '')}"
        return f"{len(items)}개 항목"
    if el.VR in ("OB", "OW", "OF", "OD", "UN"):
        return f"(이진 {len(el.value or b'')} bytes)"
    v = el.value
    if v is None:
        return None
    if isinstance(v, pydicom.multival.MultiValue):
        return _text([str(x) for x in v])
    return _text(str(v))


def _elements(ds) -> dict:
    """{keyword: (tag text, value text)} for the top-level elements, without pixels and file meta."""
    out = {}
    for el in ds:
        if el.tag.group == 0x0002 or el.keyword in ("PixelData", ""):
            continue
        out[el.keyword] = (f"({el.tag.group:04X},{el.tag.element:04X})", _el_value(el))
    return out


def _nifti_sources(c: dict) -> dict:
    """keyword -> (where the value comes from, source value) for a series converted from NIfTI."""
    s, h, rel = c["s"], c["hdr"] or {}, c["rel"]
    dim, pix = h.get("dim") or [], h.get("pixdim") or []
    key = ("대상자 키 (파일 이름)", s["subject_key"])
    out = {"PatientID": key, "PatientName": key, "SeriesDescription": key,
           "StudyInstanceUID": ("대상자 키", s["subject_key"]),
           "SeriesInstanceUID": ("원본 경로", rel), "SOPInstanceUID": ("원본 경로 + 장 번호", rel),
           "StudyDescription": ("데이터셋 이름", c["dataset_name"]), "ImageComments": ("원본 경로", rel),
           "Modality": ("Modality 근거", s.get("modality_basis"))}
    if s["kind"] == "rtstruct":
        names = (s.get("conversion") or {}).get("label_names") or {}
        out.update({"FrameOfReferenceUID": ("짝 영상 경로", c["image_rel"]),
                    "ReferencedFrameOfReferenceSequence": ("짝 영상 경로", c["image_rel"]),
                    "StructureSetROISequence": ("라벨 정의 (값 이름)", ", ".join(f"{k} {v}" for k, v in sorted(
                        names.items(), key=lambda kv: int(kv[0])))),
                    "ROIContourSequence": ("라벨 맵 화소", rel), "StructureSetName": key})
        return out
    out.update({"FrameOfReferenceUID": ("원본 경로", rel),
                "Rows": ("NIfTI dim[2]", dim[2] if len(dim) > 2 else None),
                "Columns": ("NIfTI dim[1]", dim[1] if len(dim) > 1 else None),
                "PixelSpacing": ("NIfTI pixdim[1:3]", pix[1:3] or None),
                "SliceThickness": ("NIfTI pixdim[3]", pix[3] if len(pix) > 3 else None),
                "SpacingBetweenSlices": ("NIfTI pixdim[3]", pix[3] if len(pix) > 3 else None),
                "InstanceNumber": ("NIfTI 장 순서", "첫 장"),
                "ImagePositionPatient": ("NIfTI srow (affine)", [h.get("srow_x"), h.get("srow_y"), h.get("srow_z")]),
                "ImageOrientationPatient": ("NIfTI srow (affine)", [h.get("srow_x"), h.get("srow_y"), h.get("srow_z")]),
                "SliceLocation": ("NIfTI srow (affine)", h.get("srow_z")),
                "PixelRepresentation": ("NIfTI datatype / bitpix", f"{h.get('datatype')} / {h.get('bitpix')}"),
                "RescaleSlope": ("NIfTI scl_slope", h.get("scl_slope")),
                "RescaleIntercept": ("NIfTI scl_inter", h.get("scl_inter"))})
    return out


def _row(group, item, src_from, src, out, action, ident=False) -> dict:
    src, out = _text(src), _text(out)
    return {"group": group, "item": item, "from": src_from, "src": src, "out": out, "action": action,
            "changed": src != out, "flag": "식별 정보 그대로" if ident and out and src == out else None}


def _dicom_rows(c: dict, out_ds) -> list[dict]:
    s = c["s"]
    rows = []
    if s["kind"] == "dicom":
        src_ds = c["src_ds"]
        src, out = _elements(src_ds) if src_ds is not None else {}, _elements(out_ds) if out_ds is not None else {}
        for kw in sorted(set(src) | set(out), key=lambda k: (src.get(k) or out.get(k))[0]):
            tag, sv = src.get(kw, (None, None))
            otag, ov = out.get(kw, (None, None))
            action = "그대로" if sv == ov else ("삭제" if kw not in out else "변경")
            rows.append(_row("DICOM", f"{kw} {tag or otag}", "원본 DICOM", sv, ov, action, kw in IDENTIFYING))
        return rows
    src_map = _nifti_sources(c)
    for kw, (tag, ov) in (_elements(out_ds) if out_ds is not None else {}).items():
        origin = TAG_SOURCE.get(kw, ("?", ""))[0]
        src_from, sv = src_map.get(kw, ("—", None))
        action = ACTION.get(origin, "생성") if (sv is None or _text(sv) != ov) else "그대로"
        rows.append(_row("DICOM", f"{kw} {tag}", src_from, sv, ov, action, kw in IDENTIFYING))
    if s["kind"] == "nifti":           # source header values that go nowhere
        for k, v in (c["hdr"] or {}).items():
            if k not in NIFTI_USED and v not in (None, "", 0, 0.0) and not (isinstance(v, list) and not any(v)):
                rows.append(_row("DICOM", "—", f"NIfTI {k}", v, None, "버림"))
    return rows


DB_SOURCE = {
    ("subjects", "source_subject_key"): "대상자 키", ("subjects", "patient_id"): "대상자 키",
    ("subjects", "subject_uuid"): "대상자 키", ("series", "description"): "대상자 키",
    ("instances", "source_relative_path"): "원본 경로", ("instances", "source_sha256"): "원본 파일 내용",
    ("series", "modality"): "Modality 근거", ("series", "modality_basis"): "Modality 근거",
    ("studies", "description"): "데이터셋 이름", ("studies", "study_date"): "원본 StudyDate",
}
# DB columns that keep a source value as is: identifying when the source is an original DICOM export.
DB_KEEPS = {("subjects", "source_subject_key"), ("subjects", "patient_id"), ("studies", "study_date"),
            ("instances", "source_relative_path")}


def _db_rows(c: dict, recs: dict, first_inst: dict | None) -> list[dict]:
    s = c["s"]
    src_val = {"대상자 키": s["subject_key"], "원본 경로": c["rel"], "원본 파일 내용": "(파일 바이트)",
               "Modality 근거": s.get("modality_basis"), "데이터셋 이름": c["dataset_name"],
               "원본 StudyDate": str(c["src_ds"].get("StudyDate", "")) if c.get("src_ds") is not None else None}
    pick = {"subjects": [r for r in recs["subjects"] if r["source_subject_key"] == s["subject_key"]],
            "studies": [r for r in recs["studies"] if r["study_uid"] == s["study_uid"]],
            "series": [r for r in recs["series"] if r["series_uid"] == s["series_uid"]],
            "instances": [first_inst] if first_inst else []}
    rows = []
    for table, found in pick.items():
        for r in found[:1]:
            for col, v in r.items():
                label = DB_SOURCE.get((table, col))
                sv = src_val.get(label) if label else None
                if v in (None, ""):
                    action = "빈 값"
                elif col == "metadata_json":
                    label, action = "원본 헤더 (기하·자료형)", "계산"
                elif label is not None and _text(sv) == _text(v):
                    action = "원본 그대로"
                else:
                    action = "생성"
                rows.append(_row("DB", f"{table}.{col}", label or "—", sv, v, action,
                                 (table, col) in DB_KEEPS and s["kind"] == "dicom"))
    return rows


def sample_view(run: Run) -> dict:
    """{"cases": [{"subject_key", "series": [{"uid", "kind", "label", "frozen", "rows"}]}], "dataset": rows}."""
    plan = run.load("plan.json") or {}
    if not plan.get("series"):
        return {"ready": False}
    root = Path(plan["input_root"])
    pilot = run.load("manifest-pilot.json")
    stale = not pilot or pilot.get("plan_sha256") != run.meta.get("plan_sha256")
    first = {}
    for inst in (pilot or {}).get("instances", []):
        u = inst["series_uid"]
        if u not in first or (inst.get("instance_number") or 0) < (first[u].get("instance_number") or 0):
            first[u] = inst
    recs = build_records(run, preview=True)
    first_rows = {r["series_uid"]: r for r in instance_rows(run, {"instances": list(first.values())})}
    by_key = {s["series_key"]: s for s in plan["series"]}
    cached = (run.load(HEADERS_FILE) or {}).get("files", {})

    def nifti_header(rel):
        hit = (cached.get(rel) or {}).get("header")
        return hit if hit and "error" not in hit else read_nifti_header(root / rel)
    cases = {}
    for s in sorted(pilot_series(plan), key=lambda x: (x["subject_key"], x["kind"] == "rtstruct", x["series_key"])):
        rel = (s.get("sources") or [None])[0]
        image = by_key.get(s.get("reference")) if s["kind"] == "rtstruct" else None
        c = {"s": s, "rel": rel, "dataset_name": plan["dataset"].get("name"),
             "image_rel": ((image or {}).get("sources") or [None])[0],
             "hdr": nifti_header(rel) if s["kind"] == "nifti" and rel else None,
             "src_ds": read_dicom_header(root / rel) if s["kind"] == "dicom" and rel else None}
        inst = None if stale else first.get(s["series_uid"])
        out_ds = None
        if inst:
            try:
                out_ds = pydicom.dcmread(run.dir / inst["path"], stop_before_pixels=True)
            except Exception:  # noqa: BLE001 - shown as "not extracted"
                out_ds = None
        rows = _dicom_rows(c, out_ds) + _db_rows(c, recs, first_rows.get(s["series_uid"]) if inst else None)
        cases.setdefault(s["subject_key"], []).append({
            "uid": s["series_uid"], "kind": s["kind"], "modality": s.get("modality"),
            "label": f"{KIND_LABEL.get(s['kind'], s['kind'])} {s.get('modality') or ''}".strip(), "source": rel,
            "frozen": out_ds is not None, "rows": rows,
            "flags": sum(1 for r in rows if r["flag"])})
    ds_row = recs["datasets"][0] if recs["datasets"] else {}
    dataset = [_row("DB", f"datasets.{k}", "에이전트 분석 (데이터셋 문서)" if k not in ("dataset_uuid",) else "입력 폴더",
                    v if k != "dataset_uuid" else plan["input_root"], v,
                    "그대로" if k != "dataset_uuid" else "새 UUID 생성") for k, v in ds_row.items()]
    return {"ready": True, "stale": stale and bool(pilot), "extracted": bool(pilot) and not stale,
            "cases": [{"subject_key": k, "series": v} for k, v in cases.items()], "dataset": dataset}
