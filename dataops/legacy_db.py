"""The existing production DB (scpdb / oncolabdb on MariaDB 10.5.8), read for its *shape* only.

Only information_schema is queried (tables, columns, indexes, foreign keys) plus VERSION(), in a read-only
session; no row of a production table is ever read or written. The result is kept as a snapshot in the
runtime folder so the page works without a connection. `MAPPING` says where each value of the new DB goes
in the existing one (docs/DATABASE.md "Export/import to oncolabdb") and is checked against the snapshot.
"""
from __future__ import annotations

import json

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


def view(snap: dict | None) -> dict:
    """Tables grouped by schema with their columns / indexes / FKs, and the mapping checked against them."""
    if not snap:
        return {"tables": [], "mapping": [dict(zip(("new", "old", "how", "note"), m), state="확인 전", type=None)
                                          for m in MAPPING]}
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
        mapping.append({"new": new, "old": old, "how": how, "note": note, "state": state,
                        "type": f"{hit['type']}{'' if hit['nullable'] == 'YES' else ' NOT NULL'}" if hit else None})
    out = sorted(tables.values(), key=lambda x: (x["s"], x["t"]))
    for t in out:
        t["indexes"] = list(t["indexes"].values())
    return {"tables": out, "mapping": mapping}
