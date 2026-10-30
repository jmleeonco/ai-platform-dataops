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

from . import deid
from .inventory import read_dicom_header
from .metadata import HEADERS_FILE, KIND_LABEL, TAG_SOURCE, read_nifti_header
from .prepare import pilot_series
from .records import absolute, build_records, instance_rows, series_source_path
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
            "changed": src != out,      # a prefixed source ID (<prefix>_<PatientID>) still carries it
            "flag": "식별 정보 그대로" if ident and out and src and (src == out or src in out) else None}


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
    ("series", "source_path"): "원본 전체 경로", ("instances", "source_path"): "원본 전체 경로",
    ("ingest_runs", "input_root"): "입력 폴더", ("instances", "artifact_relative_path"): "변환된 파일 (이 서버)",
}
# DB columns that keep a source value as is: identifying when the source is an original DICOM export.
DB_KEEPS = {("subjects", "source_subject_key"), ("subjects", "patient_id"), ("studies", "study_date"),
            ("instances", "source_relative_path"), ("instances", "source_path"), ("series", "source_path")}


def _db_rows(table: str, rec: dict, c: dict) -> list[dict]:
    """One DB row, column by column: the source value it comes from and the value registration writes."""
    s = c["s"]
    src_val = {"대상자 키": s["subject_key"], "원본 경로": c["rel"], "원본 파일 내용": "(파일 바이트)",
               "Modality 근거": s.get("modality_basis"), "데이터셋 이름": c["dataset_name"],
               "원본 전체 경로": c["abs"], "입력 폴더": c["input_root"], "변환된 파일 (이 서버)": c.get("artifact"),
               "원본 StudyDate": str(c["src_ds"].get("StudyDate", "")) if c.get("src_ds") is not None else None}
    rows = []
    for col, v in rec.items():
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
        rows.append(_row("DB", col, label or "—", sv, v, action, (table, col) in DB_KEEPS and s["kind"] == "dicom"))
    return rows


def _dataset_rows(rec: dict, input_root: str) -> list[dict]:
    return [_row("DB", k, "입력 폴더" if k == "dataset_uuid" else "에이전트 분석 (데이터셋 문서)",
                 input_root if k == "dataset_uuid" else v, v, "새 UUID 생성" if k == "dataset_uuid" else "그대로")
            for k, v in rec.items()]


def sample_view(run: Run) -> dict:
    """{"cases": [{"subject_key", "dicom": [series with rows], "db": [{"table", "label", "rows"}], "flags"}]}:
    per sample subject, what the DICOM files carry (one entry per series) and what the DB gets (one entry per
    row: dataset, subject, study, each series and its link, the first instance of each series)."""
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

    def one(table, key, value):
        return next((r for r in recs[table] if r[key] == value), None)
    ds_rec = recs["datasets"][0] if recs["datasets"] else {}
    cases = {}
    for s in sorted(pilot_series(plan), key=lambda x: (x["subject_key"], x["kind"] == "rtstruct", x["series_key"])):
        rel = (s.get("sources") or [None])[0]
        image = by_key.get(s.get("reference")) if s["kind"] == "rtstruct" else None
        c = {"s": s, "rel": rel, "dataset_name": plan["dataset"].get("name"), "input_root": plan["input_root"],
             "image_rel": ((image or {}).get("sources") or [None])[0],
             "hdr": nifti_header(rel) if s["kind"] == "nifti" and rel else None,
             "src_ds": read_dicom_header(root / rel) if s["kind"] == "dicom" and rel else None}
        inst = None if stale else first.get(s["series_uid"])
        out_ds = None
        c["abs"] = absolute(plan, inst["source"]["rel"]) if inst else series_source_path(plan, s)
        c["artifact"] = str(run.dir / inst["path"]) if inst else None
        if inst:
            try:
                out_ds = pydicom.dcmread(run.dir / inst["path"], stop_before_pixels=True)
            except Exception:  # noqa: BLE001 - shown as "not extracted"
                out_ds = None
        label = f"{KIND_LABEL.get(s['kind'], s['kind'])} {s.get('modality') or ''}".strip()
        case = cases.setdefault(s["subject_key"], {"subject_key": s["subject_key"], "dicom": [], "db": []})
        rows = _dicom_rows(c, out_ds)
        case["dicom"].append({"uid": s["series_uid"], "kind": s["kind"], "label": label, "source": rel,
                              "frozen": out_ds is not None, "rows": rows, "flags": sum(1 for r in rows if r["flag"])})
        db = case["db"]
        if not db:
            db.append({"table": "datasets", "label": "데이터셋 (모든 샘플 공통)",
                       "rows": _dataset_rows(ds_rec, plan["input_root"])})
            for table, key, value in (("subjects", "source_subject_key", deid.subject(plan, s["subject_key"])),
                                      ("studies", "study_uid", s["study_uid"])):
                rec = one(table, key, value)
                if rec:
                    db.append({"table": table, "label": "", "rows": _db_rows(table, rec, c)})
            if recs["ingest_runs"]:
                c_run = {**c, "abs": series_source_path(plan, s)}
                db.append({"table": "ingest_runs", "label": "이 등록 실행 (모든 샘플 공통)",
                           "rows": _db_rows("ingest_runs", recs["ingest_runs"][0], c_run)})
        for table, key, rec in (("series", "series_uid", one("series", "series_uid", s["series_uid"])),
                                ("dataset_series", "series_uid", one("dataset_series", "series_uid", s["series_uid"])),
                                ("instances", "series_uid", first_rows.get(s["series_uid"]) if inst else None)):
            if rec:
                db.append({"table": table, "label": label + (" · 첫 파일 (파일마다 1행)" if table == "instances" else ""),
                           "rows": _db_rows(table, rec, c)})
    sent = {}
    if pilot and not stale:
        from .local_send import journals
        uid_subject = {se["series_uid"]: se["subject_key"] for se in pilot["series"]}
        for j in journals(run):
            if j.get("scope") == "pilot" and j["manifest_sha256"] == pilot["manifest_sha256"]:
                for i in pilot["instances"]:
                    n = sent.setdefault(uid_subject[i["series_uid"]], [0, 0])
                    n[1] += 1
                    n[0] += j["instances"].get(i["sop_uid"], {}).get("status") in ("accepted", "warning")
    out = []
    for case in cases.values():
        n = sent.get(case["subject_key"])
        case["sent"] = f"수신 {n[0]}/{n[1]}" if n and n[0] else None
        case["flags"] = sum(se["flags"] for se in case["dicom"]) + sum(
            1 for t in case["db"] for r in t["rows"] if r["flag"])
        out.append(case)
    return {"ready": True, "stale": stale and bool(pilot), "extracted": bool(pilot) and not stale, "cases": out,
            "deid": deid.enabled(plan)}
