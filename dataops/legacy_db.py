"""The existing production DB (scpdb / oncolabdb on MariaDB 10.5.8), read for its *shape* only.

Only information_schema is queried (tables, columns, indexes, foreign keys) plus VERSION(), in a read-only
session; no row of a production table is ever read or written. The result is kept as a snapshot in the
runtime folder so the page works without a connection. `MAPPING` says where each value of the new DB goes
in the existing one (docs/DATABASE.md "Export/import to oncolabdb") and is checked against the snapshot.
"""
from __future__ import annotations

import json
import re

import pymysql
import pymysql.cursors

from .util import now_iso, write_json

SNAPSHOT = "legacy_schema.json"
QUERIES = {   # the only statements this module runs
    "version": "SELECT VERSION() AS v",
    "tables": "SELECT TABLE_SCHEMA s, TABLE_NAME t, ENGINE engine, TABLE_ROWS rows_est, "
              "DATA_LENGTH + INDEX_LENGTH bytes, TABLE_COMMENT comment FROM information_schema.TABLES "
              "WHERE TABLE_SCHEMA IN ({}) ORDER BY TABLE_SCHEMA, TABLE_NAME",
    "columns": "SELECT TABLE_SCHEMA s, TABLE_NAME t, ORDINAL_POSITION n, COLUMN_NAME c, COLUMN_TYPE type, "
               "IS_NULLABLE nullable, COLUMN_DEFAULT dflt, COLUMN_KEY k, EXTRA extra, COLUMN_COMMENT comment "
               "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA IN ({}) ORDER BY TABLE_SCHEMA, TABLE_NAME, "
               "ORDINAL_POSITION",
    "indexes": "SELECT TABLE_SCHEMA s, TABLE_NAME t, INDEX_NAME i, NON_UNIQUE non_unique, SEQ_IN_INDEX seq, "
               "COLUMN_NAME c FROM information_schema.STATISTICS WHERE TABLE_SCHEMA IN ({}) "
               "ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
    "fks": "SELECT TABLE_SCHEMA s, TABLE_NAME t, COLUMN_NAME c, REFERENCED_TABLE_SCHEMA rs, "
           "REFERENCED_TABLE_NAME rt, REFERENCED_COLUMN_NAME rc FROM information_schema.KEY_COLUMN_USAGE "
           "WHERE TABLE_SCHEMA IN ({}) AND REFERENCED_TABLE_NAME IS NOT NULL",
}

