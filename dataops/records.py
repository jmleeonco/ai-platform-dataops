"""The one projection from a run to DB rows: used both for the HTML preview and for registration."""
from __future__ import annotations

import json
import posixpath
from functools import lru_cache
from pathlib import PurePosixPath
from uuid import UUID, uuid5

import pydicom

from . import deid
from .runs import Run

TABLES = ["datasets", "subjects", "studies", "series", "dataset_series", "ingest_runs", "instances",
          "transfer_results", "deid_runs", "deid_subject_mappings", "deid_series_mappings", "deid_instance_mappings"]
DEID_TABLES = TABLES[-4:]          # rows only for a pseudonymised run: the re-identification key, in clear


def subject_uuid(dataset_uuid: str, source_subject_key: str) -> str:
    return str(uuid5(UUID(dataset_uuid), source_subject_key))


def _plan_summary(plan: dict) -> dict:
    """ingest_runs.plan_json: the plan without per-file source lists (the full plan stays in the run folder)."""
    return {"schema_version": plan["schema_version"], "adapter": plan["adapter"],
            "adapter_version": plan["adapter_version"], "options": plan["options"],
            "subjects": len(plan["subjects"]), "studies": len(plan["studies"]),
            "series": [{**{k: s.get(k) for k in ("kind", "excluded", "series_uid", "modality", "modality_basis",
                                                  "needs_review", "conversion")},
                        "series_key": deid.path(plan, s["series_key"])} for s in plan["series"]],
            "deid": {k: v for k, v in (plan.get("deid") or {"enabled": False}).items() if k not in ("key", "subjects")},
            "skipped": len(plan.get("skipped", [])), "unresolved": plan.get("unresolved", [])}


