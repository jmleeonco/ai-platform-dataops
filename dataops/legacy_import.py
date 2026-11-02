"""Copy what the existing DB (oncolabdb on 10.101.1.4) already knows into the new DBs, values only.

Owner 2026-11-03: fill the management DB with the existing DB's content; copy only values the existing DB has
(nothing looked up in files yet). Scope chosen by the owner: the LAB datasets at series level
(lab_datasets + lab_dataset_items -> datasets / subjects / studies / series / dataset_series, linked through
external_mappings) and the existing pseudonymisation maps (enc_pipeline_runs / enc_patient_mappings /
enc_series_mappings -> the map DB's deid_runs / deid_subject_mappings / deid_series_mappings). No file-level
rows (scpdb.dicom_instances) and no ingest_runs: the existing DB has no run of this system.

The existing DB is read in a READ ONLY session with the SELECTs below and nothing else. Re-running updates the
copied rows; rows this system registered itself (its own runs) are never overwritten — a LAB series that is
already ours only gains the dataset link.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter

import pymysql

from .records import subject_uuid, to_db_value

SOURCE_KIND = "oncolabdb"                  # series.source_kind of a copied series
TARGET_SYSTEM = "oncolabdb.lab_datasets"   # external_mappings.target_system
NIL = "00000000-0000-0000-0000-000000000000"
SELECTS = {   # the only statements run on the existing DB
    "lab_datasets": "SELECT id, name, description, license, source_url, commercial_use, created_at "
                    "FROM lab_datasets ORDER BY id",
    "lab_dataset_items": "SELECT * FROM lab_dataset_items ORDER BY dataset_id, id",
    "enc_pipeline_runs": "SELECT * FROM enc_pipeline_runs ORDER BY id",
    "enc_patient_mappings": "SELECT * FROM enc_patient_mappings ORDER BY id",
    "enc_series_mappings": "SELECT * FROM enc_series_mappings ORDER BY id",
}
COMMERCIAL = {"available": "Y", "not_available": "N"}
# lab_dataset_items columns copied 1:1 into series (same names, migration 0004)
SERIES_SAME = ("slice_thickness", "spacing_between_slices", "pixel_spacing", "image_orientation", "patient_position",
               "image_type", "rescale_slope", "rescale_intercept", "bits_allocated", "repetition_time", "echo_time",
               "inversion_time", "flip_angle", "magnetic_field_strength", "scanning_sequence", "sequence_variant",
               "sequence_name", "diffusion_b_value", "number_of_temporal_positions", "temporal_position_identifier",
               "contrast_bolus_agent", "contrast_bolus_ingredient", "contrast_bolus_volume", "contrast_bolus_total_dose",
               "series_date", "body_part_examined")
SUBJECT_SAME = ("patient_name", "patient_sex", "patient_age", "patient_weight", "patient_ethnicity")


def dataset_uuid(lab_id) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:ai-platform-dataops:{TARGET_SYSTEM}:{lab_id}"))


def read_legacy(cfg: dict) -> dict:
    conn = pymysql.connect(host=cfg["host"], port=int(cfg.get("port") or 3306), user=cfg["user"],
                           password=cfg["password"], database="oncolabdb", charset="utf8mb4", autocommit=True,
                           connect_timeout=5, read_timeout=600, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            out = {}
            for name, sql in SELECTS.items():
                cur.execute(sql)
                out[name] = list(cur.fetchall())
            return out
    finally:
        conn.close()


def _blank(v):
    return None if v is None or (isinstance(v, str) and not v.strip()) else v


def _uid(v):
    v = _blank(v)
    return v if v is None or len(v) <= 64 else None


def _roi_names(v):
    v = _blank(v)
    names = [x.strip() for x in v.split(",") if x.strip()] if v else []   # existing DB: "A, B"; new DB: JSON list
    return json.dumps(names, ensure_ascii=False) if names else None


def management_rows(legacy: dict, ours: dict) -> tuple[dict, dict]:
    """Rows per table and what was left out. `ours` = {"series": set, "studies": set} registered by this system."""
    skipped = Counter()
    rows = {t: [] for t in ("datasets", "external_mappings", "subjects", "studies", "series", "dataset_series")}
    lab_ids = {}
    for d in legacy["lab_datasets"]:
        du = lab_ids[d["id"]] = dataset_uuid(d["id"])
        rows["datasets"].append({"dataset_uuid": du, "name": d["name"], "description": _blank(d["description"]),
                                 "license": _blank(d["license"]), "source_url": _blank(d["source_url"]),
                                 "commercial_use": COMMERCIAL.get(d["commercial_use"], "U"),
                                 "created_at": d["created_at"]})
        rows["external_mappings"].append({"dataset_uuid": du, "target_system": TARGET_SYSTEM,
                                          "target_dataset_id": d["id"]})
    subjects, studies, series, links = {}, {}, {}, set()
    for it in legacy["lab_dataset_items"]:
        du = lab_ids.get(it["dataset_id"])
        se_uid, st_uid = _uid(it["series_instance_uid"]), _uid(it["study_instance_uid"])
        if du is None or se_uid is None or st_uid is None or not _blank(it["patient_id"]):
            skipped["UID가 64자를 넘거나 비어 있는 항목" if du else "데이터셋 없는 항목"] += 1
            continue
        pid = it["patient_id"]
        su = subject_uuid(du, pid)
        s = subjects.setdefault(su, {"subject_uuid": su, "dataset_uuid": du, "source_subject_key": pid,
                                     "patient_id": pid, **{c: None for c in SUBJECT_SAME}})
        for c in SUBJECT_SAME:                      # first non-empty value in the existing DB's order
            if s[c] is None:
                s[c] = _blank(it.get(c))
        links.add((du, se_uid))
        if st_uid not in studies:
            if st_uid in ours["studies"]:
                studies[st_uid] = None
            else:
                date = _blank(it["study_date"])
                studies[st_uid] = {"study_uid": st_uid, "subject_uuid": su,
                                   "study_date": date if date and len(date) == 8 and date.isdigit() else None,
                                   "description": _blank(it["study_description"])}
        elif studies[st_uid] and studies[st_uid]["subject_uuid"] != su:
            skipped["다른 환자·데이터셋에 이미 들어간 study (먼저 나온 쪽 유지)"] += 1
        if se_uid in series or se_uid in ours["series"]:
            continue
        ref = it.get("referenced_series_uid")
        if _blank(ref) and _uid(ref) is None:
            skipped["referenced_series_uid가 64자를 넘어 비움"] += 1
        num = _blank(it["series_number"])
        series[se_uid] = {
            "series_uid": se_uid, "study_uid": st_uid, "modality": it["modality"] or "OT",
            "description": _blank(it["series_description"]),
            "series_number": int(num) if num is not None and str(num).lstrip("-").isdigit() else None,
            "instance_count": it["instance_count"] or 0, "source_kind": SOURCE_KIND,
            "modality_basis": "기존 DB oncolabdb.lab_dataset_items.modality",
            "referenced_series_uid": _uid(ref), "roi_names": _roi_names(it["roi_names"]),
            "metadata_json": {"oncolabdb": {"lab_dataset_item_id": it["id"], "source_type": it["source_type"],
                                            "study_path": _blank(it["study_path"])}},
            **{c: _blank(it.get(c)) for c in SERIES_SAME}}
    rows["subjects"] = list(subjects.values())
    rows["studies"] = [s for s in studies.values() if s]
    rows["series"] = list(series.values())
    rows["dataset_series"] = [{"dataset_uuid": d, "series_uid": s} for d, s in sorted(links)]
    return rows, dict(skipped)


def map_rows(legacy: dict, ours: dict) -> tuple[dict, dict]:
    """deid_* rows from enc_*; `ours` = keys of the map DB that this system's own runs wrote."""
    skipped = Counter()
    runs, lab = {}, {}
    known = {d["id"] for d in legacy["lab_datasets"]}
    for r in legacy["enc_pipeline_runs"]:
        lab[r["run_id"]] = dataset_uuid(r["lab_dataset_id"]) if r["lab_dataset_id"] is not None else NIL
        try:
            summary = json.loads(r["summary_json"]) if r["summary_json"] else None
        except ValueError:
            summary = r["summary_json"]
        extra = {k: (str(v) if v is not None and k.endswith("_at") else v) for k, v in r.items()
                 if k not in ("run_id", "patient_id_prefix", "pseudonym_internal_hash_label", "pipeline_version",
                              "summary_json")}
        extra["lab_dataset_known"] = r["lab_dataset_id"] in known
        runs[r["run_id"]] = {"run_uuid": r["run_id"], "dataset_uuid": lab[r["run_id"]], "profile": "oncolabdb-enc",
                             "patient_id_prefix": r["patient_id_prefix"],
                             "key_label": r["pseudonym_internal_hash_label"] or "",
                             "pipeline_version": r["pipeline_version"],
                             "summary_json": {"oncolabdb.enc_pipeline_runs": extra, "summary": summary},
                             "created_at": r["started_at"]}
    subj, ser = {}, {}
    for m in legacy["enc_patient_mappings"]:
        du = lab.get(m["run_id"], NIL)
        su = subject_uuid(du, m["pseudonym_display_id"])
        if su in ours["deid_subject_mappings"]:
            skipped["이 시스템이 쓴 대상자 대응과 같은 키"] += 1
            continue
        if su in subj:
            if subj[su]["original_patient_id"] != (m["original_patient_id"] or ""):
                skipped["같은 가명에 다른 원본 (먼저 나온 쪽 유지)"] += 1
            continue
        subj[su] = {"subject_uuid": su, "dataset_uuid": du, "run_uuid": m["run_id"], "internal_hash": m["internal_hash"],
                    "original_patient_id": m["original_patient_id"] or "",
                    "original_patient_name": _blank(m["original_patient_name"]),
                    "original_patient_birth_date": _blank(m["original_patient_birth_date"]),
                    "original_patient_address": _blank(m["original_patient_address"]),
                    "original_institution": _blank(m["original_institution"]),
                    "pseudonym_patient_id": m["pseudonym_display_id"], "created_at": m["created_at"]}
    for m in legacy["enc_series_mappings"]:
        uid = m["series_uid"]
        if uid in ours["deid_series_mappings"]:
            skipped["이 시스템이 쓴 시리즈 대응과 같은 키"] += 1
            continue
        if uid in ser:
            skipped["여러 실행에 나온 시리즈 (먼저 나온 실행 유지)"] += 1
            continue
        ser[uid] = {"series_uid": uid, "run_uuid": m["run_id"], "internal_hash": m["internal_hash"],
                    "modality": m["modality"], "original_acq_date": _blank(m["ori_acq_date"]),
                    "original_acq_time": _blank(m["ori_acq_time"]), "out_acq_date": _blank(m["enc_acq_date"]),
                    "out_acq_time": _blank(m["enc_acq_time"]), "created_at": m["created_at"]}
    return ({"deid_runs": list(runs.values()), "deid_subject_mappings": list(subj.values()),
             "deid_series_mappings": list(ser.values())}, dict(skipped))


