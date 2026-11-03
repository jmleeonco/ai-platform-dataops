"""The management DB as a catalogue: datasets (by origin), one dataset's series, one series, and a search across all.

Read only. Every dataset is one of three origins: this system's own runs, the existing DB's LAB datasets
(import-legacy, an external_mappings row) or a /mnt/deploy/data training bundle (import-deploy, name deploy/…).
Source values are shown as they are (see docs: the web shows real values on the source side).
"""
from __future__ import annotations

import json
import re
import time

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .. import db as dbmod

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
UID_RE = re.compile(r"[0-9.]{1,64}")
PAGE = 100
SEARCH_LIMIT = 200
LIST_TTL = 60                      # seconds the dataset list's counts are reused (they change on imports only)
ORIGINS = {"own": "이 시스템", "legacy": "기존 DB", "deploy": "배포 묶음"}
ORIGIN_SQL = ("CASE WHEN x.target_dataset_id IS NOT NULL THEN 'legacy' WHEN d.name LIKE 'deploy/%%' THEN 'deploy'"
              " ELSE 'own' END")
SOURCE_KINDS = {"dicom": "DICOM", "oncolabdb": "기존 DB", "deploy": "배포 묶음 파일", "central-find": "Central 조회"}


def roi_list(v) -> list[str]:
    if not v:
        return []
    try:
        got = json.loads(v)
    except ValueError:
        return [str(v)]
    return [str(x) for x in got] if isinstance(got, list) else [str(got)]