# new DB value -> existing DB column (schema.table.column), how it gets there, and what we know about it
MAPPING = [
    ("datasets.name", "oncolabdb.lab_datasets.name", "내보내기", "필수 (기본값 없음)"),
    ("datasets.dataset_uuid", "oncolabdb.lab_datasets.id", "external_mappings로 연결",
     "기존 id는 auto increment. 이름·숫자로 맞추지 않음"),
    ("datasets.description", "oncolabdb.lab_datasets.description", "내보내기", "열 존재 확인 필요"),
    ("dataset_series.dataset_uuid", "oncolabdb.lab_dataset_items.dataset_id", "external_mappings로 변환",
     "필수, FK"),
    ("dataset_series.series_uid", "oncolabdb.lab_dataset_items.series_instance_uid", "내보내기",
     "필수, (dataset_id, series_instance_uid) unique"),
    ("studies.study_uid", "oncolabdb.lab_dataset_items.study_instance_uid", "내보내기", "필수"),
    ("subjects.patient_id", "oncolabdb.lab_dataset_items.patient_id", "내보내기", "필수, 64자 이하"),
    ("(Central 수신 확인 뒤 'pacs')", "oncolabdb.lab_dataset_items.source_type", "내보내기", "기본값 local"),
    ("datasets.license", "oncolabdb.lab_datasets.license", "내보내기", ""),
    ("datasets.source_url", "oncolabdb.lab_datasets.source_url", "내보내기", ""),
    ("datasets.commercial_use", "oncolabdb.lab_datasets.commercial_use", "값 변환 후 내보내기",
     "새 DB는 Y/N/U, 기존은 'not_available' 같은 단어 — 값 대응 확인 필요"),
    ("(시리즈·대상자 수)", "oncolabdb.lab_datasets.series_count", "등록된 소속에서 계산", "patient_count, modalities도 같음"),
    ("series.modality", "oncolabdb.lab_dataset_items.modality", "내보내기", ""),
    ("series.description", "oncolabdb.lab_dataset_items.series_description", "내보내기", ""),
    ("series.series_number", "oncolabdb.lab_dataset_items.series_number", "내보내기", "기존은 문자열"),
    ("series.instance_count", "oncolabdb.lab_dataset_items.instance_count", "내보내기", ""),
    ("studies.description", "oncolabdb.lab_dataset_items.study_description", "내보내기", ""),
    ("studies.study_date", "oncolabdb.lab_dataset_items.study_date", "내보내기", "NIfTI 변환본은 비어 있음"),
    *[(f"series.{c}", f"oncolabdb.lab_dataset_items.{c}", "내보내기", "보낸 DICOM 첫 파일의 값 (migration 0004)")
      for c in ("slice_thickness", "spacing_between_slices", "pixel_spacing", "image_orientation", "patient_position",
                "image_type", "rescale_slope", "rescale_intercept", "bits_allocated", "repetition_time", "echo_time",
                "inversion_time", "flip_angle", "magnetic_field_strength", "scanning_sequence", "sequence_variant",
                "sequence_name", "diffusion_b_value", "number_of_temporal_positions", "temporal_position_identifier",
                "contrast_bolus_agent", "contrast_bolus_ingredient", "contrast_bolus_volume",
                "contrast_bolus_total_dose", "series_date", "body_part_examined")],
    ("series.roi_names", "oncolabdb.lab_dataset_items.roi_names", "내보내기", "라벨 시리즈만. 새 DB는 JSON 목록 — 기존 구분자 확인 필요"),
    ("series.referenced_series_uid", "oncolabdb.lab_dataset_items.referenced_series_uid", "내보내기", "라벨 시리즈만"),
    ("subjects.patient_name", "oncolabdb.lab_dataset_items.patient_name", "내보내기", "= PatientID (명명 규칙)"),
    ("subjects.patient_sex", "oncolabdb.lab_dataset_items.patient_sex", "내보내기", ""),
    ("subjects.patient_age", "oncolabdb.lab_dataset_items.patient_age", "내보내기", ""),
    ("subjects.patient_weight", "oncolabdb.lab_dataset_items.patient_weight", "내보내기", ""),
    ("subjects.patient_ethnicity", "oncolabdb.lab_dataset_items.patient_ethnicity", "내보내기", "가명화 시 비어 있음"),
    ("series.source_path", "oncolabdb.lab_dataset_items.study_path", "의미 확인 필요",
     "기존 study_path가 원본 위치인지 NAS 위치인지 모름"),
    ("subjects.patient_id", "scpdb.dicom_instances.PatientID", "Central SCP가 수신하며 등록",
     "우리가 쓰지 않음. 같은 PatientID가 이미 있는지 전송 전 확인 필요"),
    ("studies.study_uid", "scpdb.dicom_instances.StudyInstanceUID", "Central SCP가 수신하며 등록", "UID 그대로"),
    ("series.series_uid", "scpdb.dicom_instances.SeriesInstanceUID", "Central SCP가 수신하며 등록", "UID 그대로"),
    ("instances.sop_uid", "scpdb.dicom_instances.SOPInstanceUID", "Central SCP가 수신하며 등록",
     "기존 index가 unique 아님"),
    ("series.modality", "scpdb.dicom_instances.Modality", "Central SCP가 수신하며 등록", ""),
    ("transfer_results.verification_json", "scpdb.dicom_instances.InstancePath", "Central 수신 확인 때 읽어 옴",
     "Central의 실제 저장 위치"),
    *[(f"{new}", f"scpdb.dicom_instances.{old}", "Central SCP가 수신하며 등록", "보낸 DICOM 값 그대로")
      for new, old in (("subjects.patient_name", "PatientName"), ("subjects.patient_sex", "PatientSex"),
                       ("subjects.patient_age", "PatientAge"), ("studies.study_date", "StudyDate"),
                       ("studies.description", "StudyDescription"), ("series.series_number", "SeriesNumber"),
                       ("series.series_date", "SeriesDate"), ("series.series_time", "SeriesTime"),
                       ("series.description", "SeriesDescription"), ("series.manufacturer", "Manufacturer"),
                       ("series.frame_of_reference_uid", "FrameOfReferenceUID"), ("instances.sop_class_uid", "SOPClassUID"),
                       ("instances.slice_location", "SliceLocation"), ("series.referenced_series_uid", "ReferencedSeriesUID"),
                       ("series.roi_names", "RoiNames"))],
    # pseudonymisation maps: oncolabdb.enc_* (the existing pipeline's re-identification tables)
    ("deid_runs.run_uuid", "oncolabdb.enc_pipeline_runs.run_id", "내보내기", "가명화한 실행만"),
    ("ingest_runs.created_at", "oncolabdb.enc_pipeline_runs.started_at", "내보내기", ""),
    ("ingest_runs.registered_at", "oncolabdb.enc_pipeline_runs.completed_at", "내보내기", ""),
    ("ingest_runs.status", "oncolabdb.enc_pipeline_runs.terminal_status", "값 변환 후 내보내기", "상태 어휘가 다름"),
    ("ingest_runs.approval_json (approved_by)", "oncolabdb.enc_pipeline_runs.operator_subject", "값 꺼내서 내보내기", ""),
    ("ingest_runs.manifest_sha256", "oncolabdb.enc_pipeline_runs.config_fingerprint", "내보내기", "의미 확인 필요"),
    ("datasets.name", "oncolabdb.enc_pipeline_runs.src_dataset_name", "내보내기", ""),
    ("deid_runs.patient_id_prefix", "oncolabdb.enc_pipeline_runs.patient_id_prefix", "내보내기", ""),
    ("deid_runs.key_label", "oncolabdb.enc_pipeline_runs.pseudonym_internal_hash_label", "내보내기", ""),
    ("deid_runs.pipeline_version", "oncolabdb.enc_pipeline_runs.pipeline_version", "내보내기", ""),
    ("deid_runs.summary_json", "oncolabdb.enc_pipeline_runs.summary_json", "내보내기", ""),
    ("external_mappings.target_dataset_id", "oncolabdb.enc_pipeline_runs.lab_dataset_id", "external_mappings로 연결", ""),
    ("deid_subject_mappings.run_uuid", "oncolabdb.enc_patient_mappings.run_id", "내보내기", ""),
    ("deid_subject_mappings.internal_hash", "oncolabdb.enc_patient_mappings.internal_hash", "내보내기",
     "keyed hash 32자 — 기존 방식과 값이 같지는 않음"),
    *[(f"deid_subject_mappings.{c}", f"oncolabdb.enc_patient_mappings.{c}", "내보내기", note)
      for c, note in (("original_patient_id", ""), ("original_patient_name", "수집하지 않아 비어 있음"),
                      ("original_patient_address", "수집하지 않아 비어 있음"), ("original_institution", ""),
                      ("original_patient_birth_date", "수집하지 않아 비어 있음"))],
    ("deid_subject_mappings.pseudonym_patient_id", "oncolabdb.enc_patient_mappings.pseudonym_display_id", "내보내기", ""),
    ("deid_series_mappings.run_uuid", "oncolabdb.enc_series_mappings.run_id", "내보내기", ""),
    ("deid_series_mappings.internal_hash", "oncolabdb.enc_series_mappings.internal_hash", "내보내기", ""),
    ("deid_series_mappings.series_uid", "oncolabdb.enc_series_mappings.series_uid", "내보내기",
     "기존 열이 원본/가명 중 어느 UID인지 확인 필요 (원본은 original_series_uid)"),
    ("deid_series_mappings.modality", "oncolabdb.enc_series_mappings.modality", "내보내기", ""),
    ("deid_series_mappings.original_acq_date", "oncolabdb.enc_series_mappings.ori_acq_date", "내보내기", ""),
    ("deid_series_mappings.original_acq_time", "oncolabdb.enc_series_mappings.ori_acq_time", "내보내기", ""),
    ("deid_series_mappings.out_acq_date", "oncolabdb.enc_series_mappings.enc_acq_date", "내보내기", "날짜 유지라 원본과 같음"),
    ("deid_series_mappings.out_acq_time", "oncolabdb.enc_series_mappings.enc_acq_time", "내보내기", ""),
]
# Columns of the compared tables that the new DB deliberately has no value for, and why.
NOT_MAPPED = {
    "id": "기존 DB의 auto increment 키 (새 DB는 UUID / UID, external_mappings로 연결)",
    "created_at": "기존 DB가 넣을 때 채움", "updated_at": "기존 DB가 채움", "added_at": "기존 DB가 넣을 때 채움",
    "status": "LAB 진행 상태 (COLLECTING 등) — LAB에서 관리", "tags": "LAB 큐레이션", "usage_notes": "LAB 큐레이션",
    "created_by": "LAB 사용자", "added_by": "LAB 사용자", "sort_order": "LAB 화면 순서",
    "male_count": "등록된 subjects.patient_sex에서 계산", "female_count": "등록된 subjects.patient_sex에서 계산",
    "sex_counts": "등록된 subjects.patient_sex에서 계산", "patient_count": "등록된 소속에서 계산",
    "series_count": "등록된 소속에서 계산", "modalities": "등록된 소속에서 계산",
    "source_origin": "데이터셋 출처 분류 — 접두어 SOURCE 코드에서 값 변환 (어휘 확인 필요)",
    "ai_model_version": "AI 모델 관리 (LAB)", "collection_countries": "큐레이션 입력", "collection_year": "큐레이션 입력",
    "collection_institution": "큐레이션 입력 (원본 기관명은 가명화 시 deid_subject_mappings.original_institution)",
    "year_start": "등록된 study_date에서 계산", "year_end": "등록된 study_date에서 계산",
    "contour_year_start": "LAB 컨투어 작업", "contour_year_end": "LAB 컨투어 작업",
    "disclosure_scope": "접두어 TIER(PUB/NC/IO)에서 값 변환 (어휘 확인 필요)",
    "contrast_method": "LAB 분류 설정", "contrast_custom_keywords": "LAB 분류 설정", "contrast_counts": "LAB 분류 결과",
    "sequence_method": "LAB 분류 설정", "body_part_custom_keywords": "LAB 분류 설정",
    "performed_procedure_code": "DICOM 코드 시퀀스 — 수집 안 함 (의미 확인 필요)",
    "procedure_code": "DICOM 코드 시퀀스 — 수집 안 함 (의미 확인 필요)",
    "biological_features": "LAB 분석 결과", "non_biological_features": "LAB 분석 결과",
    "contrast_predicted": "LAB AI 예측", "contrast_manual": "LAB 수동 입력", "contrast_predicted_reason": "LAB AI 예측",
    "sequence_predicted": "LAB AI 예측", "sequence_manual": "LAB 수동 입력", "sequence_predicted_reason": "LAB AI 예측",
    "implant_predicted": "LAB AI 예측", "implant_manual": "LAB 수동 입력", "implant_predicted_reason": "LAB AI 예측",
    "contour_status": "LAB 컨투어 작업", "contour_assignee": "LAB 컨투어 작업",
    "contour_assignee_comment": "LAB 컨투어 작업", "contour_reviewer": "LAB 컨투어 작업",
    "contour_reviewer_comment": "LAB 컨투어 작업", "contour_completed_at": "LAB 컨투어 작업",
    "meta_scan_status": "LAB 메타 스캔 상태", "split_origin_series_uid": "LAB 시리즈 분할 기능",
    "split_key": "LAB 시리즈 분할 기능", "split_value": "LAB 시리즈 분할 기능", "split_run_id": "LAB 시리즈 분할 기능",
    "from_stage": "기존 파이프라인 내부 단계", "publish_mode": "기존 파이프라인 내부 설정",
    "dest_working_label": "기존 파이프라인 작업 폴더",
    "PatientBirthDate": "보내는 DICOM에서 비움 (가명화) / 공개 데이터는 원본에 없음",
    "AccessionNumber": "보내는 DICOM에서 비움", "ReferringPhysicianName": "보내는 DICOM에서 비움",
    "InstancePath": "Central의 저장 위치 — 수신 확인 때 transfer_results.verification_json에 저장",
    "Tag": "Central 운영 필드", "ImportedDateTime": "Central SCP가 수신 시각을 채움",
}
# The tables a merge writes or reads value by value: every column must be mapped or listed in NOT_MAPPED.
COMPARED_TABLES = [("oncolabdb", "lab_datasets"), ("oncolabdb", "lab_dataset_items"), ("oncolabdb", "enc_pipeline_runs"),
                   ("oncolabdb", "enc_patient_mappings"), ("oncolabdb", "enc_series_mappings"),
                   ("scpdb", "dicom_instances")]