def build_records(run: Run, preview: bool = False) -> dict:
    """preview=True works before the full freeze: instance counts come from the plan, and the per-file rows
    (instances, transfer_results) stay empty because they only exist once artifacts are frozen and sent."""
    meta, plan, manifest = run.meta, run.load("plan.json"), run.load("manifest.json")
    if plan is None or (manifest is None and not preview):
        raise ValueError("prepare 이후에만 DB 레코드를 만들 수 있습니다")
    if manifest is None:
        manifest = {"manifest_sha256": None, "instances": []}
    ds = plan["dataset"]
    duuid = ds["dataset_uuid"]
    active = [s for s in plan["series"] if not s.get("excluded")]
    counts = {}
    for inst in manifest["instances"]:
        counts[inst["series_uid"]] = counts.get(inst["series_uid"], 0) + 1
    if manifest["manifest_sha256"] is None:
        counts = {s["series_uid"]: s.get("instance_count") or 0 for s in active}
    studies_meta = {st["study_uid"]: st.get("metadata") or {} for st in plan["studies"]}
    subj = {s["subject_key"]: s["patient_id"] for s in active}
    heads = outgoing_headers(run, plan, manifest, active)
    first_head = {}
    for s in active:
        first_head.setdefault(s["subject_key"], heads.get(s["series_uid"]) or {})
    rows = {t: [] for t in TABLES}
    rows["datasets"].append({k: ds.get(k) for k in ("dataset_uuid", "name", "source_url", "license",
                                                    "commercial_use", "description", "patient_id_prefix")})
    for key, pid in sorted(subj.items()):
        out_key = deid.subject(plan, key)
        h = first_head.get(key) or {}
        rows["subjects"].append({"subject_uuid": subject_uuid(duuid, out_key), "dataset_uuid": duuid,
                                 "source_subject_key": out_key, "patient_id": pid, "patient_name": pid,
                                 "patient_sex": _text(h.get("PatientSex"), 32), "patient_age": _text(h.get("PatientAge"), 16),
                                 "patient_weight": _text(h.get("PatientWeight"), 16),
                                 "patient_ethnicity": _text(h.get("EthnicGroup"), 64)})
    seen_studies = set()
    for s in active:
        if s["study_uid"] not in seen_studies:
            seen_studies.add(s["study_uid"])
            m = studies_meta.get(s["study_uid"], {})
            desc = m.get("StudyDescription") or (s["conversion"] or {}).get("study_description")
            rows["studies"].append({"study_uid": s["study_uid"],
                                    "subject_uuid": subject_uuid(duuid, deid.subject(plan, s["subject_key"])),
                                    "study_date": m.get("StudyDate"), "description": desc,
                                    "metadata_json": m or None})
        meta_json = deid.metadata(plan, dict(s.get("metadata") or {})) or None
        if meta_json and "label_of" in meta_json:
            meta_json["label_of"] = deid.path(plan, meta_json["label_of"])
        rows["series"].append({"series_uid": s["series_uid"], "study_uid": s["study_uid"], "modality": s["modality"],
                               "description": s.get("description"), "metadata_json": meta_json,
                               "series_number": int(s["series_number"]) if str(s.get("series_number") or "").lstrip(
                                   "-").isdigit() else None,
                               "instance_count": counts.get(s["series_uid"], 0), "source_kind": s["kind"],
                               "modality_basis": s.get("modality_basis"), "needs_review": int(bool(s.get("needs_review"))),
                               "first_run_uuid": meta["run_uuid"],
                               "source_path": deid.path(plan, series_source_path(plan, s)),
                               **series_columns(s, heads.get(s["series_uid"]) or {})})
        rows["dataset_series"].append({"dataset_uuid": duuid, "series_uid": s["series_uid"]})
    rows["ingest_runs"].append({"run_uuid": meta["run_uuid"], "run_label": run.id, "dataset_uuid": duuid,
                                "revision": meta["revision"], "manifest_sha256": manifest["manifest_sha256"],
                                "status": meta["status"], "plan_json": _plan_summary(plan),
                                "approval_json": meta.get("approval"), "input_root": plan["input_root"]})
    rows["instances"] = instance_rows(run, manifest, plan)
    rows["transfer_results"] = transfer_rows(run, {i["sop_uid"] for i in manifest["instances"]})
    if deid.enabled(plan):
        rows.update(deid_rows(run, plan, manifest, active, heads))
    return rows


def deid_rows(run: Run, plan: dict, manifest: dict, active: list[dict], heads: dict) -> dict:
    """The pseudonymisation maps (oncolabdb.enc_pipeline_runs / enc_patient_mappings / enc_series_mappings, and
    one row per file): every original next to what left, so the source can be found from the DB alone.
    Person names, birth dates and addresses are never read, so their columns stay empty."""
    ds, duuid, run_uuid = plan["dataset"], plan["dataset"]["dataset_uuid"], run.meta["run_uuid"]
    d = plan["deid"]
    out = {t: [] for t in DEID_TABLES}
    out["deid_runs"].append({
        "run_uuid": run_uuid, "dataset_uuid": duuid, "profile": d.get("profile") or deid.PROFILE,
        "patient_id_prefix": ds.get("patient_id_prefix"), "key_label": "hmac-sha256(deid.key, dataset_uuid)",
        "pipeline_version": f"{plan.get('adapter')} {plan.get('adapter_version')}",
        "summary_json": {"subjects": len({s["subject_key"] for s in active}), "series": len(active),
                         "instances": len(manifest["instances"]), "dates": "retained"}})
    seen = set()
    for s in active:
        key = s["subject_key"]
        if key in seen:
            continue
        seen.add(key)
        src = s.get("source_ids") or {}
        out["deid_subject_mappings"].append({
            "subject_uuid": subject_uuid(duuid, deid.subject(plan, key)), "dataset_uuid": duuid, "run_uuid": run_uuid,
            "internal_hash": deid.mac(plan, "subject:" + key)[:32],
            "original_patient_id": src.get("patient_id") or key, "original_patient_name": None,
            "original_patient_birth_date": None, "original_patient_address": None,
            "original_institution": (s.get("metadata") or {}).get("InstitutionName"),
            "pseudonym_patient_id": s["patient_id"]})
    for s in active:
        src, m, h = s.get("source_ids") or {}, s.get("metadata") or {}, heads.get(s["series_uid"]) or {}
        orig = src.get("series_uid") if s["kind"] == "dicom" else None
        out["deid_series_mappings"].append({
            "series_uid": s["series_uid"], "run_uuid": run_uuid,
            "internal_hash": deid.mac(plan, "series:" + (orig or s["series_key"]))[:32], "modality": s["modality"],
            "original_series_uid": orig, "original_study_uid": src.get("study_uid") if orig else None,
            "original_source_path": series_source_path(plan, s),
            # the same attribute on both sides: AcquisitionDate/Time when the source has it, else SeriesDate/Time
            **{f"{side}_acq_{part}": _text(src_of.get(f"Acquisition{part.title()}") if f"Acquisition{part.title()}" in m
                                           else src_of.get(f"Series{part.title()}"), 32)
               for side, src_of in (("original", m), ("out", h)) for part in ("date", "time")}})
    out["deid_instance_mappings"] = [
        {"sop_uid": i["sop_uid"], "run_uuid": run_uuid, "original_sop_uid": i["source"].get("sop_uid"),
         "original_source_path": absolute(plan, i["source"]["rel"])} for i in manifest["instances"]]
    return out