def register_catalog_views(app, page, db_conn) -> None:
    def with_conn(request, fn, template_on_error="datasets.html"):
        conn, err = db_conn()
        if conn is None:
            return page(request, template_on_error, rows=[], totals={}, db_error=err, origin="", q="", counts={},
                        res={}, origins=ORIGINS, n_all=0, limit=SEARCH_LIMIT)
        try:
            return fn(conn)
        finally:
            conn.close()

    listed = {}

    def dataset_list(conn):
        if listed and time.monotonic() - listed["at"] < LIST_TTL:
            return listed["rows"], listed["totals"]
        rows = dbmod.query(conn, f"""
                SELECT d.dataset_uuid, d.name, d.license, d.commercial_use, d.patient_id_prefix, d.created_at,
                       x.target_dataset_id AS legacy_id, {ORIGIN_SQL} AS origin,
                       COALESCE(sb.n, 0) AS n_subjects, COALESCE(ag.n_series, 0) AS n_series,
                       COALESCE(ag.n_rt, 0) AS n_rt, COALESCE(ag.n_inst, 0) AS n_instances,
                       COALESCE(ag.n_train, 0) AS n_train, COALESCE(ag.n_valid, 0) AS n_valid
                FROM datasets d
                LEFT JOIN external_mappings x ON x.dataset_uuid=d.dataset_uuid
                     AND x.target_system='oncolabdb.lab_datasets'
                LEFT JOIN (SELECT dataset_uuid, COUNT(DISTINCT pid) AS n FROM (  -- own patients + linked series patients
                             SELECT dataset_uuid, patient_id AS pid FROM subjects
                             UNION SELECT m.dataset_uuid, sj.patient_id FROM dataset_series m
                               JOIN series se ON se.series_uid=m.series_uid JOIN studies st ON st.study_uid=se.study_uid
                               JOIN subjects sj ON sj.subject_uuid=st.subject_uuid) u GROUP BY dataset_uuid) sb
                     ON sb.dataset_uuid=d.dataset_uuid
                LEFT JOIN (SELECT m.dataset_uuid, COUNT(*) AS n_series, SUM(se.modality='RTSTRUCT') AS n_rt,
                                  SUM(se.instance_count) AS n_inst, SUM(m.split LIKE 'train%%') AS n_train,
                                  SUM(m.split LIKE 'valid%%') AS n_valid
                           FROM dataset_series m JOIN series se ON se.series_uid=m.series_uid
                           GROUP BY m.dataset_uuid) ag ON ag.dataset_uuid=d.dataset_uuid
                ORDER BY d.name""")
        totals = dbmod.query(conn, "SELECT (SELECT COUNT(DISTINCT patient_id) FROM subjects) AS subjects,"
                                   " (SELECT COUNT(*) FROM studies) AS studies,"
                                   " (SELECT COUNT(*) FROM series) AS series,"
                                   " (SELECT COALESCE(SUM(instance_count),0) FROM series) AS instances")[0]
        listed.update(at=time.monotonic(), rows=rows, totals=totals)
        return rows, totals

    # ------------------------------------------------------------------ every dataset
    @app.get("/datasets", response_class=HTMLResponse)
    def datasets(request: Request, origin: str = "", q: str = ""):
        def run(conn):
            rows, totals = dataset_list(conn)
            counts = {k: sum(1 for r in rows if r["origin"] == k) for k in ORIGINS}
            shown = [r for r in rows if (not origin or r["origin"] == origin)
                     and (not q or q.lower() in r["name"].lower())]
            return page(request, "datasets.html", rows=shown, counts=counts, totals=totals, n_all=len(rows),
                        origin=origin if origin in ORIGINS else "", q=q, origins=ORIGINS, db_error=None)
        return with_conn(request, run)

    # ------------------------------------------------------------------ one dataset
    @app.get("/datasets/{dataset_uuid}", response_class=HTMLResponse)
    def dataset_page(request: Request, dataset_uuid: str, modality: str | None = None, q: str | None = None,
                     split: str | None = None, p: int = Query(1, ge=1)):
        if not UUID_RE.fullmatch(dataset_uuid):
            raise HTTPException(404)

        def run(conn):
            ds = dbmod.query(conn, f"SELECT d.*, x.target_dataset_id AS legacy_id, {ORIGIN_SQL} AS origin"
                                   " FROM datasets d LEFT JOIN external_mappings x ON x.dataset_uuid=d.dataset_uuid"
                                   " AND x.target_system='oncolabdb.lab_datasets' WHERE d.dataset_uuid=%s",
                             (dataset_uuid,))
            if not ds:
                raise HTTPException(404)
            where, args = ["m.dataset_uuid=%s"], [dataset_uuid]
            if modality:
                where.append("se.modality=%s")
                args.append(modality)
            if split:
                where.append("m.split IS NULL" if split == "-" else "m.split=%s")
                args += [] if split == "-" else [split]
            if q:
                where.append("(sj.patient_id LIKE %s OR se.description LIKE %s OR se.series_uid LIKE %s"
                             " OR se.roi_names LIKE %s)")
                args += [f"%{q}%"] * 4
            base = (" FROM dataset_series m JOIN series se ON se.series_uid=m.series_uid"
                    " JOIN studies st ON st.study_uid=se.study_uid JOIN subjects sj ON sj.subject_uuid=st.subject_uuid"
                    " WHERE " + " AND ".join(where))
            total = dbmod.query(conn, "SELECT COUNT(*) AS n" + base, args)[0]["n"]
            rows = dbmod.query(conn, "SELECT sj.patient_id, sj.source_subject_key, st.study_uid, st.study_date,"
                                     " se.series_uid, se.modality, se.description, se.instance_count, se.source_kind,"
                                     " m.split, se.roi_names, se.needs_review, se.modality_basis, se.first_run_uuid,"
                                     " se.metadata_json, (SELECT r.run_label FROM ingest_runs r"
                                     " WHERE r.run_uuid=se.first_run_uuid) AS run_label" + base +
                                     " ORDER BY sj.patient_id, st.study_uid, se.series_number"
                                     f" LIMIT {PAGE} OFFSET %s", [*args, (p - 1) * PAGE])
            modalities = dbmod.query(conn, "SELECT se.modality, COUNT(*) AS n FROM dataset_series m JOIN series se"
                                           " ON se.series_uid=m.series_uid WHERE m.dataset_uuid=%s"
                                           " GROUP BY se.modality ORDER BY n DESC", (dataset_uuid,))
            splits = dbmod.query(conn, "SELECT COALESCE(split, '-') AS split, COUNT(*) AS n FROM dataset_series"
                                       " WHERE dataset_uuid=%s GROUP BY split ORDER BY n DESC", (dataset_uuid,))
            kinds = dbmod.query(conn, "SELECT se.source_kind, COUNT(*) AS n FROM dataset_series m JOIN series se"
                                      " ON se.series_uid=m.series_uid WHERE m.dataset_uuid=%s"
                                      " GROUP BY se.source_kind ORDER BY n DESC", (dataset_uuid,))
            n_subjects = dbmod.query(conn, "SELECT COUNT(*) AS n FROM (SELECT patient_id FROM subjects"
                                           " WHERE dataset_uuid=%s UNION SELECT sj.patient_id FROM dataset_series m"
                                           " JOIN series se ON se.series_uid=m.series_uid"
                                           " JOIN studies st ON st.study_uid=se.study_uid"
                                           " JOIN subjects sj ON sj.subject_uuid=st.subject_uuid"
                                           " WHERE m.dataset_uuid=%s) u", (dataset_uuid, dataset_uuid))[0]["n"]
            delivery = dbmod.query(conn, "SELECT t.target_key, t.status, COUNT(*) AS n FROM transfer_results t"
                                         " JOIN instances i ON i.sop_uid=t.sop_uid JOIN dataset_series m"
                                         " ON m.series_uid=i.series_uid WHERE m.dataset_uuid=%s"
                                         " GROUP BY t.target_key, t.status", (dataset_uuid,))
            for r in rows:
                r["meta"] = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                r["rois"] = roi_list(r["roi_names"])
            return page(request, "dataset.html", ds=ds[0], rows=rows, total=total, p=p,
                        pages=max(1, -(-total // PAGE)), modality=modality or "", q=q or "", split=split or "",
                        modalities=modalities, splits=splits, kinds=kinds, n_subjects=n_subjects,
                        delivery=delivery, origins=ORIGINS, source_kinds=SOURCE_KINDS)
        return with_conn(request, run)

    # ------------------------------------------------------------------ one series
    @app.get("/series/{series_uid}", response_class=HTMLResponse)
    def series_page(request: Request, series_uid: str):
        if not UID_RE.fullmatch(series_uid):
            raise HTTPException(404)

        def run(conn):
            got = dbmod.query(conn, "SELECT se.*, st.study_date, st.description AS study_description,"
                                    " st.subject_uuid, sj.patient_id, sj.patient_name, sj.patient_sex, sj.patient_age,"
                                    " sj.dataset_uuid AS subject_dataset_uuid, d.name AS subject_dataset_name,"
                                    " (SELECT r.run_label FROM ingest_runs r WHERE r.run_uuid=se.first_run_uuid)"
                                    " AS run_label"
                                    " FROM series se JOIN studies st ON st.study_uid=se.study_uid"
                                    " JOIN subjects sj ON sj.subject_uuid=st.subject_uuid"
                                    " JOIN datasets d ON d.dataset_uuid=sj.dataset_uuid WHERE se.series_uid=%s",
                              (series_uid,))
            if not got:
                raise HTTPException(404)
            s = got[0]
            member = dbmod.query(conn, f"SELECT d.dataset_uuid, d.name, m.split, {ORIGIN_SQL} AS origin"
                                       " FROM dataset_series m JOIN datasets d ON d.dataset_uuid=m.dataset_uuid"
                                       " LEFT JOIN external_mappings x ON x.dataset_uuid=d.dataset_uuid"
                                       " AND x.target_system='oncolabdb.lab_datasets'"
                                       " WHERE m.series_uid=%s ORDER BY d.name", (series_uid,))
            refs = dbmod.query(conn, "SELECT series_uid, modality, description, instance_count FROM series"
                                     " WHERE series_uid=%s", (s["referenced_series_uid"],)) \
                if s.get("referenced_series_uid") else []
            referencing = dbmod.query(conn, "SELECT series_uid, modality, description, roi_names FROM series"
                                            " WHERE referenced_series_uid=%s ORDER BY series_uid", (series_uid,))
            for r in referencing:
                r["rois"] = roi_list(r["roi_names"])
            siblings = dbmod.query(conn, "SELECT series_uid, modality, description, instance_count FROM series"
                                         " WHERE study_uid=%s AND series_uid<>%s ORDER BY series_number, series_uid",
                                   (s["study_uid"], series_uid))
            delivery = dbmod.query(conn, "SELECT t.target_key, t.status, COUNT(*) AS n FROM transfer_results t"
                                         " JOIN instances i ON i.sop_uid=t.sop_uid WHERE i.series_uid=%s"
                                         " GROUP BY t.target_key, t.status", (series_uid,))
            meta = json.loads(s["metadata_json"]) if s.get("metadata_json") else {}
            skip = {"metadata_json", "roi_names", "subject_uuid", "subject_dataset_uuid", "subject_dataset_name",
                    "run_label", "patient_id", "patient_name", "patient_sex", "patient_age", "study_date",
                    "study_description"}
            columns = {k: v for k, v in s.items() if k not in skip and v not in (None, "")}
            return page(request, "series_db.html", s=s, rois=roi_list(s.get("roi_names")), member=member,
                        refs=refs, referencing=referencing, siblings=siblings, delivery=delivery, meta=meta,
                        columns=columns, origins=ORIGINS, source_kinds=SOURCE_KINDS)
        return with_conn(request, run)

    # ------------------------------------------------------------------ search everything
    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = ""):
        q = q.strip()

        def run(conn):
            res = {"datasets": [], "subjects": [], "series": [], "rois": [], "roi_names": []}
            if len(q) >= 2:
                like = f"%{q}%"
                res["datasets"] = dbmod.query(conn, "SELECT dataset_uuid, name FROM datasets WHERE name LIKE %s"
                                                    " ORDER BY name LIMIT %s", (like, SEARCH_LIMIT))
                found = dbmod.query(conn, "SELECT sj.patient_id, sj.patient_name, d.dataset_uuid, d.name"
                                          " FROM subjects sj JOIN datasets d ON d.dataset_uuid=sj.dataset_uuid"
                                          " WHERE sj.patient_id LIKE %s OR sj.patient_name LIKE %s"
                                          " UNION SELECT sj.patient_id, sj.patient_name, d.dataset_uuid, d.name"
                                          " FROM subjects sj JOIN studies st ON st.subject_uuid=sj.subject_uuid"
                                          " JOIN series se ON se.study_uid=st.study_uid"
                                          " JOIN dataset_series m ON m.series_uid=se.series_uid"
                                          " JOIN datasets d ON d.dataset_uuid=m.dataset_uuid"
                                          " WHERE sj.patient_id LIKE %s OR sj.patient_name LIKE %s"
                                          " ORDER BY patient_id, name LIMIT %s", (like, like, like, like, SEARCH_LIMIT * 10))
                people = {}                                   # one row per PatientID: every dataset that has it
                for r in found:
                    e = people.setdefault(r["patient_id"], {"patient_id": r["patient_id"], "names": set(),
                                                            "datasets": {}})
                    if r["patient_name"]:
                        e["names"].add(r["patient_name"])
                    e["datasets"][r["dataset_uuid"]] = r["name"]
                res["subjects"] = list(people.values())[:SEARCH_LIMIT]
                res["series"] = dbmod.query(conn, "SELECT se.series_uid, se.modality, se.description, sj.patient_id,"
                                                  " (SELECT COUNT(*) FROM dataset_series m"
                                                  "  WHERE m.series_uid=se.series_uid) AS n_datasets"
                                                  " FROM series se JOIN studies st ON st.study_uid=se.study_uid"
                                                  " JOIN subjects sj ON sj.subject_uuid=st.subject_uuid"
                                                  " WHERE se.series_uid LIKE %s OR se.study_uid LIKE %s"
                                                  " OR se.description LIKE %s ORDER BY se.series_uid LIMIT %s",
                                            (f"{q}%", f"{q}%", like, SEARCH_LIMIT))
                res["rois"] = dbmod.query(conn, "SELECT se.series_uid, se.roi_names, sj.patient_id,"
                                                " (SELECT COUNT(*) FROM dataset_series m"
                                                "  WHERE m.series_uid=se.series_uid) AS n_datasets"
                                                " FROM series se JOIN studies st ON st.study_uid=se.study_uid"
                                                " JOIN subjects sj ON sj.subject_uuid=st.subject_uuid"
                                                " WHERE se.modality='RTSTRUCT' AND se.roi_names LIKE %s"
                                                " ORDER BY sj.patient_id LIMIT %s", (like, SEARCH_LIMIT))
                for r in res["rois"]:
                    r["hits"] = [x for x in roi_list(r["roi_names"]) if q.lower() in x.lower()]
                names = {}                                    # every matching ROI name: structure sets, datasets
                for r in dbmod.query(conn, "SELECT se.series_uid, se.roi_names, m.dataset_uuid, d.name"
                                           " FROM series se JOIN dataset_series m ON m.series_uid=se.series_uid"
                                           " JOIN datasets d ON d.dataset_uuid=m.dataset_uuid"
                                           " WHERE se.modality='RTSTRUCT' AND se.roi_names LIKE %s", (like,)):
                    for x in roi_list(r["roi_names"]):
                        if q.lower() in x.lower():
                            e = names.setdefault(x, {"name": x, "series": set(), "datasets": {}})
                            e["series"].add(r["series_uid"])
                            e["datasets"][r["dataset_uuid"]] = r["name"]
                res["roi_names"] = sorted(names.values(), key=lambda e: (-len(e["series"]), e["name"]))
            return page(request, "search.html", q=q, res=res, limit=SEARCH_LIMIT, db_error=None)
        return with_conn(request, run, "search.html")