def column_coverage(snap: dict | None) -> list[dict]:
    """Every column of COMPARED_TABLES: which new-DB value fills it, or why none does."""
    if not snap:
        return []
    by_old = {}
    for new, old, how, note in MAPPING:
        by_old.setdefault(old.lower(), []).append(new)
    out = []
    for c in snap["columns"]:
        if (c["s"], c["t"]) not in COMPARED_TABLES:
            continue
        full = f"{c['s']}.{c['t']}.{c['c']}"
        news = by_old.get(full.lower())
        out.append({"column": full, "type": c["type"], "new": news or [],
                    "state": "대응" if news else ("대응 안 함" if c["c"] in NOT_MAPPED else "대응 없음"),
                    "why": "" if news else NOT_MAPPED.get(c["c"], "")})
    return out
EXPORT_TABLES = {("oncolabdb", "lab_datasets"), ("oncolabdb", "lab_dataset_items")}   # the export writes these
KNOWN = [   # observed 2026-10-27 (docs/CONTEXT.md, docs/DATABASE.md) — shown until a snapshot exists
    "MariaDB 10.5.8. scpdb 7테이블, oncolabdb 26테이블, 테이블+인덱스 약 9.45 GB.",
    "scpdb는 직접 수정하지 않음 (운영 원칙). LAB 부가 정보는 oncolabdb에 저장.",
    "scpdb.dicom_instances: PatientID, Study/Series/SOP UID, Modality, InstancePath 등. SOPInstanceUID index는 unique 아님. "
    "NOT NULL 열 다수가 빈 문자열 기본값, PatientID 기본값 0.",
    "oncolabdb.lab_datasets: name만 기본값 없음 (id auto increment), status 기본값 COLLECTING.",
    "oncolabdb.lab_dataset_items: dataset_id, series_instance_uid, study_instance_uid, patient_id 필수, "
    "source_type 기본값 local, (dataset_id, series_instance_uid) unique, dataset FK.",
]