# --------------------------------------------------------------------------- outgoing per-series header values
HEAD_TAGS = ["SOPClassUID", "SeriesDate", "SeriesTime", "AcquisitionDate", "AcquisitionTime", "BodyPartExamined",
             "Manufacturer", "FrameOfReferenceUID", "SliceThickness", "SpacingBetweenSlices", "PixelSpacing",
             "ImageOrientationPatient", "PatientPosition", "ImageType", "RescaleSlope", "RescaleIntercept",
             "BitsAllocated", "RepetitionTime", "EchoTime", "InversionTime", "FlipAngle", "MagneticFieldStrength",
             "ScanningSequence", "SequenceVariant", "SequenceName", "ContrastBolusAgent", "ContrastBolusVolume",
             "DiffusionBValue", "NumberOfTemporalPositions", "TemporalPositionIdentifier", "ContrastBolusIngredient",
             "ContrastBolusTotalDose", "PatientSex", "PatientAge", "PatientWeight", "EthnicGroup"]
# series column (named like oncolabdb.lab_dataset_items) <- DICOM attribute, and how the value is stored
SERIES_COLUMNS = [
    ("sop_class_uid", "SOPClassUID", "text64"), ("series_date", "SeriesDate", "text16"),
    ("series_time", "SeriesTime", "text16"), ("body_part_examined", "BodyPartExamined", "text64"),
    ("manufacturer", "Manufacturer", "text128"), ("frame_of_reference_uid", "FrameOfReferenceUID", "text64"),
    ("referenced_series_uid", "ReferencedSeriesUID", "text64"), ("roi_names", "RoiNames", "json"),
    ("slice_thickness", "SliceThickness", "num4"), ("spacing_between_slices", "SpacingBetweenSlices", "num4"),
    ("pixel_spacing", "PixelSpacing", "text64"), ("image_orientation", "ImageOrientationPatient", "text128"),
    ("patient_position", "PatientPosition", "text32"), ("image_type", "ImageType", "text256"),
    ("rescale_slope", "RescaleSlope", "num6"), ("rescale_intercept", "RescaleIntercept", "num6"),
    ("bits_allocated", "BitsAllocated", "int"), ("repetition_time", "RepetitionTime", "num3"),
    ("echo_time", "EchoTime", "num3"), ("inversion_time", "InversionTime", "num3"), ("flip_angle", "FlipAngle", "num2"),
    ("magnetic_field_strength", "MagneticFieldStrength", "num3"), ("scanning_sequence", "ScanningSequence", "text64"),
    ("sequence_variant", "SequenceVariant", "text64"), ("sequence_name", "SequenceName", "text128"),
    ("diffusion_b_value", "DiffusionBValue", "num3"), ("number_of_temporal_positions", "NumberOfTemporalPositions", "int"),
    ("temporal_position_identifier", "TemporalPositionIdentifier", "int"),
    ("contrast_bolus_agent", "ContrastBolusAgent", "text128"), ("contrast_bolus_ingredient", "ContrastBolusIngredient", "text128"),
    ("contrast_bolus_volume", "ContrastBolusVolume", "num3"), ("contrast_bolus_total_dose", "ContrastBolusTotalDose", "num3"),
]


