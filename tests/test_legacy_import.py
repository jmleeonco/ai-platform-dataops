import datetime
import json

from dataops import db as dbmod
from dataops import legacy_import as li

T = datetime.datetime(2026, 1, 2, 3, 4, 5)


def item(i, ds, series, study, pid, **kw):
    base = {"id": i, "dataset_id": ds, "series_instance_uid": series, "study_instance_uid": study, "patient_id": pid,
            "source_type": "pacs", "patient_name": pid, "modality": "CT", "series_description": "s",
            "series_number": "3", "study_description": "st", "study_date": "20200101", "instance_count": 10,
            "roi_names": None, "referenced_series_uid": None, "study_path": "", **{c: None for c in li.SERIES_SAME},
            "patient_sex": None, "patient_age": None, "patient_weight": None, "patient_ethnicity": None}
    return {**base, **kw}


LEGACY = {
    "lab_datasets": [{"id": 1, "name": "A", "description": "", "license": None, "source_url": None,
                      "commercial_use": "not_available", "created_at": T},
                     {"id": 2, "name": "B", "description": "d", "license": None, "source_url": None,
                      "commercial_use": "available", "created_at": T}],
    "lab_dataset_items": [
        item(1, 1, "1.2.1", "1.1", "P1", patient_sex="M"),
        item(2, 1, "1.2.2", "1.1", "P1", modality="RTSTRUCT", roi_names="GTV, CTV ,", referenced_series_uid="1.2.1",
             patient_age="050Y"),
        item(3, 2, "1.2.1", "1.1", "Q1"),                 # same series & study in a second dataset, other patient id
        item(4, 2, "1." + "9" * 70, "1.3", "Q2"),         # UID over 64 characters
        item(5, 2, "1.2.9", "1.4", "Q2", study_date="", series_number=None),
        item(6, 2, "1.2.ours", "1.ours", "Q3"),           # a series this system registered itself
    ],
    "enc_pipeline_runs": [{"id": 1, "run_id": "r1", "started_at": T, "completed_at": None, "terminal_status": "success",
                           "operator_subject": "x", "config_fingerprint": None, "from_stage": None,
                           "publish_mode": None, "src_dataset_name": "A", "dest_working_label": None,
                           "patient_id_prefix": "INT_X", "pseudonym_internal_hash_label": "k", "pipeline_version": "1",
                           "summary_json": '{"n": 1}', "lab_dataset_id": 1, "created_at": T},
                          {"id": 2, "run_id": "r2", "started_at": T, "completed_at": T, "terminal_status": "failed",
                           "operator_subject": None, "config_fingerprint": None, "from_stage": None,
                           "publish_mode": None, "src_dataset_name": None, "dest_working_label": None,
                           "patient_id_prefix": None, "pseudonym_internal_hash_label": "k", "pipeline_version": None,
                           "summary_json": None, "lab_dataset_id": None, "created_at": T}],
    "enc_patient_mappings": [{"id": 1, "run_id": "r1", "internal_hash": "h", "original_patient_id": "O1",
                              "original_patient_name": None, "original_patient_address": None,
                              "original_institution": "I", "pseudonym_display_id": "P1", "created_at": T,
                              "original_patient_birth_date": None}],
    "enc_series_mappings": [{"id": i, "run_id": r, "internal_hash": "h", "series_uid": "1.2.1", "modality": "CT",
                             "ori_acq_date": "20200101", "ori_acq_time": None, "enc_acq_date": "20200101",
                             "enc_acq_time": None, "created_at": T} for i, r in ((1, "r1"), (2, "r2"))],
}


def test_rows_copy_values_only_and_keep_the_first_owner():
    rows, skipped = li.management_rows(LEGACY, {"series": {"1.2.ours"}, "studies": {"1.ours"}})
    d1, d2 = li.dataset_uuid(1), li.dataset_uuid(2)
    assert [d["commercial_use"] for d in rows["datasets"]] == ["N", "Y"]
    assert {(x["dataset_uuid"], x["target_dataset_id"]) for x in rows["external_mappings"]} == {(d1, 1), (d2, 2)}
    series = {s["series_uid"]: s for s in rows["series"]}
    assert set(series) == {"1.2.1", "1.2.2", "1.2.9"}                       # not the long UID, not ours
    assert json.loads(series["1.2.2"]["roi_names"]) == ["GTV", "CTV"]
    assert series["1.2.9"]["series_number"] is None and series["1.2.1"]["source_kind"] == "oncolabdb"
    studies = {s["study_uid"]: s for s in rows["studies"]}
    assert studies["1.1"]["subject_uuid"] == li.subject_uuid(d1, "P1") and studies["1.4"]["study_date"] is None
    assert "1.ours" not in studies
    assert {(x["dataset_uuid"], x["series_uid"]) for x in rows["dataset_series"]} >= {(d2, "1.2.1"), (d2, "1.2.ours")}
    p1 = next(s for s in rows["subjects"] if s["patient_id"] == "P1")
    assert (p1["patient_sex"], p1["patient_age"]) == ("M", "050Y")           # first non-empty value per patient
    assert skipped["UID가 64자를 넘거나 비어 있는 항목"] == 1
    assert skipped["다른 환자·데이터셋에 이미 들어간 study (먼저 나온 쪽 유지)"] == 1


def test_map_rows_from_the_enc_tables():
    rows, skipped = li.map_rows(LEGACY, {"deid_subject_mappings": set(), "deid_series_mappings": set()})
    runs = {r["run_uuid"]: r for r in rows["deid_runs"]}
    assert runs["r1"]["dataset_uuid"] == li.dataset_uuid(1) and runs["r2"]["dataset_uuid"] == li.NIL
    assert runs["r1"]["summary_json"]["summary"] == {"n": 1}
    assert runs["r1"]["summary_json"]["oncolabdb.enc_pipeline_runs"]["terminal_status"] == "success"
    (s,) = rows["deid_subject_mappings"]
    assert (s["original_patient_id"], s["pseudonym_patient_id"]) == ("O1", "P1")
    assert s["subject_uuid"] == li.subject_uuid(li.dataset_uuid(1), "P1")   # the same key as the management DB
    assert [(m["series_uid"], m["run_uuid"]) for m in rows["deid_series_mappings"]] == [("1.2.1", "r1")]
    assert skipped == {"여러 실행에 나온 시리즈 (먼저 나온 실행 유지)": 1}


def test_upsert_is_repeatable(test_db):
    with dbmod.connection(test_db) as conn:
        dbmod.migrate(conn)
        for _ in range(2):
            rows, _ = li.management_rows(LEGACY, li._ours(conn))
            with conn.cursor() as cur:
                for t in ("datasets", "external_mappings", "subjects", "studies", "series", "dataset_series"):
                    li.upsert(cur, t, rows[t])
            conn.commit()
        counts = dbmod.table_counts(conn)
    assert (counts["datasets"], counts["series"], counts["dataset_series"]) == (2, 4, 5)