class LegacyError(Exception):
    pass


def configured(cfg: dict) -> bool:
    return all(cfg.get(k) for k in ("host", "user", "password")) and bool(cfg.get("schemas"))


def read_schema(cfg: dict) -> dict:
    """Read the shape of cfg["schemas"] from information_schema in a read-only session."""
    if not configured(cfg):
        raise LegacyError("기존 DB 연결 정보가 없습니다 (DATAOPS_LEGACY_DB_HOST/USER/PASSWORD)")
    schemas = list(cfg["schemas"])
    try:
        conn = pymysql.connect(host=cfg["host"], port=int(cfg.get("port") or 3306), user=cfg["user"],
                               password=cfg["password"], database=None, charset="utf8mb4", autocommit=True,
                               connect_timeout=5, read_timeout=60, cursorclass=pymysql.cursors.DictCursor)
    except pymysql.MySQLError as e:
        raise LegacyError(f"기존 DB 연결 실패 ({cfg['host']}:{cfg.get('port') or 3306}): {e}") from None
    out = {"read_at": now_iso(), "host": f"{cfg['host']}:{cfg.get('port') or 3306}", "schemas": schemas}
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            marks = ", ".join(["%s"] * len(schemas))
            for name, sql in QUERIES.items():
                cur.execute(sql.format(marks) if "{}" in sql else sql, schemas if "{}" in sql else None)
                out[name] = cur.fetchall()
    except pymysql.MySQLError as e:
        raise LegacyError(f"기존 DB 구조 읽기 실패: {e}") from None
    finally:
        conn.close()
    out["version"] = out["version"][0]["v"] if out["version"] else None
    return json.loads(json.dumps(out, default=str))