def _text(v, n: int):
    if v in (None, "", []):
        return None
    v = "\\".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
    return v[:n]


def _number(v, places: int | None = None):
    """A number as the DECIMAL(…, places) column keeps it (places None: an integer)."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    try:
        return None if v in (None, "") else (int(float(v)) if places is None else round(float(v), places))
    except (TypeError, ValueError):
        return None


def series_columns(s: dict, h: dict) -> dict:
    out = {}
    for col, tag, how in SERIES_COLUMNS:
        v = h.get(tag)
        if how.startswith("num"):
            out[col] = _number(v, int(how[3:]))
        elif how == "int":
            out[col] = _number(v)
        elif how == "json":
            out[col] = json.dumps(v, ensure_ascii=False) if v else None
        else:
            out[col] = _text(v, int(how[4:]))
    if not out["sop_class_uid"] and s.get("sop_class_uids"):
        out["sop_class_uid"] = s["sop_class_uids"][0]
    return out


@lru_cache(maxsize=4096)
def _read_head(path: str, mtime_ns: int) -> dict:
    return head_values(pydicom.dcmread(path, stop_before_pixels=True))


def head_values(ds) -> dict:
    """The series-level values the DB keeps (HEAD_TAGS, ROI names, referenced series) of one DICOM dataset."""
    from .inventory import _jsonable
    h = {t: _jsonable(ds.get(t)) for t in HEAD_TAGS if t in ds}
    if "StructureSetROISequence" in ds:
        h["RoiNames"] = [str(r.get("ROIName", "")) for r in ds.StructureSetROISequence]
        refs = [se.SeriesInstanceUID for fr in ds.get("ReferencedFrameOfReferenceSequence", [])
                for st in fr.get("RTReferencedStudySequence", []) for se in st.get("RTReferencedSeriesSequence", [])]
        if refs:
            h["ReferencedSeriesUID"] = str(refs[0])
    return {k: v for k, v in h.items() if v not in (None, "", [])}


def outgoing_headers(run: Run, plan: dict, manifest: dict, active: list[dict]) -> dict:
    """{series_uid: header values}: read from the first frozen file of the series (what really leaves), the full
    manifest first, then the samples; a series not frozen yet falls back to its source header, with UIDs as they
    will leave (a preview before the full conversion)."""
    first = {}
    pilot = run.load("manifest-pilot.json") or {"instances": []}
    for inst in [*manifest.get("instances", []), *pilot["instances"]]:
        first.setdefault(inst["series_uid"], inst["path"])
    out = {}
    for s in active:
        rel = first.get(s["series_uid"])
        p = run.dir / rel if rel else None
        try:
            if p is not None and p.exists():
                out[s["series_uid"]] = _read_head(str(p), p.stat().st_mtime_ns)
                continue
        except Exception:  # noqa: BLE001 - an unreadable artifact falls back to the plan
            pass
        m = deid.metadata(plan, dict(s.get("metadata") or {})) or {}
        if s["kind"] == "nifti" and m.get("spacing_mm"):
            sp = m["spacing_mm"]
            m = {**m, "SliceThickness": sp[2], "PixelSpacing": [sp[1], sp[0]]}
        out[s["series_uid"]] = m
    return out


def absolute(plan: dict, rel: str | None) -> str | None:
    """A source path as an absolute path on this host (input folder + relative path)."""
    return None if rel is None else str(PurePosixPath(plan["input_root"]) / rel)


def series_source_path(plan: dict, s: dict) -> str | None:
    """The file a converted series comes from, or the folder holding a DICOM series' files."""
    srcs = s.get("sources") or []
    if not srcs:
        return None
    if s["kind"] != "dicom" or len(srcs) == 1:
        return absolute(plan, srcs[0])
    parent = posixpath.commonpath(srcs)
    return absolute(plan, parent) if parent else plan["input_root"]


