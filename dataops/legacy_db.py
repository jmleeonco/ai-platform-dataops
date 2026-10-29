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
    ("series.metadata_json (간격)", "oncolabdb.lab_dataset_items.slice_thickness", "값 꺼내서 내보내기",
     "pixel_spacing, image_orientation도 같음"),
    ("RTSTRUCT의 ROI 이름", "oncolabdb.lab_dataset_items.roi_names", "내보내기", "라벨 시리즈만"),
    ("RTSTRUCT가 가리키는 영상", "oncolabdb.lab_dataset_items.referenced_series_uid", "내보내기", "라벨 시리즈만"),
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
]
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
        return {"tables": [], "required": [], "mapping": [
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
    return {"tables": out, "mapping": mapping, "required": required}