def refresh(cfg: dict, runtime_dir) -> dict:
    snap = read_schema(cfg)
    write_json(runtime_dir / SNAPSHOT, snap)
    return snap


def load(runtime_dir) -> dict | None:
    p = runtime_dir / SNAPSHOT
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _shape(column_type: str | None):
    """("str", max chars) / ("num", None) / ("enum", values) / ("time", None) from an information_schema type."""
    if not column_type:
        return None
    t = column_type.lower()
    if t.startswith("enum("):
        return "enum", re.findall(r"'([^']*)'", column_type)
    m = re.match(r"(?:var)?char\((\d+)\)", t)
    if m:
        return "str", int(m.group(1))
    for name, n in (("tinytext", 255), ("mediumtext", 16_777_215), ("longtext", 4_294_967_295), ("text", 65_535),
                    ("json", 4_294_967_295)):
        if t.startswith(name):
            return "str", n
    if re.match(r"(tiny|small|medium|big)?int|decimal|float|double", t):
        return "num", None
    if re.match(r"timestamp|datetime|date|time", t):
        return "time", None
    return "other", None


# Values that are shorter than their new-DB column allows: the plan validation or DICOM itself limits them.
VALUE_LIMITS = {"datasets.license": (128, "plan 검증에서 128자로 제한"),
                "datasets.source_url": (512, "plan 검증에서 512자로 제한"),
                "series.description": (64, "DICOM SeriesDescription(LO)이라 64자 이하"),
                "studies.description": (64, "DICOM StudyDescription(LO)이라 64자 이하")}