def instance_rows(run: Run, manifest: dict, plan: dict | None = None) -> list[dict]:
    """instances rows for a frozen manifest (the full one at registration; the pilot one as a preview)."""
    plan = plan if plan is not None else (run.load("plan.json") or {})
    return [{"sop_uid": inst["sop_uid"], "series_uid": inst["series_uid"], "sop_class_uid": inst["sop_class_uid"],
             "artifact_sha256": inst["sha256"], "artifact_relative_path": f"{run.id}/{inst['path']}",
             "prepared_run_uuid": run.meta["run_uuid"], "instance_number": inst["instance_number"],
             "slice_location": inst.get("slice_location"),
             "size_bytes": inst["size"], "transfer_syntax_uid": inst["transfer_syntax"] or None,
             "source_relative_path": deid.path(plan, inst["source"]["rel"]),
             "source_path": deid.path(plan, absolute(plan, inst["source"]["rel"])),
             "source_sha256": inst["source"]["sha256"]}
            for inst in manifest["instances"]]


def transfer_rows(run: Run, frozen: set) -> list[dict]:
    """transfer_results rows: every journal entry for a frozen SOP (any target, pilot or full send)."""
    from .local_send import journals  # local import: avoids a cycle at module load
    out = []
    for j in journals(run) if frozen else []:
        for sop, rec in sorted(j["instances"].items()):
            if sop not in frozen:            # e.g. a pilot frozen from an earlier plan revision
                continue
            out.append({"run_uuid": run.meta["run_uuid"], "target_key": j["target_key"], "sop_uid": sop,
                        "target_snapshot": j["target"], "status": rec["status"], "attempts": rec.get("attempts", 0),
                        "response_code": None, "verification_json": None, "error_message": rec.get("detail") or None})
    return out


TABLE_DESC = {
    "datasets": "공개 데이터셋 1행", "subjects": "대상자 (원본 키 → PatientID)", "studies": "대상자별 검사",
    "series": "검사별 시리즈", "dataset_series": "데이터셋과 시리즈의 소속", "ingest_runs": "이 등록 실행 1행",
    "instances": "DICOM 파일 (SOP Instance)", "transfer_results": "SOP별 전송 결과",
    "deid_runs": "가명화 실행 1행 (oncolabdb.enc_pipeline_runs 대응)",
    "deid_subject_mappings": "원본 환자 ↔ 가명 (재식별 키, enc_patient_mappings 대응)",
    "deid_series_mappings": "원본 시리즈 ↔ 보낸 시리즈 (enc_series_mappings 대응)",
    "deid_instance_mappings": "원본 파일 ↔ 보낸 파일",
}
SERIES_COLUMN_DESC = {
    "sop_class_uid": "SOPClassUID", "series_date": "SeriesDate", "series_time": "SeriesTime",
    "body_part_examined": "BodyPartExamined", "manufacturer": "Manufacturer", "frame_of_reference_uid": "FrameOfReferenceUID",
    "referenced_series_uid": "RTSTRUCT가 가리키는 영상 시리즈", "roi_names": "RTSTRUCT ROI 이름 (JSON 목록)",
    "slice_thickness": "SliceThickness", "spacing_between_slices": "SpacingBetweenSlices", "pixel_spacing": "PixelSpacing (행\\열)",
    "image_orientation": "ImageOrientationPatient", "patient_position": "PatientPosition", "image_type": "ImageType",
    "rescale_slope": "RescaleSlope", "rescale_intercept": "RescaleIntercept", "bits_allocated": "BitsAllocated",
    "repetition_time": "RepetitionTime", "echo_time": "EchoTime", "inversion_time": "InversionTime", "flip_angle": "FlipAngle",
    "magnetic_field_strength": "MagneticFieldStrength", "scanning_sequence": "ScanningSequence",
    "sequence_variant": "SequenceVariant", "sequence_name": "SequenceName", "diffusion_b_value": "DiffusionBValue",
    "number_of_temporal_positions": "NumberOfTemporalPositions", "temporal_position_identifier": "TemporalPositionIdentifier",
    "contrast_bolus_agent": "ContrastBolusAgent", "contrast_bolus_ingredient": "ContrastBolusIngredient",
    "contrast_bolus_volume": "ContrastBolusVolume", "contrast_bolus_total_dose": "ContrastBolusTotalDose"}