KEYS = {"datasets": ("dataset_uuid",), "external_mappings": ("dataset_uuid", "target_system"),
        "subjects": ("subject_uuid",), "studies": ("study_uid",), "series": ("series_uid",),
        "dataset_series": ("dataset_uuid", "series_uid"), "deid_runs": ("run_uuid",),
        "deid_subject_mappings": ("subject_uuid",), "deid_series_mappings": ("series_uid",)}


def upsert(cur, table: str, rows: list[dict], batch: int = 1000) -> None:
    if not rows:
        return
    cols = list(rows[0])
    upd = [c for c in cols if c not in KEYS[table] and c != "created_at"]
    sql = (f"INSERT INTO `{table}` ({', '.join(f'`{c}`' for c in cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
           + (f" ON DUPLICATE KEY UPDATE {', '.join(f'`{c}`=VALUES(`{c}`)' for c in upd)}" if upd
              else f" ON DUPLICATE KEY UPDATE `{cols[0]}`=`{cols[0]}`"))
    for i in range(0, len(rows), batch):
        cur.executemany(sql, [[to_db_value(c, r[c]) for c in cols] for r in rows[i:i + batch]])


def _ours(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT series_uid, study_uid FROM series WHERE source_kind<>%s", (SOURCE_KIND,))
        got = cur.fetchall()
    return {"series": {r["series_uid"] for r in got}, "studies": {r["study_uid"] for r in got}}


def _ours_map(conn, legacy_runs: set) -> dict:
    out = {}
    with conn.cursor() as cur:
        for t, k in (("deid_subject_mappings", "subject_uuid"), ("deid_series_mappings", "series_uid")):
            cur.execute(f"SELECT {k} AS k, run_uuid FROM {t}")
            out[t] = {r["k"] for r in cur.fetchall() if r["run_uuid"] not in legacy_runs}
    return out


def run_import(settings, dry_run: bool = False, log=print) -> dict:
    from . import db as dbmod
    log("기존 DB 읽는 중 (READ ONLY)")
    legacy = read_legacy(settings.legacy_db)
    log("읽음: " + ", ".join(f"{k} {len(v):,}" for k, v in legacy.items()))
    report = {"read": {k: len(v) for k, v in legacy.items()}}
    with dbmod.connection(settings.db) as conn:
        rows, skipped = management_rows(legacy, _ours(conn))
        report["management"] = {t: len(r) for t, r in rows.items()}
        report["management_skipped"] = skipped
        if not dry_run:
            try:
                with conn.cursor() as cur:
                    for t in ("datasets", "external_mappings", "subjects", "studies", "series", "dataset_series"):
                        upsert(cur, t, rows[t])
                        log(f"관리 DB {t}: {len(rows[t]):,}행")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    if settings.deid_map_configured:
        with dbmod.connection(settings.deid_map_db) as mconn:
            mrows, mskipped = map_rows(legacy, _ours_map(mconn, {r["run_id"] for r in legacy["enc_pipeline_runs"]}))
            report["map"] = {t: len(r) for t, r in mrows.items()}
            report["map_skipped"] = mskipped
            if not dry_run:
                try:
                    with mconn.cursor() as cur:
                        for t in ("deid_runs", "deid_subject_mappings", "deid_series_mappings"):
                            upsert(cur, t, mrows[t])
                            log(f"가명 대응 DB {t}: {len(mrows[t]):,}행")
                    mconn.commit()
                except Exception:
                    mconn.rollback()
                    raise
    else:
        report["map"] = "가명 대응 DB 설정 없음 — 건너뜀"
    return report