def compat(new_type: str | None, old_type: str | None, new: str = "", how: str = "") -> tuple[str, str]:
    """Can a value of the new DB column be written into the existing column as is?"""
    if "읽어 옴" in how:
        return "맞음", "기존 DB에서 읽어 와 새 DB에 저장 (방향이 반대라 길이 문제 없음)"
    if "external_mappings" in how:
        return "ID 대응", "새 UUID ↔ 기존 숫자 id를 external_mappings에 저장해서 연결"
    if new_type is None:
        return "계산", "새 DB의 열이 아니라 등록된 값에서 만들어 넣음"
    if old_type is None:
        return "확인 전", ""
    n, o = _shape(new_type), _shape(old_type.replace(" NOT NULL", ""))
    limit, why = VALUE_LIMITS.get(new.split(" ")[0], (None, ""))
    if limit and n[0] == "str" and o[0] == "str" and limit <= o[1] < n[1]:
        return "맞음", why
    if n[0] == "enum":
        longest = max(len(v) for v in n[1])
        return ("값 변환", f"새 값 {'/'.join(n[1])} → 기존 값 형식으로 바꿔야 함") if o[0] == "str" and longest <= o[1] \
            else ("주의", "열거형 → 다른 타입")
    if n[0] == "str" and o[0] == "str":
        if n[1] <= o[1]:
            return "맞음", ""
        return "길이 주의", f"새 DB는 {n[1]:,}자까지, 기존은 {o[1]:,}자 — 긴 값은 잘리거나 거부됨"
    if n[0] == "num" and o[0] == "num":
        return "맞음", ""
    if n[0] == "num" and o[0] == "str":
        return "맞음", "숫자를 문자로 넣음"
    return "주의", f"{new_type} → {old_type}"