# Every column registration writes, in the order of build_records; the DB fills AUTO_COLUMNS itself.
COLUMN_DESC = {
    "datasets": {
        "dataset_uuid": "데이터셋 UUID (원본 폴더에서 결정)", "name": "데이터셋 이름", "source_url": "출처 URL",
        "license": "라이선스", "commercial_use": "상업적 이용 Y/N/U", "description": "설명",
        "patient_id_prefix": "PatientID 접두어"},
    "subjects": {
        "subject_uuid": "dataset UUID + 대상자 키로 결정적 생성", "dataset_uuid": "→ datasets",
        "source_subject_key": "대상자 키 (원본 파일명·PatientID, 가명화 시 가명 번호)", "patient_id": "DICOM PatientID (접두어_대상자 키)",
        "patient_name": "DICOM PatientName (= PatientID)", "patient_sex": "PatientSex", "patient_age": "PatientAge",
        "patient_weight": "PatientWeight", "patient_ethnicity": "EthnicGroup (가명화 시 비움)"},
    "studies": {
        "study_uid": "DICOM StudyInstanceUID", "subject_uuid": "→ subjects", "study_date": "StudyDate (원본에 있을 때)",
        "description": "StudyDescription", "metadata_json": "원본 DICOM의 검사 태그 (DICOM 원본일 때)"},
    "series": {
        "series_uid": "DICOM SeriesInstanceUID", "study_uid": "→ studies", "modality": "CT / MR / RTSTRUCT",
        "description": "SeriesDescription", "metadata_json": "원본에서 뽑은 값: 분할, 크기, 간격, 축 방향, 자료형, 라벨 대상",
        "series_number": "SeriesNumber", "instance_count": "DICOM 파일 수", "source_kind": "원본 형식 nifti / rtstruct / dicom",
        "modality_basis": "modality를 정한 근거", "needs_review": "검토 필요 1/0", "first_run_uuid": "처음 등록한 실행",
        "source_path": "원본 전체 경로 (변환한 파일, DICOM이면 파일들이 있는 폴더)", **SERIES_COLUMN_DESC},
    "dataset_series": {"dataset_uuid": "→ datasets", "series_uid": "→ series"},
    "ingest_runs": {
        "run_uuid": "실행 UUID", "run_label": "실행 ID (run 폴더 이름)", "dataset_uuid": "→ datasets",
        "revision": "plan revision", "manifest_sha256": "전체 변환 manifest 해시 (변환 뒤)", "status": "실행 상태",
        "plan_json": "plan 요약 (규칙·옵션·시리즈별 modality)", "approval_json": "최종 승인 기록",
        "input_root": "입력 폴더 (절대 경로)"},
    "instances": {
        "sop_uid": "DICOM SOPInstanceUID", "series_uid": "→ series", "sop_class_uid": "SOPClassUID",
        "artifact_sha256": "변환된 DICOM 파일 해시", "artifact_relative_path": "run 폴더 기준 파일 경로",
        "prepared_run_uuid": "→ ingest_runs (처음 변환한 실행)", "instance_number": "InstanceNumber",
        "slice_location": "SliceLocation",
        "size_bytes": "파일 크기", "transfer_syntax_uid": "TransferSyntaxUID", "source_relative_path": "원본 파일 (입력 폴더 기준)",
        "source_path": "원본 파일 전체 경로", "source_sha256": "원본 파일 해시"},
    "transfer_results": {
        "run_uuid": "→ ingest_runs", "target_key": "전송 대상 AE@host:port", "sop_uid": "→ instances",
        "target_snapshot": "전송 당시 대상 설정", "status": "accepted / warning / failed", "attempts": "시도 횟수",
        "response_code": "응답 코드", "verification_json": "수신 확인 결과", "error_message": "오류"},
    "deid_runs": {
        "run_uuid": "→ ingest_runs", "dataset_uuid": "→ datasets", "profile": "가명화 방식", "patient_id_prefix": "PatientID 접두어",
        "key_label": "가명 키 (이 서버의 비밀키 + 데이터셋)", "pipeline_version": "변환기 버전", "summary_json": "대상자·시리즈·파일 수"},
    "deid_subject_mappings": {
        "subject_uuid": "→ subjects", "dataset_uuid": "→ datasets", "run_uuid": "→ ingest_runs (처음 등록한 실행)",
        "internal_hash": "원본 키의 keyed hash", "original_patient_id": "원본 PatientID (NIfTI면 원본 대상자 키)",
        "original_patient_name": "원본 이름 (수집하지 않음)", "original_patient_birth_date": "원본 생년월일 (수집하지 않음)",
        "original_patient_address": "원본 주소 (수집하지 않음)", "original_institution": "원본 InstitutionName",
        "pseudonym_patient_id": "가명 PatientID"},
    "deid_series_mappings": {
        "series_uid": "→ series (보낸 UID)", "run_uuid": "→ ingest_runs (처음 등록한 실행)", "internal_hash": "원본 시리즈의 keyed hash",
        "modality": "Modality", "original_series_uid": "원본 SeriesInstanceUID (DICOM 원본일 때)",
        "original_study_uid": "원본 StudyInstanceUID", "original_source_path": "원본 전체 경로",
        "original_acq_date": "원본 촬영 날짜", "original_acq_time": "원본 촬영 시각", "out_acq_date": "보낸 촬영 날짜 (날짜 유지)",
        "out_acq_time": "보낸 촬영 시각"},
    "deid_instance_mappings": {
        "sop_uid": "→ instances (보낸 UID)", "run_uuid": "→ ingest_runs (처음 등록한 실행)",
        "original_sop_uid": "원본 SOPInstanceUID", "original_source_path": "원본 파일 전체 경로"},
}
AUTO_COLUMNS = {"datasets": ["created_at", "updated_at"], "subjects": ["created_at"], "studies": ["created_at"],
                "series": ["created_at", "updated_at"], "dataset_series": [],
                "ingest_runs": ["created_at", "updated_at", "registered_at"], "instances": ["created_at"],
                "transfer_results": ["updated_at"], "deid_runs": ["created_at"], "deid_subject_mappings": ["created_at"],
                "deid_series_mappings": ["created_at"], "deid_instance_mappings": []}


JSON_COLUMNS = {"metadata_json", "plan_json", "approval_json", "target_snapshot", "verification_json", "summary_json"}


def to_db_value(column: str, value):
    if column in JSON_COLUMNS and value is not None:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value