def view(snap: dict | None, new_types: dict | None = None) -> dict:
    """Tables grouped by schema with their columns / indexes / FKs, the mapping checked against them, and how the
    new DB's column types fit (`new_types`: {"table.column": COLUMN_TYPE} of the new DB)."""
    new_types = new_types or {}

    def new_type(new):
        key = new.split(" ")[0]
        return new_types.get(key) if re.fullmatch(r"\w+\.\w+", key) and " " not in new else None
    if not snap:
        return {"tables": [], "required": [], "coverage": [], "compared": [f"{s}.{t}" for s, t in COMPARED_TABLES],
                "mapping": [
            dict(zip(("new", "old", "how", "note"), m), state="확인 전", type=None, new_type=new_type(m[0]),
                 fit="확인 전", fit_note="") for m in MAPPING]}
    tables = {}
    for t in snap["tables"]:
        tables[(t["s"], t["t"])] = {**t, "columns": [], "indexes": {}, "fks": []}
    for c in snap["columns"]:
        if (c["s"], c["t"]) in tables:
            tables[(c["s"], c["t"])]["columns"].append(c)
    for i in snap["indexes"]:
        if (i["s"], i["t"]) in tables:
            idx = tables[(i["s"], i["t"])]["indexes"].setdefault(i["i"], {"name": i["i"], "unique": not int(i["non_unique"]),
                                                                          "columns": []})
            idx["columns"].append(i["c"])
    for f in snap["fks"]:
        if (f["s"], f["t"]) in tables:
            tables[(f["s"], f["t"])]["fks"].append(f)
    cols = {(c["s"], c["t"], c["c"].lower()): c for c in snap["columns"]}
    mapping = []
    for new, old, how, note in MAPPING:
        s, t, c = old.split(".")
        hit = cols.get((s, t, c.lower()))
        state = ("있음" if hit else ("테이블 없음" if (s, t) not in tables else "열 없음")) if s in snap["schemas"] \
            else "읽지 않은 schema"
        old_type = f"{hit['type']}{'' if hit['nullable'] == 'YES' else ' NOT NULL'}" if hit else None
        nt = new_type(new)
        fit, fit_note = compat(nt, old_type, new, how) if hit else ("확인 전", "")
        mapping.append({"new": new, "old": old, "how": how, "note": note, "state": state, "type": old_type,
                        "new_type": nt, "fit": fit, "fit_note": fit_note})
    out = sorted(tables.values(), key=lambda x: (x["s"], x["t"]))
    for t in out:
        t["indexes"] = list(t["indexes"].values())
    covered = {m["old"] for m in mapping}
    required = [{"column": f"{c['s']}.{c['t']}.{c['c']}", "type": c["type"],
                 "covered": f"{c['s']}.{c['t']}.{c['c']}" in covered}
                for c in snap["columns"] if (c["s"], c["t"]) in EXPORT_TABLES and c["nullable"] == "NO"
                and c["dflt"] is None and "auto_increment" not in (c["extra"] or "")]
    return {"tables": out, "mapping": mapping, "required": required, "coverage": column_coverage(snap),
            "compared": [f"{s}.{t}" for s, t in COMPARED_TABLES]}
